"""
Balatro poker-hand detection.

Given the played card selection (with wild enhancements), classify the hand
into one of Balatro's 14 hand types and report the level-1 base chips/mult.
(Level upgrades from Planet cards are handled by the bridge passing hand
levels in state — v1 keeps level 1 defaults.)
"""

from __future__ import annotations

from itertools import combinations
from typing import Dict, List, Sequence, Tuple

from .state import ACE_LOWTAIL, Card

# Vanilla level-1 base chips x mult.
BASE_VALUES: Dict[str, Tuple[int, int]] = {
    "Flush Five": (160, 16),
    "Flush House": (140, 14),
    "Five of a Kind": (120, 12),
    "Straight Flush": (100, 8),
    "Four of a Kind": (60, 7),
    "Full House": (40, 4),
    "Flush": (35, 4),
    "Straight": (30, 4),
    "Three of a Kind": (30, 3),
    "Two Pair": (20, 2),
    "Pair": (10, 2),
    "High Card": (5, 1),
}

# Strongest first — first match wins.
_HAND_ORDER = [
    "Flush Five", "Flush House", "Five of a Kind", "Straight Flush",
    "Four of a Kind", "Full House", "Flush", "Straight",
    "Three of a Kind", "Two Pair", "Pair", "High Card",
]

_RANK_VALUE = {str(v): v for v in range(2, 11)}
_RANK_VALUE.update({"J": 11, "Q": 12, "K": 13, "A": 14})


def _rank_value(card: Card) -> int:
    return _RANK_VALUE[card.rank]


def _satisfies_suit(card: Card, wanted: str) -> bool:
    return wanted in card.suits()


def _is_flush(cards: Sequence[Card]) -> bool:
    """All cards share one suit (wilds count as any suit -> satisfied iff some
    suit is compatible with every card)."""
    if not cards:
        return False
    for suit in ("S", "H", "D", "C"):
        if all(_satisfies_suit(c, suit) for c in cards):
            return True
    return False


def _is_straight(cards: Sequence[Card]) -> bool:
    """Five distinct consecutive ranks; A plays high and low (A2345)."""
    if len(cards) != 5:
        return False
    vals = sorted({_rank_value(c) for c in cards})
    if len(vals) != 5:
        return False
    if vals[-1] - vals[0] == 4:
        return True
    # Ace-low wheel: A(14) 2 3 4 5.
    ranks = {c.rank for c in cards}
    return ranks == set(ACE_LOWTAIL)


def _is_five_of_a_kind(cards: Sequence[Card]) -> bool:
    if len(cards) != 5:
        return False
    return len({c.rank for c in cards}) == 1


def detect_hand_type(cards: Sequence[Card]) -> str:
    """Classify a played selection into a Balatro hand type (vanilla rules).

    Priority mirrors the game: five-of-a-kind family > straight flush >
    four of a kind > flush house > full house > flush > straight > trips >
    two pair > pair > high card.
    """
    n = len(cards)
    if n == 0:
        return "High Card"
    counts: Dict[int, int] = {}
    for c in cards:
        counts[_rank_value(c)] = counts.get(_rank_value(c), 0) + 1
    count_vals = sorted(counts.values(), reverse=True)
    flush_ok = n >= 5 and _is_flush(cards)
    straight_ok = n == 5 and _is_straight(cards)
    five_ok = n == 5 and len(counts) == 1

    if five_ok and flush_ok:
        return "Flush Five"
    if five_ok:
        return "Five of a Kind"
    if flush_ok and straight_ok:
        return "Straight Flush"
    if count_vals[0] >= 4:
        return "Four of a Kind"
    if n == 5 and count_vals == [3, 2]:
        return "Flush House" if flush_ok else "Full House"
    if flush_ok:
        return "Flush"
    if straight_ok:
        return "Straight"
    if count_vals[0] == 3:
        return "Three of a Kind"
    if count_vals[0] == 2 and len(count_vals) >= 2 and count_vals[1] == 2:
        return "Two Pair"
    if count_vals[0] == 2:
        return "Pair"
    return "High Card"


def base_values(hand_type: str) -> Tuple[int, int]:
    """Level-1 base (chips, mult) for a hand type."""
    return BASE_VALUES.get(hand_type, (5, 1))


def enumerate_selections(hand: Sequence[Card], max_played: int = 5) -> List[Tuple[int, ...]]:
    """All legal played-card selections of a hand (index tuples), 1..max_played."""
    n = len(hand)
    out: List[Tuple[int, ...]] = []
    for k in range(1, min(max_played, n) + 1):
        out.extend(combinations(range(n), k))
    return out
