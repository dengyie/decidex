"""
HearthstoneAdvisor: read-only "best play" recommender built on DecideX.

Pipeline (maps 1:1 onto DecideX's five stages):
  1. Evidence Assembly   -> board.build_evidence(state)
  2. Legal enumeration   -> rules.enumerate_legal_actions(state)  [deterministic]
  3. Model judgment      -> DecisionEngine.step(..., legal_actions=...)
  4. Safety gating       -> CalibratedDecisionGuard (legality/confidence/oscillation)
  5. Fallback            -> rules.choose_safe_default / first-legal play

The advisor never actuates the client. It returns a recommendation plus the
reason, confidence, and the full legal-action envelope for transparency.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from decidex.engine import DecisionEngine
from decidex.guards import CalibratedDecisionGuard
from decidex.pruner import CandidatePruner
from decidex.providers.base import BaseInferenceProvider
from decidex.types import ActionCandidate, PrimitiveType, QuestionSpec, TurnPhase

from .board import BoardState, build_evidence
from .rules import (
    choose_safe_default,
    enumerate_legal_actions,
    legal_action_ids,
)


@dataclass
class Recommendation:
    """One recommended action plus provenance."""
    candidate_id: str
    action_type: str
    description: str
    confidence: float
    is_fallback: bool = False
    fallback_reason: Optional[str] = None
    distribution: Dict[str, float] = field(default_factory=dict)
    legal_actions: List[Dict[str, Any]] = field(default_factory=list)
    # Evidence dict + question spec that produced this verdict, carried through
    # so a viewer can render tokens / lint / bars without re-deriving them.
    # Purely observational — nothing reads these back.
    evidence: Dict[str, Any] = field(default_factory=dict)
    question: Dict[str, Any] = field(default_factory=dict)
    primitive: str = PrimitiveType.CHOICE.value


class HearthstoneAdvisor:
    """
    Turns a BoardState into a single recommended legal action via DecideX.

    Parameters
    ----------
    provider: decides the action among legal candidates (Jev, NanoJev, mock, ...).
    top_k: pre-prune the legal-action space to the top-K heuristic candidates
        before the model sees it (keeps probability mass from diluting).
    min_confidence / min_margin: guard thresholds; below them the advisor falls
        back to a deterministic safe play instead of a low-confidence guess.
    """

    def __init__(
        self,
        provider: BaseInferenceProvider,
        top_k: int = 8,
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
            anti_oscillation=False,  # a static board has no temporal loop to police
            max_consecutive_action=0,
        )
        self._engine = DecisionEngine(
            provider=provider,
            guard=guard,
            journal_path=journal_path,
        )

    async def recommend(
        self,
        state: BoardState,
        legal_actions: Optional[List[ActionCandidate]] = None,
    ) -> Recommendation:
        """
        Recommend a play for the given board.

        Args:
            state: The observed board state.
            legal_actions: Optional authoritative legal-action list. When provided
                (e.g. reconstructed from the live game's Options packet), it *replaces*
                the built-in rules enumerator, so the model is pinned to exactly what
                the game engine offers. When None, ``enumerate_legal_actions`` is used.
        """
        # Stage 1-2: deterministic legal-action envelope.
        candidates = legal_actions if legal_actions is not None else enumerate_legal_actions(state)
        top_candidates = CandidatePruner.prune_to_top_k(candidates, k=self.top_k)
        option_ids, lookup, descriptions = CandidatePruner.to_choice_bundle(top_candidates)

        # Build the single decision head over the legal candidates.
        question = QuestionSpec(
            id="hearthstone_action",
            primitive=PrimitiveType.CHOICE,
            description=(
                "Choose the strongest legal play this turn to maximize board control, "
                "tempo and lethal threat while avoiding wasted trades."
            ),
            options=option_ids,
            criteria=descriptions,
        )

        evidence = build_evidence(state, len(top_candidates))
        spec = {
            "id": question.id,
            "primitive": question.primitive.value,
            "description": question.description,
            "options": option_ids,
            "criteria": descriptions,
        }

        # Stage 3-4: model judgment + gating. legal_actions pins the model to
        # the deterministic legal set, so an illegal pick is rejected -> fallback.
        verdicts = await self._engine.step(
            domain_state=evidence,
            questions=[question],
            legal_actions=option_ids,
            phase=TurnPhase.MAIN,
        )
        verdict = verdicts["hearthstone_action"]

        # Stage 5: transparent recommendation (with fallback provenance).
        if verdict.is_fallback:
            safe = choose_safe_default(top_candidates)
            return self._recommendation_from_candidate(
                safe, verdict.calibrated_confidence, verdicts_verdict=verdict,
                legal=top_candidates, reason=verdict.fallback_reason,
                evidence=evidence, question=spec,
            )

        selected = lookup.get(str(verdict.selected))
        if selected is None:
            safe = choose_safe_default(top_candidates)
            return self._recommendation_from_candidate(
                safe, verdict.calibrated_confidence, verdicts_verdict=verdict,
                legal=top_candidates, reason="SELECTION_NOT_IN_ENVELOPE",
                evidence=evidence, question=spec,
            )

        return self._recommendation_from_candidate(
            selected, verdict.calibrated_confidence, verdicts_verdict=verdict,
            legal=top_candidates, evidence=evidence, question=spec,
        )

    def _recommendation_from_candidate(
        self,
        candidate: ActionCandidate,
        confidence: float,
        verdicts_verdict: Any,
        legal: List[ActionCandidate],
        reason: Optional[str] = None,
        evidence: Optional[Dict[str, Any]] = None,
        question: Optional[Dict[str, Any]] = None,
    ) -> Recommendation:
        return Recommendation(
            candidate_id=candidate.candidate_id,
            action_type=candidate.metadata.get("action_type", "unknown"),
            description=candidate.description,
            confidence=confidence,
            is_fallback=reason is not None,
            fallback_reason=reason,
            distribution=dict(getattr(verdicts_verdict, "distribution", {})),
            evidence=evidence or {},
            question=question or {},
            primitive=str(getattr(getattr(verdicts_verdict, "primitive", None), "value",
                                  getattr(verdicts_verdict, "primitive", PrimitiveType.CHOICE.value))),
            legal_actions=[
                {
                    "id": c.candidate_id,
                    "action_type": c.metadata.get("action_type"),
                    "description": c.description,
                }
                for c in legal
            ],
        )

    def close(self) -> None:
        """Flushes the telemetry journal (DecisionEngine.close is synchronous)."""
        self._engine.close()
