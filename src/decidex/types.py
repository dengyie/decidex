"""
Core data types and contracts for the DecideX decision engine.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Union
from pydantic import BaseModel, ConfigDict, Field


class PrimitiveType(str, Enum):
    """
    Primitive decision output types supported by System One models (Jev/NanoJev).
    """
    CHOICE = "choice"  # Multi-class categorical decision among candidate options
    NOUL = "noul"      # Binary boolean truth assessment (returns probability of true: 0.0~1.0)
    SCORE = "score"    # Continuous metric score evaluation within specified scale range


class TurnPhase(str, Enum):
    """
    Phases within a turn-based lifecycle.
    """
    SETUP = "setup"
    MULLIGAN = "mulligan"
    MAIN = "main"
    COMBAT = "combat"
    SETTLEMENT = "settlement"
    SHOP = "shop"
    END_TURN = "end_turn"


class QuestionSpec(BaseModel):
    """
    Specification of a single decision question to be evaluated by the model.
    """
    model_config = ConfigDict(frozen=True)

    id: str = Field(description="Unique identifier for this decision head within the step")
    primitive: PrimitiveType = Field(description="Choice, Noul, or Score primitive type")
    description: str = Field(description="Natural language instruction guiding the decision")
    options: Optional[List[str]] = Field(
        default=None,
        description="Available categorical options (required for CHOICE primitive)"
    )
    criteria: Optional[Dict[str, str]] = Field(
        default=None,
        description="Optional detailed criteria mapping each option to its description"
    )
    scale: Tuple[float, float] = Field(
        default=(0.0, 1.0),
        description="Numerical bounds range [min, max] (used for SCORE primitive)"
    )

    def validate_spec(self) -> None:
        if self.primitive == PrimitiveType.CHOICE:
            if not self.options or len(self.options) < 2:
                raise ValueError(f"CHOICE question '{self.id}' requires at least 2 options, got {self.options}")
        elif self.primitive == PrimitiveType.SCORE:
            if self.scale[0] >= self.scale[1]:
                raise ValueError(f"SCORE question '{self.id}' scale min must be < max, got {self.scale}")


class DecisionVerdict(BaseModel):
    """
    The verdict and calibrated evaluation output for a single QuestionSpec.
    """
    id: str = Field(description="Matching QuestionSpec id")
    primitive: PrimitiveType = Field(description="Primitive type of this decision")
    selected: Union[str, bool, float, int] = Field(
        description="The winning option name (Choice), boolean decision (Noul), or score (Score)"
    )
    raw_confidence: float = Field(
        default=1.0,
        description="Raw confidence / probability of the selected answer as returned by the model"
    )
    calibrated_confidence: float = Field(
        default=1.0,
        description="Calibrated confidence after temperature scaling and margin auditing"
    )
    distribution: Dict[str, float] = Field(
        default_factory=dict,
        description="Full probability distribution over candidate answers"
    )
    latency_ms: float = Field(
        default=0.0,
        description="End-to-end evaluation latency in milliseconds"
    )
    is_fallback: bool = Field(
        default=False,
        description="True if safety guardrails or circuit breaker substituted a fallback action"
    )
    fallback_reason: Optional[str] = Field(
        default=None,
        description="Audit failure reason if fallback was triggered"
    )


class ActionCandidate(BaseModel):
    """
    High-level candidate action representation for combinatorial pruning.
    Used in card games (e.g. Balatro, Hearthstone) and discrete grid games (2048).
    """
    candidate_id: str = Field(description="Unique identifier of this candidate action")
    description: str = Field(description="Human/model readable description summarizing the candidate")
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Domain-specific execution parameters (e.g. card indices, coordinates)"
    )
    heuristic_score: float = Field(
        default=0.0,
        description="Rule-engine calculated score for heuristic pre-ranking and pruning"
    )


class ObservationPayload(BaseModel):
    """
    Structured model-facing observation payload.
    """
    observation: Any = Field(description="Domain-specific structured observation (dict, list, or primitive)")
    memory: Dict[str, Any] = Field(default_factory=dict, description="Injected short-term memory")
    phase: Optional[TurnPhase] = Field(default=None, description="Optional turn phase")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Auxiliary telemetry metadata")
