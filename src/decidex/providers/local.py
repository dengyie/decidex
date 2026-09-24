"""
Local / Self-hosted NanoJev inference provider supporting local HTTP (vLLM/Ollama)
and direct Python in-memory callable/ONNX execution.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional
import httpx

from decidex.providers.base import BaseInferenceProvider
from decidex.types import DecisionVerdict, ObservationPayload, QuestionSpec


class LocalNanoJevProvider(BaseInferenceProvider):
    """
    Inference provider for locally hosted lightweight decision models (NanoJev, SLMs).
    Supports either direct in-memory callable execution or a local HTTP endpoint with connection pooling.
    """

    def __init__(
        self,
        endpoint_url: Optional[str] = "http://localhost:8000/v1/decide",
        in_memory_runner: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
        timeout_s: float = 0.5
    ):
        self.endpoint_url = endpoint_url
        self.in_memory_runner = in_memory_runner
        self.timeout_s = timeout_s
        self._client: Optional[httpx.AsyncClient] = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout_s)
        return self._client

    async def aclose(self) -> None:
        """Closes the underlying HTTP connection pool."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> LocalNanoJevProvider:
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.aclose()

    async def infer(
        self,
        payload: ObservationPayload,
        questions: List[QuestionSpec]
    ) -> List[DecisionVerdict]:
        start_time = time.perf_counter()

        req_body = {
            "observation": payload.observation,
            "memory": payload.memory,
            "phase": payload.phase.value if payload.phase else None,
            "metadata": payload.metadata,
            "questions": [
                {
                    "id": q.id,
                    "primitive": q.primitive.value,
                    "description": q.description,
                    "options": q.options,
                    "scale": list(q.scale)
                }
                for q in questions
            ]
        }

        # 1. In-memory execution if runner is provided
        if self.in_memory_runner is not None:
            data = self.in_memory_runner(req_body)
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            return self._parse_results(data, questions, elapsed_ms)

        # 2. Local HTTP request with pooled client
        if not self.endpoint_url:
            raise ValueError("Neither endpoint_url nor in_memory_runner was provided to LocalNanoJevProvider")

        client = self._get_client()
        resp = await client.post(self.endpoint_url, json=req_body)
        resp.raise_for_status()
        data = resp.json()

        elapsed_ms = (time.perf_counter() - start_time) * 1000.0
        return self._parse_results(data, questions, elapsed_ms)

    def _parse_results(
        self,
        data: Dict[str, Any],
        questions: List[QuestionSpec],
        elapsed_ms: float
    ) -> List[DecisionVerdict]:
        results_map = {item["id"]: item for item in data.get("results", []) if "id" in item}
        verdicts: List[DecisionVerdict] = []

        for q in questions:
            if q.id not in results_map:
                # Omit missing heads so DecisionEngine handles them with proper fallback
                continue

            item = results_map[q.id]
            selected = item.get("selected")
            if selected is None:
                continue

            dist = item.get("distribution", {})
            raw_conf = float(item.get("confidence") or 1.0)

            verdicts.append(
                DecisionVerdict(
                    id=q.id,
                    primitive=q.primitive,
                    selected=selected,
                    raw_confidence=raw_conf,
                    calibrated_confidence=raw_conf,
                    distribution=dist,
                    latency_ms=elapsed_ms / max(1, len(questions)),
                    is_fallback=False
                )
            )

        return verdicts
