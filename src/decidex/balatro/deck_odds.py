"""
Deck Composition and Draw Probability Tracking for Balatro DecideX.

Calculates exact remaining deck counts (outs) and hypergeometric / combinatorial
draw probabilities when discarding or searching for specific hands (Flush, Straight,
Full House, Pairs).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

from .state import SUITS, RANKS, Card

SUIT_NORM: Dict[str, str] = {
    "S": "S", "Spades": "S", "spades": "S", "♠": "S",
    "H": "H", "Hearts": "H", "hearts": "H", "♥": "H",
    "D": "D", "Diamonds": "D", "diamonds": "D", "♦": "D",
    "C": "C", "Clubs": "C", "clubs": "C", "♣": "C",
}


def _comb(n: int, k: int) -> int:
    """Standard combination nCr with safe boundary handling."""
    if k < 0 or k > n:
        return 0
    return math.comb(n, k)


@dataclass
class DeckProfile:
    """Exact breakdown of remaining cards in the deck."""
    total_remaining: int
    suit_counts: Dict[str, int]
    rank_counts: Dict[str, int]
    enhanced_counts: Dict[str, int]
    sealed_counts: Dict[str, int]
    face_card_count: int


def compute_remaining_deck(
    hand: List[Card],
    discards: Optional[List[Card]] = None,
    deck_cards: Optional[List[Card]] = None,
    total_deck_size: int = 52,
) -> DeckProfile:
    """
    Compute cards remaining in the draw pile given the visible hand,
    discards, and total deck size. If full deck cards are known, derives
    exact remaining cards; otherwise assumes standard 52-card base.
    """
    seen: List[Card] = list(hand)
    if discards:
        seen.extend(discards)

    suit_counts = {s: 13 for s in SUITS}
    rank_counts = {r: 4 for r in RANKS}
    enhanced_counts = {"glass": 0, "steel": 0, "gold": 0, "lucky": 0, "bonus": 0, "mult": 0, "wild": 0}
    sealed_counts = {"red": 0, "blue": 0, "gold": 0, "purple": 0}

    if deck_cards and len(deck_cards) > 0:
        # Full deck breakdown is provided
        suit_counts = {s: 0 for s in SUITS}
        rank_counts = {r: 0 for r in RANKS}
        for c in deck_cards:
            s = SUIT_NORM.get(c.suit, c.suit)
            suit_counts[s] = suit_counts.get(s, 0) + 1
            rank_counts[c.rank] = rank_counts.get(c.rank, 0) + 1
            if c.enhancement:
                enhanced_counts[c.enhancement] = enhanced_counts.get(c.enhancement, 0) + 1
            if c.seal:
                sealed_counts[c.seal] = sealed_counts.get(c.seal, 0) + 1

    # Subtract seen cards
    for c in seen:
        s = SUIT_NORM.get(c.suit, c.suit)
        if s in suit_counts and suit_counts[s] > 0:
            suit_counts[s] -= 1
        if c.rank in rank_counts and rank_counts[c.rank] > 0:
            rank_counts[c.rank] -= 1
        if c.enhancement and c.enhancement in enhanced_counts and enhanced_counts[c.enhancement] > 0:
            enhanced_counts[c.enhancement] -= 1
        if c.seal and c.seal in sealed_counts and sealed_counts[c.seal] > 0:
            sealed_counts[c.seal] -= 1

    total_rem = sum(rank_counts.values())
    face_count = sum(rank_counts.get(r, 0) for r in ("J", "Q", "K"))

    return DeckProfile(
        total_remaining=max(0, total_rem),
        suit_counts=suit_counts,
        rank_counts=rank_counts,
        enhanced_counts=enhanced_counts,
        sealed_counts=sealed_counts,
        face_card_count=face_count,
    )


def calculate_flush_draw_odds(
    current_suit_count: int,
    suit: str,
    deck: DeckProfile,
    cards_drawn: int,
) -> float:
    """
    Calculate the exact probability of hitting a 5-card Flush when drawing
    `cards_drawn` cards. Hypergeometric probability:
    P(Flush) = 1 - P(drawn < needed)
    """
    needed = max(0, 5 - current_suit_count)
    if needed == 0:
        return 1.0
    N = deck.total_remaining
    K = deck.suit_counts.get(suit, 0)
    n = cards_drawn

    if n <= 0 or K < needed or N < n:
        return 0.0

    total_ways = _comb(N, n)
    if total_ways == 0:
        return 0.0

    # Sum ways to draw fewer than `needed` target suit cards
    fail_ways = sum(_comb(K, i) * _comb(N - K, n - i) for i in range(needed))
    prob = 1.0 - (fail_ways / total_ways)
    return round(max(0.0, min(1.0, prob)), 4)


def calculate_straight_draw_odds(
    current_ranks: Set[str],
    deck: DeckProfile,
    cards_drawn: int,
) -> float:
    """Exact chance of drawing every missing rank for any legal five-rank run."""
    present = set(current_ranks) & set(RANKS)
    rank_values = list(RANKS)
    windows = [set(rank_values[i:i + 5]) for i in range(len(rank_values) - 4)]
    windows.append({"A", "2", "3", "4", "5"})
    missing_sets = [window - present for window in windows if len(window - present) <= cards_drawn]
    if not missing_sets:
        return 0.0
    if any(not missing and len(present) >= 5 for missing in missing_sets):
        return 1.0

    n, total = cards_drawn, deck.total_remaining
    if n <= 0 or n > total:
        return 0.0
    denominator = _comb(total, n)
    if not denominator:
        return 0.0

    # Inclusion-exclusion over successful straight windows. A window succeeds
    # iff every rank in its missing set appears at least once in the draw.
    favorable = 0
    for mask in range(1, 1 << len(missing_sets)):
        required = set()
        bits = 0
        for i, missing in enumerate(missing_sets):
            if mask & (1 << i):
                bits += 1
                required.update(missing)
        ways = 0
        ranks = list(required)
        for submask in range(1 << len(ranks)):
            excluded = [ranks[i] for i in range(len(ranks)) if submask & (1 << i)]
            excluded_count = sum(max(0, deck.rank_counts.get(rank, 0)) for rank in excluded)
            ways += (-1 if len(excluded) % 2 else 1) * _comb(total - excluded_count, n)
        favorable += (-1 if bits % 2 == 0 else 1) * ways

    return round(max(0.0, min(1.0, favorable / denominator)), 4)


def analyze_hand_draw_potential(
    hand: List[Card],
    deck: DeckProfile,
    discards_left: int,
) -> Dict[str, float]:
    """
    Analyze the immediate draw potential for the current hand when discarding up to 5 cards.
    Returns probabilities of completing major hands.
    """
    if not hand or deck.total_remaining <= 0 or discards_left <= 0:
        return {"flush_best_odds": 0.0, "straight_odds": 0.0}

    # Best suit flush potential
    suit_counts: Dict[str, int] = {}
    for c in hand:
        s = SUIT_NORM.get(c.suit, c.suit)
        suit_counts[s] = suit_counts.get(s, 0) + 1

    best_suit = max(suit_counts, key=suit_counts.get) if suit_counts else "S"
    cur_suit_count = suit_counts.get(best_suit, 0)
    # Cards to discard to chase flush
    cards_to_discard = min(5, 8 - cur_suit_count)
    flush_odds = calculate_flush_draw_odds(cur_suit_count, best_suit, deck, cards_to_discard)

    # Straight potential
    ranks_present = {c.rank for c in hand}
    straight_odds = calculate_straight_draw_odds(ranks_present, deck, cards_drawn=4)

    return {
        "best_suit": best_suit,
        "best_suit_in_hand": cur_suit_count,
        "best_suit_outs_in_deck": deck.suit_counts.get(best_suit, 0),
        "flush_best_odds": flush_odds,
        "straight_odds": straight_odds,
    }
