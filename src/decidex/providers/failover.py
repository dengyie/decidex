"""
Priority-ordered failover orchestrator across inference providers.

Routes every infer() call to the highest-priority healthy backend and
transparently degrades to lower-priority backends when the primary is
rate-limited (429), quota/wallet-exhausted (402), revoked (401) or
deterministically misconfigured (400/403/404/422).

Typical wiring: FuturePPO System One gateway first (free primary data source),
Nace Drex hosted decisions next (cheap paid tier), MindsHub free Jev gateway
after that (consume free promotion quota), official TypeSafe key pool as the
durable fallback.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import httpx

from decidex.pool import GATEWAY_ORGS, KeyPool
from decidex.providers.base import BaseInferenceProvider
from decidex.providers.typesafe import TypeSafeJevProvider
from decidex.types import DecisionVerdict, ObservationPayload, QuestionSpec

DEFAULT_FUTUREPPO_BASE_URL = "https://pool.futureppo.top/v1/systemone"
DEFAULT_FUTUREPPO_MODEL = "jev-latest"


class RouteState(str, Enum):
    ACTIVE = "active"
    COOLDOWN = "cooldown"
    DEAD = "dead"


class NoAvailableProvidersError(RuntimeError):
    """Raised when every route is dead or cooling down and none could serve."""


def _key_source_candidates() -> List[str]:
    """
    Credential file paths scanned by from_default_routes, highest priority first.
    Delegates to KeyPool so the candidate list has a single source of truth;
    tests monkeypatch this function to stay environment-independent.
    """
    return KeyPool.default_key_file_candidates() + KeyPool.vault_key_note_candidates()


@dataclass
class ProviderRoute:
    """One prioritized inference backend plus its runtime health telemetry."""
    name: str
    provider: BaseInferenceProvider
    state: RouteState = RouteState.ACTIVE
    cooldown_until: float = 0.0
    consecutive_failures: int = 0
    total_requests: int = 0
    success_count: int = 0
    failure_count: int = 0
    last_error: Optional[str] = None

    def is_available(self, now: float) -> bool:
        if self.state == RouteState.DEAD:
            return False
        if self.state == RouteState.COOLDOWN:
            return now >= self.cooldown_until
        return True

    def mark_success(self) -> None:
        self.state = RouteState.ACTIVE
        self.consecutive_failures = 0
        self.success_count += 1

    def mark_cooldown(self, cooldown_s: float, reason: str) -> None:
        self.state = RouteState.COOLDOWN
        self.cooldown_until = time.time() + max(0.0, cooldown_s)
        self.last_error = f"COOLDOWN ({cooldown_s:.1f}s): {reason}"

    def mark_dead(self, reason: str) -> None:
        self.state = RouteState.DEAD
        self.last_error = f"DEAD: {reason}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state.value,
            "cooldown_remaining_s": round(max(0.0, self.cooldown_until - time.time()), 1),
            "consecutive_failures": self.consecutive_failures,
            "total_requests": self.total_requests,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "last_error": self.last_error,
        }


class PriorityFailoverProvider(BaseInferenceProvider):
    """
    Composite provider executing routes strictly in priority order.
    A route is skipped while cooling down or dead; classification of the
    inner failure decides whether the route cools down (transient, e.g. 429
    with Retry-After, inner key-pool exhaustion) or dies permanently
    (401 revoked, 402 quota exhausted, deterministic 4xx).
    """

    def __init__(
        self,
        routes: List[Tuple[str, BaseInferenceProvider]],
        default_cooldown_s: float = 60.0,
        cooldown_cap_s: float = 600.0,
        max_consecutive_failures: int = 3
    ):
        if not routes:
            raise ValueError("PriorityFailoverProvider needs at least one route")
        self.routes: List[ProviderRoute] = [
            ProviderRoute(name=name, provider=provider) for name, provider in routes
        ]
        self.default_cooldown_s = default_cooldown_s
        self.cooldown_cap_s = cooldown_cap_s
        self.max_consecutive_failures = max_consecutive_failures

    # ------------------------------------------------------------------
    # Classification helpers (inner providers wrap low-level httpx errors
    # into RuntimeError chains; walk __cause__ to recover status details)
    # ------------------------------------------------------------------

    @staticmethod
    def _find_http_status(exc: BaseException) -> Optional[int]:
        current: Optional[BaseException] = exc
        hops = 0
        while current is not None and hops < 8:
            if isinstance(current, httpx.HTTPStatusError):
                return current.response.status_code
            current = current.__cause__ or current.__context__
            hops += 1
        return None

    @staticmethod
    def _find_retry_after_s(exc: BaseException) -> Optional[float]:
        current: Optional[BaseException] = exc
        hops = 0
        while current is not None and hops < 8:
            if isinstance(current, httpx.HTTPStatusError):
                headers = current.response.headers
                # Standard seconds header first, then Nace Drex's millisecond variant
                # ("retry-after-ms"). A non-numeric standard header (HTTP-date) must not
                # abort the search: fall through to the next candidate.
                for header, scale in (("Retry-After", 1.0), ("Retry-After-Ms", 0.001)):
                    raw = headers.get(header)
                    if not raw:
                        continue
                    try:
                        return max(1.0, float(raw) * scale)
                    except ValueError:
                        continue
                return None
            current = current.__cause__ or current.__context__
            hops += 1
        return None

    @staticmethod
    def _find_http_response_text(exc: BaseException) -> str:
        current: Optional[BaseException] = exc
        hops = 0
        while current is not None and hops < 8:
            if isinstance(current, httpx.HTTPStatusError):
                try:
                    return current.response.text or ""
                except Exception:
                    return ""
            current = current.__cause__ or current.__context__
            hops += 1
        return ""

    def _classify_failure(self, route: ProviderRoute, exc: Exception) -> None:
        status = self._find_http_status(exc)
        retry_after = self._find_retry_after_s(exc)
        resp_text = self._find_http_response_text(exc)
        err_text = str(exc)
        err_summary = err_text.splitlines()[0][:160] if err_text else exc.__class__.__name__
        combined_text = f"{err_text} {resp_text}".lower()

        # Check for quota exhaustion signatures across HTTP status and error body
        is_quota_exhausted = (
            status == 402
            or any(term in combined_text for term in (
                "insufficient_user_quota",
                "quota_exhausted",
                "insufficient quota",
                "balance_exhausted",
                "wallet_empty",
                "out of quota",
            ))
        )

        if status == 401:
            # Surfaces only after the inner pool rotated through several revoked keys
            # (see TypeSafeJevProvider retry loop): pool-wide revocation, futile until
            # credentials are replaced.
            route.mark_dead("401 Unauthorized (credentials revoked)")
        elif is_quota_exhausted:
            # Wallet / included allowance exhausted: cannot succeed until reset or top-up.
            route.mark_dead("quota/wallet exhausted")
        elif status == 404:
            # Endpoint URL or model misconfiguration is request-independent.
            route.mark_dead("404 endpoint not found (base_url or model misconfigured)")
        elif status == 429:
            cooldown = min(retry_after if retry_after else self.default_cooldown_s, self.cooldown_cap_s)
            source = f"Retry-After {retry_after:.0f}s" if retry_after else "exponential backoff"
            route.mark_cooldown(cooldown, f"429 rate limited ({source})")
        elif status == 529:
            # Nace Drex "at capacity" (shared upstream pool full): transient, so pause
            # the route before retrying. Other 5xx (e.g. 503) stay on the generic
            # multi-strike transient path below instead of cooling down on first hit.
            cooldown = min(retry_after if retry_after else self.default_cooldown_s, self.cooldown_cap_s)
            source = f"Retry-After {retry_after:.0f}s" if retry_after else "exponential backoff"
            route.mark_cooldown(cooldown, f"529 upstream capacity ({source})")
        elif status in (400, 403, 422):
            # Key-level or payload-dependent rejections (e.g. one risk-controlled key
            # returning 403 out of a large pool, or a single payload failing semantic
            # validation). Other requests can still succeed, so cool down instead of
            # killing the route; repeated hits escalate the backoff.
            backoff = min(self.default_cooldown_s * route.consecutive_failures, self.cooldown_cap_s)
            route.mark_cooldown(backoff, f"HTTP {status} client error (key/payload level)")
        elif "exhausted all available keys" in err_summary:
            # Inner KeyPool temporarily empty: its cooldowns expire, so retry later.
            route.mark_cooldown(self.default_cooldown_s, "inner key pool exhausted")
        else:
            if route.consecutive_failures >= self.max_consecutive_failures:
                backoff = min(self.default_cooldown_s * route.consecutive_failures, self.cooldown_cap_s)
                route.mark_cooldown(backoff, f"transient failures: {err_summary}")
            else:
                route.last_error = err_summary

    # ------------------------------------------------------------------
    # BaseInferenceProvider
    # ------------------------------------------------------------------

    async def infer(
        self,
        payload: ObservationPayload,
        questions: List[QuestionSpec]
    ) -> List[DecisionVerdict]:
        now = time.time()
        failures: List[Dict[str, Any]] = []

        for route in self.routes:
            if not route.is_available(now):
                continue
            route.total_requests += 1
            try:
                verdicts = await route.provider.infer(payload, questions)
                route.mark_success()
                return verdicts
            except Exception as exc:
                route.failure_count += 1
                route.consecutive_failures += 1
                self._classify_failure(route, exc)
                failures.append({"route": route.name, "error": route.last_error or str(exc)[:160]})

        detail = "; ".join(f"{f['route']}: {f['error']}" for f in failures) or "all routes cooling down"
        raise NoAvailableProvidersError(
            f"All {len(self.routes)} failover routes unavailable: {detail}"
        )

    # ------------------------------------------------------------------
    # Ops surface
    # ------------------------------------------------------------------

    def stats(self) -> Dict[str, Any]:
        return {
            "total_routes": len(self.routes),
            "active_routes": sum(1 for r in self.routes if r.state == RouteState.ACTIVE),
            "cooldown_routes": sum(1 for r in self.routes if r.state == RouteState.COOLDOWN),
            "dead_routes": sum(1 for r in self.routes if r.state == RouteState.DEAD),
            "routes": [r.to_dict() for r in self.routes],
        }

    async def aclose(self) -> None:
        for route in self.routes:
            close = getattr(route.provider, "aclose", None)
            if close:
                await close()

    # ------------------------------------------------------------------
    # Builders
    # ------------------------------------------------------------------

    @classmethod
    def from_default_routes(
        cls,
        futureppo_base_url: str = DEFAULT_FUTUREPPO_BASE_URL,
        futureppo_model: str = DEFAULT_FUTUREPPO_MODEL,
        nace_base_url: str = "https://console.nace.ai/v1",
        nace_model: str = "drex-latest",
        mindshub_base_url: str = "https://api.mindshub.ai/v1/decisions",
        mindshub_model: str = "jev",
        typesafe_base_url: str = "https://api.typesafe.ai/v1",
        typesafe_model: str = "jev-latest",
        default_cooldown_s: float = 60.0,
    ) -> "PriorityFailoverProvider":
        """
        Builds the standard priority failover chain without touching
        DECIDEX_BASE_URL (which only applies to a standalone TypeSafeJevProvider):
          1. "futureppo-pool": FuturePPO gateway keys (FUTUREPPO_API_KEY /
             DECIDEX_FUTUREPPO_KEY env, or keys tagged org=futureppo in keys files),
             max_retries=0 so 429 or quota issues immediately surface and failover.
          2. "nace-decision": Nace Drex hosted API (NACE_API_KEY / DREX_API_KEY env,
             or keys tagged org=nace in keys files). Wire-compatible with System One.
          3. "mindshub-free": MindsHub gateway keys (MINDSHUB_API_KEY env, or
             keys tagged org=mindshub in keys files), max_retries=0.
          4. "typesafe-pool": every other key (TYPESAFE_API_KEY /
             DECIDEX_API_KEY env, untagged entries in keys files, the Obsidian
             typesafe API keys note), max_retries=2 for pool rotation.
        Routes without any key are skipped.
        """
        # Route order = priority. Each entry: (route name, key bucket, base_url, model, max_retries).
        # A bucket's keys come from its env vars (first set wins) or from key-file entries
        # whose org tag matches the bucket. Untagged / non-gateway-tagged keys -> "typesafe".
        route_specs = [
            ("futureppo-pool", "futureppo", futureppo_base_url, futureppo_model, 0),
            ("nace-decision", "nace", nace_base_url, nace_model, 0),
            ("mindshub-free", "mindshub", mindshub_base_url, mindshub_model, 0),
            ("typesafe-pool", "typesafe", typesafe_base_url, typesafe_model, 2),
        ]
        env_vars = {
            "futureppo": ("FUTUREPPO_API_KEY", "DECIDEX_FUTUREPPO_KEY"),
            "nace": ("NACE_API_KEY", "DREX_API_KEY"),
            "mindshub": ("MINDSHUB_API_KEY",),
            "typesafe": ("TYPESAFE_API_KEY", "DECIDEX_API_KEY"),
        }

        buckets: Dict[str, List[str]] = {bucket: [] for bucket in env_vars}
        for bucket, names in env_vars.items():
            for name in names:
                value = os.environ.get(name)
                if value:
                    buckets[bucket].extend(k.strip() for k in value.split(",") if k.strip())
                    break

        for path in _key_source_candidates():
            if not path or not os.path.isfile(path):
                continue
            try:
                pool = KeyPool.from_markdown(path) if path.endswith(".md") else KeyPool.from_file(path)
            except Exception:
                continue
            for entry in pool._entries:
                if entry.org in buckets:
                    buckets[entry.org].append(entry.key)
                elif entry.org not in GATEWAY_ORGS:
                    # Everything not bound to a known gateway belongs to the official
                    # pool: untagged keys, or official keys carrying an internal tag
                    # (e.g. a rollout name). Dropping unknown tags here would silently
                    # discard legitimate official credentials.
                    buckets["typesafe"].append(entry.key)

        def _dedup(keys: List[str]) -> List[str]:
            seen, out = set(), []
            for k in keys:
                if k and k not in seen:
                    seen.add(k)
                    out.append(k)
            return out

        routes: List[Tuple[str, BaseInferenceProvider]] = []
        for name, bucket, base_url, model, max_retries in route_specs:
            keys = _dedup(buckets[bucket])
            if not keys:
                continue
            routes.append((
                name,
                TypeSafeJevProvider(
                    api_keys=keys,
                    base_url=base_url,
                    model=model,
                    max_retries=max_retries
                )
            ))

        if not routes:
            raise NoAvailableProvidersError(
                "No Jev credentials found for any route. Set FUTUREPPO_API_KEY (free primary), "
                "NACE_API_KEY (Drex hosted), MINDSHUB_API_KEY (free gateway), or provide a "
                "TypeSafe key pool via DECIDEX_KEY_FILE / keys.jsonl."
            )

        return cls(routes, default_cooldown_s=default_cooldown_s)
