"""
Mock and replay inference provider for deterministic testing, offline benchmarks, and CI/CD.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from decidex.providers.base import BaseInferenceProvider
from decidex.types import DecisionVerdict, ObservationPayload, PrimitiveType, QuestionSpec


class MockReplayProvider(BaseInferenceProvider):
    """
    Deterministic mock provider allowing exact answers, simulated latency,
    and programmatic response solvers for testing and dry runs.
    """

    def __init__(
        self,
        preset_verdicts: Optional[Dict[str, Any]] = None,
        solver_fn: Optional[
            Callable[[ObservationPayload, QuestionSpec], Tuple[Any, float, Dict[str, float]]]
        ] = None,
        simulated_latency_ms: float = 5.0,
        inject_error: bool = False
    ):
        self.preset_verdicts = preset_verdicts or {}
        self.solver_fn = solver_fn
        self.simulated_latency_ms = simulated_latency_ms
        self.inject_error = inject_error
        self.call_history: List[Tuple[ObservationPayload, List[QuestionSpec]]] = []

    async def infer(
        self,
        payload: ObservationPayload,
        questions: List[QuestionSpec]
    ) -> List[DecisionVerdict]:
        self.call_history.append((payload, questions))

        if self.inject_error:
            raise RuntimeError("MockReplayProvider simulated upstream failure")

        if self.simulated_latency_ms > 0:
            await asyncio.sleep(self.simulated_latency_ms / 1000.0)

        verdicts: List[DecisionVerdict] = []
        for q in questions:
            # 1. Custom solver function if provided
            if self.solver_fn is not None:
                selected, conf, dist = self.solver_fn(payload, q)
                verdicts.append(
                    DecisionVerdict(
                        id=q.id,
                        primitive=q.primitive,
                        selected=selected,
                        raw_confidence=conf,
                        calibrated_confidence=conf,
                        distribution=dist,
                        latency_ms=self.simulated_latency_ms,
                        is_fallback=False
                    )
                )
                continue

            # 2. Preset verdicts mapping
            if q.id in self.preset_verdicts:
                val = self.preset_verdicts[q.id]
                if isinstance(val, dict) and "selected" in val:
                    selected = val["selected"]
                    conf = float(val.get("confidence", 0.95))
                    dist = val.get("distribution", {str(selected): conf})
                else:
                    selected = val
                    conf = 0.95
                    dist = {str(selected): conf}
            else:
                # 3. Default fallback based on primitive
                if q.primitive == PrimitiveType.CHOICE and q.options:
                    selected = q.options[0]
                    conf = 0.90
                    dist = {opt: (0.90 if opt == selected else 0.10 / max(1, len(q.options) - 1)) for opt in q.options}
                elif q.primitive == PrimitiveType.NOUL:
                    selected = True
                    conf = 0.85
                    dist = {"true": 0.85, "false": 0.15}
                else:
                    selected = (q.scale[0] + q.scale[1]) / 2.0
                    conf = 0.90
                    dist = {}

            verdicts.append(
                DecisionVerdict(
                    id=q.id,
                    primitive=q.primitive,
                    selected=selected,
                    raw_confidence=conf,
                    calibrated_confidence=conf,
                    distribution=dist,
                    latency_ms=self.simulated_latency_ms,
                    is_fallback=False
                )
            )

        return verdicts
