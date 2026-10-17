"""
BalatroAdvisor: Strategic read-only "best move" recommender built on DecideX.

Standard DecideX pipeline enhanced with Four Pillars Synergy & Hazard Constraints:
  1. Evidence   -> build_evidence(state): Injects 4-pillars radar, interest guard, and hazard warnings.
  2. Legal set  -> rules.enumerate_legal_actions: Multi-action generation with constraint-aware priors.
  3. Pruner     -> CandidatePruner.prune_to_top_k: Top-K choice bundle for LLM/Heuristic solver.
  4. Model      -> DecisionEngine.step: Evaluates criteria and returns calibrated verdict.
  5. Guard      -> CalibratedDecisionGuard: Enforces legality and confidence thresholds.
  6. Fallback   -> rules.choose_safe_default: Deterministic synergy-aware safe default policy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from decidex.engine import DecisionEngine
from decidex.guards import CalibratedDecisionGuard
from decidex.pruner import CandidatePruner
from decidex.providers.base import BaseInferenceProvider
from decidex.types import ActionCandidate, PrimitiveType, QuestionSpec, TurnPhase

from .constraints import detect_active_constraints
from .economy import calculate_economy_status
from .joker_order import compute_optimal_joker_order, estimate_ordering_gain
from .rules import choose_safe_default, enumerate_legal_actions
from .state import BalatroState
from .synergy import analyze_synergy


def build_evidence(state: BalatroState, n_candidates: int) -> Dict[str, Any]:
    """Rich domain evidence dict handed to DecisionEngine as observation payload."""
    ante = getattr(state, "ante", 1)
    money = getattr(state, "money", 0)
    blind_name = getattr(getattr(state, "blind", None), "name", "")
    most_played = getattr(state, "most_played_hand", None)

    # Domain strategies
    synergy = analyze_synergy(state.jokers, ante)
    econ = calculate_economy_status(money, ante)
    constraints = detect_active_constraints(
        state.jokers,
        blind_name=blind_name,
        most_played_hand=most_played,
        money=money,
    )
    opt_order, needs_reorder, order_reason = compute_optimal_joker_order(state.jokers)
    order_gain = estimate_ordering_gain(state.jokers)

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

        # Boss Blind first-class model context
        "boss_blind": {
            "name": constraints.boss_name,
            "has_boss": bool(constraints.boss_rule),
            "description": constraints.boss_rule.description if constraints.boss_rule else "普通盲注 (无特殊限制)",
            "strategic_directive": constraints.strategic_directive or "正常构筑高倍率或高筹码牌型斩杀",
            "requires_5_cards": constraints.is_the_psychic,
            "no_repeat_hand": constraints.is_the_eye,
            "single_hand_type": constraints.is_the_mouth,
            "zero_discards": constraints.is_the_water,
            "single_hand_only": constraints.is_the_needle,
            "wipes_money": constraints.is_the_ox,
            "debuffed_suit": constraints.debuffed_suit,
            "debuff_face_cards": constraints.is_the_plant,
            "active_warnings": constraints.active_warnings,
        },

        # High-tier strategic context
        "strategy": {
            "synergy_profile": synergy.to_dict(),
            "economy_status": econ.to_dict(),
            "constraints": constraints.to_dict(),
            "joker_ordering": {
                "needs_reorder": needs_reorder,
                "optimal_indices": opt_order,
                "ordering_gain": order_gain,
                "reason": order_reason,
            },
        },
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
    evidence: Dict[str, Any] = field(default_factory=dict)
    question: Dict[str, Any] = field(default_factory=dict)
    primitive: str = PrimitiveType.CHOICE.value


class BalatroAdvisor:
    """
    Turns a :class:`BalatroState` into one recommended legal action via DecideX.
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
        )
        self.engine = DecisionEngine(
            provider=provider,
            guard=guard,
            journal_path=journal_path,
        )

    async def recommend(self, state: BalatroState) -> Recommendation:
        all_actions = enumerate_legal_actions(state)
        top_candidates = CandidatePruner.prune_to_top_k(all_actions, k=self.top_k)
        option_ids, lookup, descriptions = CandidatePruner.to_choice_bundle(top_candidates)

        evidence = build_evidence(state, len(all_actions))
        boss_info = evidence.get("boss_blind", {})
        active_boss = boss_info.get("name") if boss_info.get("has_boss") else None

        question_desc = "选择最优 Balatro 动作 (打牌/弃牌/购买/离开)"
        if active_boss:
            question_desc += f" [⚠️ Boss: {active_boss} - {boss_info.get('strategic_directive')}]"
        elif boss_info.get("active_warnings"):
            question_desc += f" [⚠️ 警报: {'; '.join(boss_info.get('active_warnings', []))}]"

        question = QuestionSpec(
            id="balatro_move",
            primitive=PrimitiveType.CHOICE,
            description=question_desc,
            options=option_ids,
            criteria=descriptions,
        )

        domain_state = dict(evidence)
        domain_state["top_actions"] = [
            {"id": c.candidate_id, "desc": c.description, "priors": c.heuristic_score}
            for c in top_candidates
        ]
        domain_state["hand_filtering_guidance"] = (
            "出牌带杂牌 (Filtering Play) 机制：当选择的牌型不足5张时，可在保证目标牌型不变的前提下附带废牌打出，充当伪弃牌以洗牌换牌；"
            "在遭遇 The Psychic 时，必须附带杂牌补满 5 张牌打出，否则直接判定为 0 分！"
        )

        phase = TurnPhase.SHOP if getattr(state, "shop", None) else TurnPhase.MAIN
        verdicts = await self.engine.step(
            domain_state=domain_state,
            questions=[question],
            legal_actions=option_ids,
            phase=phase,
        )
        v = verdicts["balatro_move"]

        # Resolve candidate
        choice_id = str(v.selected)
        matched = lookup.get(choice_id)
        is_fallback = v.is_fallback
        fallback_reason = v.fallback_reason

        if not matched or is_fallback:
            safe = choose_safe_default(top_candidates or all_actions, state)
            matched = safe
            choice_id = safe.candidate_id
            is_fallback = True
            if not fallback_reason:
                fallback_reason = "GUARD_OR_FALLBACK_DEFAULT"

        meta = matched.metadata if matched else {}
        action_type = meta.get("action_type", "unknown")
        expected_score = meta.get("score")
        clears_blind = meta.get("clears")

        return Recommendation(
            candidate_id=choice_id,
            action_type=action_type,
            description=matched.description if matched else choice_id,
            confidence=v.calibrated_confidence,
            expected_score=expected_score,
            clears_blind=clears_blind,
            is_fallback=is_fallback,
            fallback_reason=fallback_reason,
            distribution=v.distribution,
            legal_actions=[
                {
                    "id": c.candidate_id,
                    "candidate_id": c.candidate_id,
                    "action_type": c.metadata.get("action_type"),
                    "description": c.description,
                    "score": c.metadata.get("score"),
                    "clears": c.metadata.get("clears"),
                    "indices": c.metadata.get("indices"),
                    "metadata": c.metadata,
                    "heuristic_score": c.heuristic_score,
                }
                for c in top_candidates
            ],
            evidence=evidence,
            question=question.model_dump(),
            primitive=PrimitiveType.CHOICE.value,
        )

    def close(self) -> None:
        self.engine.close()
