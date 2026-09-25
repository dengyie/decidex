"""
TypeSafe Jev cloud API provider implementation with KeyPool rotation,
automatic 429 cooldowns, 401 dead key isolation, and official System One payload support.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Dict, List, Optional, Union
import httpx

from decidex.pool import KeyEntry, KeyPool, KeyState, NoAvailableKeysError, RotationStrategy
from decidex.providers.base import BaseInferenceProvider
from decidex.types import DecisionVerdict, ObservationPayload, PrimitiveType, QuestionSpec


def _safe_float(val: Any, default: float = 1.0) -> float:
    """Safely converts val to float, with fallback to default on None or parsing errors."""
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


class TypeSafeJevProvider(BaseInferenceProvider):
    """
    HTTP client for the TypeSafe Jev cloud decision API (System One).
    Supports single API keys or rotating across a large KeyPool (e.g. 1000+ accounts).
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        key_pool: Optional[KeyPool] = None,
        api_keys: Optional[List[str]] = None,
        base_url: Optional[str] = None,
        model: str = "jev-latest",
        timeout_s: float = 2.0,
        max_retries: int = 3,
        auto_rotate_on_rate_limit: bool = True,
        auto_quarantine_on_unauthorized: bool = True
    ):
        # Explicit base_url wins; otherwise DECIDEX_BASE_URL (e.g. MindsHub free gateway
        # https://api.mindshub.ai/v1/decisions); otherwise official TypeSafe API.
        resolved_base = base_url or os.environ.get("DECIDEX_BASE_URL") or "https://api.typesafe.ai/v1"
        self.base_url = resolved_base.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.auto_rotate_on_rate_limit = auto_rotate_on_rate_limit
        self.auto_quarantine_on_unauthorized = auto_quarantine_on_unauthorized
        self._client: Optional[httpx.AsyncClient] = None

        # Initialize KeyPool
        if key_pool is not None:
            self.key_pool = key_pool
        elif api_keys is not None and len(api_keys) > 0:
            self.key_pool = KeyPool.from_keys(api_keys)
        elif api_key is not None:
            self.key_pool = KeyPool.from_keys([api_key])
        else:
            try:
                self.key_pool = KeyPool.from_default_locations()
            except Exception:
                self.key_pool = None

    @property
    def single_key(self) -> Optional[str]:
        if self.key_pool and len(self.key_pool) == 1:
            return self.key_pool._entries[0].key
        return None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            limits = httpx.Limits(max_keepalive_connections=20, max_connections=50, keepalive_expiry=60.0)
            self._client = httpx.AsyncClient(timeout=self.timeout_s, limits=limits)
        return self._client

    async def aclose(self) -> None:
        """Closes the underlying HTTP connection pool."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def warmup(self) -> bool:
        """
        Pre-warms the HTTP connection pool (DNS resolution, TCP/TLS handshake)
        to eliminate the cold-start penalty on the first inference call.
        """
        client = self._get_client()
        try:
            target = f"{self.base_url.rsplit('/v1', 1)[0]}/openapi.json"
            resp = await client.get(target, timeout=3.0)
            return resp.status_code == 200
        except Exception:
            return False

    async def __aenter__(self) -> TypeSafeJevProvider:
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.aclose()

    def _format_request_body(
        self,
        payload: ObservationPayload,
        questions: List[QuestionSpec]
    ) -> Dict[str, Any]:
        """
        Formats state and questions adhering to official TypeSafe Jev System One spec,
        while maintaining compatibility with custom or proxy endpoints.
        """
        questions_dict: Dict[str, Any] = {}
        for q in questions:
            q_item: Dict[str, Any] = {
                "type": q.primitive.value,
                "instructions": q.description,
            }
            if q.primitive == PrimitiveType.CHOICE and q.options:
                if q.criteria and isinstance(q.criteria, dict):
                    q_item["criteria"] = q.criteria
                else:
                    q_item["criteria"] = {opt: opt for opt in q.options}
            elif q.primitive == PrimitiveType.SCORE:
                if q.criteria and isinstance(q.criteria, dict):
                    q_item["criteria"] = q.criteria
                elif q.criteria and isinstance(q.criteria, list):
                    q_item["criteria"] = [str(x) for x in q.criteria]
                elif q.scale:
                    q_item["criteria"] = [f"Rating level {x}" for x in q.scale]
                else:
                    q_item["criteria"] = ["Low", "Medium", "High"]
            questions_dict[q.id] = q_item

        state_dict: Dict[str, Any] = {
            "observation": payload.observation
        }
        if payload.memory:
            state_dict["memory"] = payload.memory
        if payload.phase:
            state_dict["phase"] = payload.phase.value
        if payload.metadata:
            state_dict["metadata"] = payload.metadata

        return {
            "model": self.model,
            "state": state_dict,
            "questions": questions_dict
        }

    def _parse_response(
        self,
        data: Dict[str, Any],
        questions: List[QuestionSpec],
        elapsed_ms: float
    ) -> List[DecisionVerdict]:
        """
        Parses verdicts from either official System One 'answers' or DecideX 'results'.
        """
        verdicts: List[DecisionVerdict] = []
        latency_per_q = elapsed_ms / max(1, len(questions))

        # Shape 1: Official System One format: {"answers": {"head_id": {...}}}
        if "answers" in data and isinstance(data["answers"], dict):
            answers_map = data["answers"]
            for q in questions:
                if q.id not in answers_map:
                    continue
                ans = answers_map[q.id]
                if not isinstance(ans, dict):
                    continue

                if q.primitive == PrimitiveType.CHOICE:
                    selected = ans.get("choice")
                    if selected is None:
                        continue
                    dist = ans.get("probabilities", {})
                    # TypeSafe Jev returns margin/entropy in ans["confidence"],
                    # whereas dist[selected] represents the actual class posterior probability.
                    if isinstance(dist, dict) and selected in dist:
                        raw_conf = _safe_float(dist[selected], default=1.0)
                    else:
                        raw_conf = _safe_float(ans.get("confidence"), default=1.0)
                elif q.primitive == PrimitiveType.NOUL:
                    noul_raw = ans.get("noul", 0.5)
                    try:
                        noul_val = float(noul_raw)
                    except (ValueError, TypeError):
                        noul_val = 1.0 if str(noul_raw).lower() in ("true", "1") else 0.0
                    selected = noul_val >= 0.5
                    raw_conf = noul_val if selected else (1.0 - noul_val)
                    dist = {"true": noul_val, "false": round(1.0 - noul_val, 4)}
                elif q.primitive == PrimitiveType.SCORE:
                    selected = ans.get("score")
                    if selected is None:
                        continue
                    dist = ans.get("probabilities", {})
                    if isinstance(dist, dict) and str(selected) in dist:
                        raw_conf = _safe_float(dist[str(selected)], default=1.0)
                    elif isinstance(dist, dict) and selected in dist:
                        raw_conf = _safe_float(dist[selected], default=1.0)
                    else:
                        raw_conf = _safe_float(ans.get("confidence"), default=1.0)
                else:
                    continue

                verdicts.append(DecisionVerdict(
                    id=q.id,
                    primitive=q.primitive,
                    selected=selected,
                    raw_confidence=raw_conf,
                    calibrated_confidence=raw_conf,
                    distribution=dist,
                    latency_ms=latency_per_q,
                    is_fallback=False
                ))
            return verdicts

        # Shape 2: DecideX proxy format: {"results": [{"id": ..., "selected": ...}]}
        if "results" in data and isinstance(data["results"], list):
            results_map = {item["id"]: item for item in data["results"] if isinstance(item, dict) and "id" in item}
            for q in questions:
                if q.id not in results_map:
                    continue
                item = results_map[q.id]
                selected = item.get("selected")
                if selected is None:
                    continue
                dist = item.get("distribution", {})
                if isinstance(dist, dict) and selected in dist:
                    raw_conf = _safe_float(dist[selected], default=1.0)
                else:
                    raw_conf = _safe_float(item.get("confidence"), default=1.0)

                verdicts.append(DecisionVerdict(
                    id=q.id,
                    primitive=q.primitive,
                    selected=selected,
                    raw_confidence=raw_conf,
                    calibrated_confidence=raw_conf,
                    distribution=dist,
                    latency_ms=latency_per_q,
                    is_fallback=False
                ))
            return verdicts

        return verdicts

    async def infer(
        self,
        payload: ObservationPayload,
        questions: List[QuestionSpec]
    ) -> List[DecisionVerdict]:
        # Determine endpoint URL (explicit full-path bases pass through untouched)
        if self.base_url.endswith(("/systemone", "/decide", "/decisions")):
            url = self.base_url
        else:
            url = f"{self.base_url}/systemone"

        req_body = self._format_request_body(payload, questions)
        client = self._get_client()

        start_time = time.perf_counter()
        last_exception: Optional[Exception] = None

        for attempt in range(self.max_retries + 1):
            # Select key from pool or static
            current_entry: Optional[KeyEntry] = None
            if self.key_pool is not None:
                try:
                    current_entry = await self.key_pool.get_next_key()
                    current_key = current_entry.key
                except NoAvailableKeysError as e:
                    raise RuntimeError(f"TypeSafeJevProvider exhausted all available keys: {e}") from e
            else:
                current_key = None

            headers = {
                "Content-Type": "application/json",
                "User-Agent": "DecideX-Client/0.1.0"
            }
            if current_key:
                headers["Authorization"] = f"Bearer {current_key}"

            try:
                resp = await client.post(url, json=req_body, headers=headers)

                # 401 Unauthorized: dead/revoked key
                if resp.status_code == 401:
                    if self.key_pool and current_entry and self.auto_quarantine_on_unauthorized:
                        self.key_pool.record_revoked(current_entry.key, reason="401 Unauthorized")
                        if attempt < self.max_retries:
                            continue
                    resp.raise_for_status()

                # 429 Too Many Requests: rate limit cooldown with Retry-After header parsing
                if resp.status_code == 429:
                    if self.key_pool and current_entry and self.auto_rotate_on_rate_limit:
                        retry_after_str = resp.headers.get("Retry-After")
                        cooldown_s = None
                        if retry_after_str:
                            try:
                                cooldown_s = float(retry_after_str)
                            except ValueError:
                                pass
                        self.key_pool.record_rate_limit(current_entry.key, cooldown_s=cooldown_s, reason="429 Too Many Requests")
                        if attempt < self.max_retries:
                            continue
                    resp.raise_for_status()

                # 5xx Server Errors: record transient server failure against key
                if resp.status_code >= 500:
                    if self.key_pool and current_entry:
                        self.key_pool.record_failure(current_entry.key, error=f"HTTP {resp.status_code}", is_transient=True)
                    resp.raise_for_status()

                # 400, 403, 404, 422: client-side deterministic errors (do not retry key)
                if resp.status_code in (400, 403, 404, 422):
                    if self.key_pool and current_entry:
                        self.key_pool.record_failure(current_entry.key, error=f"HTTP {resp.status_code}", is_transient=False)
                    resp.raise_for_status()

                resp.raise_for_status()
                try:
                    data = resp.json()
                except Exception as json_err:
                    if self.key_pool and current_entry:
                        self.key_pool.record_failure(current_entry.key, error=f"Invalid JSON: {json_err}", is_transient=True)
                    if attempt < self.max_retries:
                        await asyncio.sleep(0.05 * (2 ** attempt))
                        continue
                    raise

                elapsed_ms = (time.perf_counter() - start_time) * 1000.0

                if self.key_pool and current_entry:
                    self.key_pool.record_success(current_entry.key, latency_ms=elapsed_ms)

                return self._parse_response(data, questions, elapsed_ms)

            except httpx.HTTPStatusError as exc:
                last_exception = exc
                if exc.response.status_code in (400, 403, 404, 422):
                    # Deterministic failure, break loop
                    break
                if attempt < self.max_retries:
                    await asyncio.sleep(0.05 * (2 ** attempt))
                    continue
            except (httpx.RequestError, httpx.TimeoutException) as exc:
                last_exception = exc
                if self.key_pool and current_entry:
                    self.key_pool.record_failure(current_entry.key, error=str(exc), is_transient=True)
                if attempt < self.max_retries:
                    await asyncio.sleep(0.05 * (2 ** attempt))
                    continue

        raise RuntimeError(
            f"TypeSafeJevProvider request failed after {self.max_retries + 1} attempts: {last_exception}"
        ) from last_exception
