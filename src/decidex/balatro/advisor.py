"""
BalatroAdvisor: read-only "best move" recommender built on DecideX.

Same five-stage pipeline as the Hearthstone advisor:
  1. Evidence   -> build_evidence(state)
  2. Legal set  -> rules.enumerate_legal_actions (plays pre-scored by the
                   deterministic scoring engine)
  3. Model      -> DecisionEngine.step(..., legal_actions=...)
  4. Guard      -> CalibratedDecisionGuard (legality/confidence/margin)
  5. Fallback   -> rules.choose_safe_default

Read-only red line: this only *recommends*; it never actuates the game.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from decidex.engine import DecisionEngine
from decidex.guards import CalibratedDecisionGuard
from decidex.pruner import CandidatePruner
from decidex.providers.base import BaseInferenceProvider
from decidex.types import ActionCandidate, PrimitiveType, QuestionSpec, TurnPhase

from .rules import choose_safe_default, enumerate_legal_actions
from .state import BalatroState


def build_evidence(state: BalatroState, n_candidates: int) -> Dict[str, Any]:
    """Compact evidence dict handed to the model as domain_state."""
    return {
        "blind": {
            "name": state.blind.name, "type": state.blind.type,
            "target": state.blind.chips, "scored": state.blind.scored,
            "remaining": state.blind.remaining,
        },
        "resources": {
            "hands_left": state.hands_left,
            "discards_left": state.discards_left,
            "money": state.money,
            "ante": state.ante, "round": state.round,
        },
        "hand": [c.label + (f"[{c.enhancement}]" if c.enhancement else "")
                 + (f"<{c.edition}>" if c.edition else "") for c in state.hand],
        "jokers": [j.name for j in state.jokers],
        "consumables": [c.name for c in state.consumables],
        "n_legal_actions": n_candidates,
    }


@dataclass
class Recommendation:
    """One recommended action plus provenance."""
    candidate_id: str
    action_type: str
    description: str
    confidence: float
    expected_score: Optional[float] = None
    clears_blind: Optional[bool] = None
    is_fallback: bool = False
    fallback_reason: Optional[str] = None
    distribution: Dict[str, float] = field(default_factory=dict)
    legal_actions: List[Dict[str, Any]] = field(default_factory=list)


class BalatroAdvisor:
    """
    Turns a :class:`BalatroState` into one recommended legal action.

    Parameters mirror :class:`~decidex.hearthstone.advisor.HearthstoneAdvisor`:
    the provider decides among legal candidates; top_k pre-prunes the space;
    the guard falls back to the deterministic safe default below thresholds.
    """

    def __init__(
        self,
        provider: BaseInferenceProvider,
        top_k: int = 10,
        min_confidence: float = 0.55,
        min_margin: float = 0.10,
        guard_temperature: float = 1.25,
        journal_path: Optional[str] = None,
    ):
        self.provider = provider
        self.top_k = top_k
        self.min_confidence = min_confidence
        self.min_margin = min_margin

        guard = CalibratedDecisionGuard(
            temperature=guard_temperature,
            min_confidence=min_confidence,
            min_margin=min_margin,
            enforce_legal=True,
            anti_oscillation=False,
            max_consecutive_action=0,
        )
        self._engine = DecisionEngine(provider=provider, guard=guard, journal_path=journal_path)

    async def recommend(
        self,
        state: BalatroState,
        legal_actions: Optional[List[ActionCandidate]] = None,
    ) -> Recommendation:
        candidates = legal_actions if legal_actions is not None else enumerate_legal_actions(state)
        top = CandidatePruner.prune_to_top_k(candidates, k=self.top_k)
        option_ids, lookup, descriptions = CandidatePruner.to_choice_bundle(top)

        question = QuestionSpec(
            id="balatro_action",
            primitive=PrimitiveType.CHOICE,
            description=(
                "Choose the best legal move: play the highest-scoring poker hand, "
                "discard for a stronger draw, or use a consumable first. Clear the "
                "blind while conserving hands and discards for later antes."
            ),
            options=option_ids,
            criteria=descriptions,
        )

        verdicts = await self._engine.step(
            domain_state=build_evidence(state, len(top)),
            questions=[question],
            legal_actions=option_ids,
            phase=TurnPhase.MAIN,
        )
        verdict = verdicts["balatro_action"]

        if verdict.is_fallback:
            safe = choose_safe_default(top, state)
            return self._from_candidate(safe, verdict.calibrated_confidence, verdict,
                                        top, reason=verdict.fallback_reason)
        selected = lookup.get(str(verdict.selected))
        if selected is None:
            safe = choose_safe_default(top, state)
            return self._from_candidate(safe, verdict.calibrated_confidence, verdict,
                                        top, reason="SELECTION_NOT_IN_ENVELOPE")
        return self._from_candidate(selected, verdict.calibrated_confidence, verdict, top)

    def _from_candidate(
        self,
        candidate: ActionCandidate,
        confidence: float,
        verdict: Any,
        legal: List[ActionCandidate],
        reason: Optional[str] = None,
    ) -> Recommendation:
        return Recommendation(
            candidate_id=candidate.candidate_id,
            action_type=candidate.metadata.get("action_type", "unknown"),
            description=candidate.description,
            confidence=confidence,
            expected_score=candidate.metadata.get("score"),
            clears_blind=candidate.metadata.get("clears"),
            is_fallback=reason is not None,
            fallback_reason=reason,
            distribution=dict(getattr(verdict, "distribution", {})),
            legal_actions=[
                {"id": c.candidate_id, "action_type": c.metadata.get("action_type"),
                 "description": c.description}
                for c in legal
            ],
        )

    def close(self) -> None:
        self._engine.close()
