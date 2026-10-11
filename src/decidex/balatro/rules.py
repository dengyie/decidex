"""
Balatro legal-action enumeration and deterministic safe default.

Action id conventions (stable, greppable):
  play:<i1>-<i2>-...      play those hand indices (1..5 cards)
  discard:<i1>-<...>      discard those hand indices (1..5 cards)
  use:<consumable_idx>    use the consumable now
"""

from __future__ import annotations

from itertools import combinations
from typing import List

from decidex.types import ActionCandidate

from .poker import enumerate_selections
from .scoring import simulate_play
from .state import BalatroState

MAX_SELECT = 5


def _ids(indices) -> str:
    return "-".join(str(i) for i in indices)


def _heuristic_keep(cards):
    """How much a discard set is 'worth dumping': low-value cards first."""
    return sum(5 - min(5, c.chips // 4) for c in cards)


def enumerate_legal_actions(state: BalatroState) -> List[ActionCandidate]:
    """All legal plays/discards/uses, each play pre-scored by the simulator."""
    candidates: List[ActionCandidate] = []
    blind = state.blind.remaining

    # 1) plays: score every legal selection (<= C(8,5)+C(8,4)+... ≈ 218).
    for sel in enumerate_selections(state.hand, MAX_SELECT):
        cards = [state.hand[i] for i in sel]
        result = simulate_play(cards, state)
        clears = result.score >= blind
        score = result.score
        # Heuristic prior: clearing now is paramount; otherwise bigger is better
        # and saving hands has mild value.
        heuristic = score / max(1.0, blind) + (3.0 if clears else 0.0) + 0.15 * (state.hands_left - 1)
        candidates.append(ActionCandidate(
            candidate_id=f"play:{_ids(sel)}",
            description=f"打出 {result.display}"
                        + ("(足够过关)" if clears else f"(距 {blind - score:.0f})"),
            heuristic_score=round(heuristic, 4),
            metadata={
                "action_type": "play", "indices": list(sel), "cards": [c.label for c in cards],
                "hand_type": result.hand_type, "score": score,
                "chips": result.chips, "mult": result.mult,
                "clears": clears,
            },
        ))

    # 2) discards: redraw potential; only meaningful with discards left.
    if state.discards_left > 0:
        for sel in enumerate_selections(state.hand, MAX_SELECT):
            cards = [state.hand[i] for i in sel]
            candidates.append(ActionCandidate(
                candidate_id=f"discard:{_ids(sel)}",
                description=f"弃掉 {' '.join(c.label for c in cards)} 换牌",
                heuristic_score=round(0.5 + 0.1 * _heuristic_keep(cards), 4),
                metadata={
                    "action_type": "discard", "indices": list(sel),
                    "cards": [c.label for c in cards],
                },
            ))

    # 3) consumables: the advisor may suggest using one before acting.
    for i, cons in enumerate(state.consumables):
        candidates.append(ActionCandidate(
            candidate_id=f"use:{i}",
            description=f"先使用 {cons.name}({cons.type})",
            heuristic_score=0.8,
            metadata={"action_type": "use_consumable", "index": i, "name": cons.name},
        ))

    return candidates


def choose_safe_default(candidates: List[ActionCandidate], state: BalatroState) -> ActionCandidate:
    """
    Deterministic fallback: clear the blind with the best-scoring play if any
    play can; otherwise the biggest single-hand score; never a blind gamble
    when a guaranteed clear exists.
    """
    plays = [c for c in candidates if c.metadata.get("action_type") == "play"]
    clears = [c for c in plays if c.metadata.get("clears")]
    if clears:
        return max(clears, key=lambda c: c.metadata["score"])
    if plays:
        return max(plays, key=lambda c: c.metadata["score"])
    if not candidates:
        return ActionCandidate(candidate_id="play:0", description="打出第一张牌",
                               metadata={"action_type": "play", "indices": [0]})
    return max(candidates, key=lambda c: c.heuristic_score)
