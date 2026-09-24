"""
Abstract base class and shared contracts for inference providers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List

from decidex.types import DecisionVerdict, ObservationPayload, QuestionSpec


class BaseInferenceProvider(ABC):
    """
    Abstract interface for System One structured inference backends
    (e.g., TypeSafe Jev API, local NanoJev ONNX/vLLM, or MockReplayProvider).
    """

    @abstractmethod
    async def infer(
        self,
        payload: ObservationPayload,
        questions: List[QuestionSpec]
    ) -> List[DecisionVerdict]:
        """
        Executes parallel multi-head inference over the given questions
        conditioned on the observation payload.
        """
        pass
