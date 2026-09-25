"""
Priority-ordered failover orchestrator across inference providers.

Routes every infer() call to the highest-priority healthy backend and
transparently degrades to lower-priority backends when the primary is
rate-limited (429), quota/wallet-exhausted (402), revoked (401) or
deterministically misconfigured (400/403/404/422).

Typical wiring: MindsHub free Jev gateway first (consume the free
promotion quota), official TypeSafe key pool as the durable fallback.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import httpx

from decidex.pool import KeyPool
from decidex.providers.base import BaseInferenceProvider
from decidex.providers.typesafe import TypeSafeJevProvider
from decidex.types import DecisionVerdict, ObservationPayload, QuestionSpec


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
                raw = current.response.headers.get("Retry-After")
                if raw:
                    try:
                        return max(1.0, float(raw))
                    except ValueError:
                        return None
            current = current.__cause__ or current.__context__
            hops += 1
        return None

    def _classify_failure(self, route: ProviderRoute, exc: Exception) -> None:
        status = self._find_http_status(exc)
        retry_after = self._find_retry_after_s(exc)
        err_summary = str(exc).splitlines()[0][:160] if str(exc) else exc.__class__.__name__

        if status == 401:
            # Surfaces only after the inner pool rotated through several revoked keys
            # (see TypeSafeJevProvider retry loop): pool-wide revocation, futile until
            # credentials are replaced.
            route.mark_dead("401 Unauthorized (credentials revoked)")
        elif status == 402:
            # Wallet / included allowance exhausted: cannot succeed until reset or top-up.
            route.mark_dead("402 quota/wallet exhausted")
        elif status == 404:
            # Endpoint URL misconfiguration is request-independent.
            route.mark_dead("404 endpoint not found (base_url misconfigured)")
        elif status == 429:
            cooldown = min(retry_after if retry_after else self.default_cooldown_s, self.cooldown_cap_s)
            source = f"Retry-After {retry_after:.0f}s" if retry_after else "exponential backoff"
            route.mark_cooldown(cooldown, f"429 rate limited ({source})")
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
        mindshub_base_url: str = "https://api.mindshub.ai/v1/decisions",
        mindshub_model: str = "jev",
        typesafe_base_url: str = "https://api.typesafe.ai/v1",
        typesafe_model: str = "jev-latest",
        default_cooldown_s: float = 60.0
    ) -> "PriorityFailoverProvider":
        """
        Builds the standard two-route setup without touching DECIDEX_BASE_URL
        (which only applies to a standalone TypeSafeJevProvider):
          1. "mindshub-free": MindsHub gateway keys (MINDSHUB_API_KEY env, or
             keys tagged org=mindshub in keys files), max_retries=0 so 429
             surfaces to this layer immediately.
          2. "typesafe-pool": every other key (TYPESAFE_API_KEY /
             DECIDEX_API_KEY env, untagged entries in keys files, the Obsidian
             typesafe API keys note).
        Routes without any key are skipped.
        """
        mindshub_keys: List[str] = []
        typesafe_keys: List[str] = []

        env_mindshub = os.environ.get("MINDSHUB_API_KEY")
        if env_mindshub:
            mindshub_keys.extend(k.strip() for k in env_mindshub.split(",") if k.strip())

        env_typesafe = os.environ.get("TYPESAFE_API_KEY") or os.environ.get("DECIDEX_API_KEY")
        if env_typesafe:
            typesafe_keys.extend(k.strip() for k in env_typesafe.split(",") if k.strip())

        for path in _key_source_candidates():
            if not path or not os.path.isfile(path):
                continue
            try:
                pool = KeyPool.from_markdown(path) if path.endswith(".md") else KeyPool.from_file(path)
            except Exception:
                continue
            for entry in pool._entries:
                if entry.org == "mindshub":
                    mindshub_keys.append(entry.key)
                else:
                    typesafe_keys.append(entry.key)

        def _dedup(keys: List[str]) -> List[str]:
            seen, out = set(), []
            for k in keys:
                if k and k not in seen:
                    seen.add(k)
                    out.append(k)
            return out

        routes: List[Tuple[str, BaseInferenceProvider]] = []
        if _dedup(mindshub_keys):
            routes.append((
                "mindshub-free",
                TypeSafeJevProvider(
                    api_keys=_dedup(mindshub_keys),
                    base_url=mindshub_base_url,
                    model=mindshub_model,
                    max_retries=0
                )
            ))
        if _dedup(typesafe_keys):
            routes.append((
                "typesafe-pool",
                TypeSafeJevProvider(
                    api_keys=_dedup(typesafe_keys),
                    base_url=typesafe_base_url,
                    model=typesafe_model,
                    max_retries=2
                )
            ))

        if not routes:
            raise NoAvailableProvidersError(
                "No Jev credentials found for any route. Set MINDSHUB_API_KEY (free gateway) "
                "or provide a TypeSafe key pool via DECIDEX_KEY_FILE / keys.jsonl."
            )

        return cls(routes, default_cooldown_s=default_cooldown_s)
