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


def get_scoring_cards(cards: Sequence[Card], hand_type: Optional[str] = None) -> List[Card]:
    """
    Identifies the subset of played cards that actually score for the detected hand type.
    In Balatro:
      - Stone cards always score regardless of the hand type.
      - High Card: Only the single highest-value card scores.
      - Pair: Only the 2 cards matching the pair's rank score.
      - Two Pair: Only the 4 cards matching the two pairs score.
      - Three of a Kind: Only the 3 matching cards score.
      - Four of a Kind: Only the 4 matching cards score.
      - Full House, Flush, Straight, Straight Flush, Five of a Kind, Flush House, Flush Five: All 5 cards score.
    """
    if not cards:
        return []
    if hand_type is None:
        hand_type = detect_hand_type(cards)

    # 5-card combinations: all cards score
    if hand_type in (
        "Full House", "Flush", "Straight", "Straight Flush",
        "Five of a Kind", "Flush House", "Flush Five"
    ):
        return list(cards)

    scoring: List[Card] = []
    stone_cards = [c for c in cards if getattr(c, "enhancement", "") == "stone"]
    non_stone = [c for c in cards if getattr(c, "enhancement", "") != "stone"]

    # Rank frequency map for non-stone cards
    counts: Dict[str, List[Card]] = {}
    for c in non_stone:
        counts.setdefault(c.rank, []).append(c)

    if hand_type == "Four of a Kind":
        for rank, group in counts.items():
            if len(group) >= 4:
                scoring.extend(group[:4])
                break
    elif hand_type == "Three of a Kind":
        for rank, group in counts.items():
            if len(group) >= 3:
                scoring.extend(group[:3])
                break
    elif hand_type == "Two Pair":
        pairs = [group for rank, group in counts.items() if len(group) >= 2]
        pairs.sort(key=lambda g: _rank_value(g[0]), reverse=True)
        for p in pairs[:2]:
            scoring.extend(p[:2])
    elif hand_type == "Pair":
        for rank, group in sorted(counts.items(), key=lambda item: _rank_value(item[1][0]), reverse=True):
            if len(group) >= 2:
                scoring.extend(group[:2])
                break
    elif hand_type == "High Card":
        if non_stone:
            highest_card = max(non_stone, key=_rank_value)
            scoring.append(highest_card)

    for sc in stone_cards:
        if sc not in scoring:
            scoring.append(sc)

    return scoring if scoring else list(cards)


def can_attach_junk(core_cards: Sequence[Card], target_hand_type: str, candidate_junk: Sequence[Card]) -> bool:
    """
    Validates whether candidate junk cards can be attached to the core cards
    without altering or corrupting the intended poker hand type.
    """
    combined = list(core_cards) + list(candidate_junk)
    if len(combined) > 5:
        return False
    return detect_hand_type(combined) == target_hand_type


def _card_keep_priority(card: Card) -> float:
    """
    Lower score = less desirable to keep = better candidate to dump/filter out.
    """
    score = float(_rank_value(card))
    if getattr(card, "enhancement", None):
        score += 20.0
    if getattr(card, "edition", None):
        score += 30.0
    if getattr(card, "seal", None):
        score += 40.0
    return score


def find_safe_junk_combinations(
    hand: Sequence[Card],
    core_indices: Sequence[int],
    target_hand_type: str,
    max_total_cards: int = 5,
) -> List[List[int]]:
    """
    Finds legal, safe combinations of junk cards from the remaining hand cards
    that can be attached to the core cards while strictly preserving target_hand_type.
    Returns list of junk index lists, ordered by best junk to dump (lowest keep priority).
    """
    core_cards = [hand[i] for i in core_indices]
    core_len = len(core_indices)
    max_junk = max_total_cards - core_len
    if max_junk <= 0:
        return []

    available_indices = [i for i in range(len(hand)) if i not in core_indices]
    if not available_indices:
        return []

    # Sort available cards by dump priority (lowest keep priority first)
    available_indices.sort(key=lambda idx: _card_keep_priority(hand[idx]))

    safe_combos: List[List[int]] = []
    # Test junk combinations of sizes from 1 up to max_junk
    for k in range(1, min(max_junk, len(available_indices)) + 1):
        for combo in combinations(available_indices, k):
            junk_cards = [hand[i] for i in combo]
            if can_attach_junk(core_cards, target_hand_type, junk_cards):
                safe_combos.append(list(combo))
                if len(safe_combos) >= 8:
                    break
        if len(safe_combos) >= 12:
            break

    return safe_combos


def enumerate_selections(hand: Sequence[Card], max_played: int = 5) -> List[Tuple[int, ...]]:
    """All legal played-card selections of a hand (index tuples), 1..max_played."""
    n = len(hand)
    out: List[Tuple[int, ...]] = []
    for k in range(1, min(max_played, n) + 1):
        out.extend(combinations(range(n), k))
    return out
