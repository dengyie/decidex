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
    """All legal plays/discards/uses, each play pre-scored by the simulator.

    Heuristic tiers keep the envelope honest under top_k pruning:
      plays always outrank discards while the blind is still winnable with the
      current hand(s); discards take over only when no sequence of remaining
      hands can reach the target ("hopeless"), and a guaranteed clear always
      outranks everything.
    """
    candidates: List[ActionCandidate] = []
    blind = state.blind.remaining

    # Pass 1: score every play, learn the best single-hand outcome.
    scored_plays = []
    best_play_score = 0.0
    for sel in enumerate_selections(state.hand, MAX_SELECT):
        cards = [state.hand[i] for i in sel]
        result = simulate_play(cards, state)
        scored_plays.append((sel, cards, result))
        best_play_score = max(best_play_score, result.score)
    # Rough "can the blind still be cleared at all" signal for discard value.
    hopeless = best_play_score * max(1, state.hands_left) < blind

    # Pass 2: build candidates with tiered priors.
    for sel, cards, result in scored_plays:
        clears = result.score >= blind
        heuristic = 2.0 + result.score / max(1.0, blind) + (3.0 if clears else 0.0)
        candidates.append(ActionCandidate(
            candidate_id=f"play:{_ids(sel)}",
            description=f"打出 {result.display}"
                        + ("(足够过关)" if clears else f"(距 {blind - result.score:.0f})"),
            heuristic_score=round(heuristic, 4),
            metadata={
                "action_type": "play", "indices": list(sel), "cards": [c.label for c in cards],
                "hand_type": result.hand_type, "score": result.score,
                "chips": result.chips, "mult": result.mult,
                "clears": clears,
            },
        ))

    if state.discards_left > 0:
        for sel in enumerate_selections(state.hand, MAX_SELECT):
            cards = [state.hand[i] for i in sel]
            # keep ∈ [0,25]; 0.03 caps the base below any play (2.0+) so the
            # envelope never loses every play while the blind is winnable.
            heuristic = 1.0 + 0.03 * _heuristic_keep(cards) + (2.5 if hopeless else 0.0)
            candidates.append(ActionCandidate(
                candidate_id=f"discard:{_ids(sel)}",
                description=f"弃掉 {' '.join(c.label for c in cards)} 换牌",
                heuristic_score=round(heuristic, 4),
                metadata={
                    "action_type": "discard", "indices": list(sel),
                    "cards": [c.label for c in cards],
                },
            ))

    for i, cons in enumerate(state.consumables):
        candidates.append(ActionCandidate(
            candidate_id=f"use:{i}",
            description=f"先使用 {cons.name}({cons.type})",
            heuristic_score=1.9,
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
