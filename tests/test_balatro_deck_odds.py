"""
Tests for Deck Odds, Outs, and Draw Probability Calculations in Balatro DecideX.
"""

from __future__ import annotations

import pytest

from decidex.balatro.advisor import build_evidence
from decidex.balatro.deck_odds import (
    DeckProfile,
    analyze_hand_draw_potential,
    calculate_flush_draw_odds,
    calculate_straight_draw_odds,
    compute_remaining_deck,
)
from decidex.balatro.state import BalatroState, Blind, Card, Joker


def test_compute_remaining_deck_subtraction():
    # 8 visible cards in hand
    hand = [
        Card(rank="A", suit="Spades"),
        Card(rank="K", suit="Spades"),
        Card(rank="Q", suit="Spades"),
        Card(rank="J", suit="Spades"),
        Card(rank="10", suit="Spades"),
        Card(rank="2", suit="Hearts"),
        Card(rank="3", suit="Hearts"),
        Card(rank="4", suit="Diamonds"),
    ]
    deck = compute_remaining_deck(hand)
    # Total remaining should be 52 - 8 = 44
    assert deck.total_remaining == 44
    # Spades started with 13, 5 seen in hand -> 8 left in deck
    assert deck.suit_counts["S"] == 8
    # Hearts started with 13, 2 seen in hand -> 11 left in deck
    assert deck.suit_counts["H"] == 11
    # Clubs: 0 seen in hand -> 13 left in deck
    assert deck.suit_counts["C"] == 13
    # Diamonds: 1 seen in hand -> 12 left in deck
    assert deck.suit_counts["D"] == 12


def test_calculate_flush_draw_odds():
    deck = DeckProfile(
        total_remaining=40,
        suit_counts={"S": 8, "H": 10, "D": 11, "C": 11},
        rank_counts={},
        enhanced_counts={},
        sealed_counts={},
        face_card_count=10,
    )
    # Player holds 4 spades (needs 1 more spade to make 5-card flush)
    # Discarding 3 cards gives 3 draws
    odds = calculate_flush_draw_odds(current_suit_count=4, suit="S", deck=deck, cards_drawn=3)
    # Expected odds: 1 - (32/40 * 31/39 * 30/38) = 1 - 0.5008 ~ 0.4992
    assert 0.48 <= odds <= 0.52

    # Already has 5 spades: probability is 1.0
    complete_odds = calculate_flush_draw_odds(current_suit_count=5, suit="S", deck=deck, cards_drawn=3)
    assert complete_odds == 1.0

    # 0 outs in deck
    no_outs_deck = DeckProfile(
        total_remaining=40,
        suit_counts={"S": 0, "H": 10, "D": 15, "C": 15},
        rank_counts={},
        enhanced_counts={},
        sealed_counts={},
        face_card_count=10,
    )
    zero_odds = calculate_flush_draw_odds(current_suit_count=4, suit="S", deck=no_outs_deck, cards_drawn=3)
    assert zero_odds == 0.0


def test_calculate_straight_draw_odds_open_ended():
    deck = DeckProfile(
        total_remaining=40,
        suit_counts={"S": 10, "H": 10, "D": 10, "C": 10},
        rank_counts={"4": 3, "9": 3, "2": 4, "3": 4, "10": 4, "J": 4, "Q": 4, "K": 4, "A": 4},
        enhanced_counts={},
        sealed_counts={},
        face_card_count=12,
    )
    # Open-ended 4-card straight: 5, 6, 7, 8 (needs 4 or 9, 6 outs total)
    current_ranks = {"5", "6", "7", "8"}
    odds = calculate_straight_draw_odds(current_ranks, deck, cards_drawn=4)
    # With 6 outs out of 40 cards, drawing 4 cards has significant hit probability
    assert 0.45 <= odds <= 0.55


def test_evidence_includes_deck_odds():
    state = BalatroState(
        hand=[
            Card(rank="A", suit="Spades"),
            Card(rank="K", suit="Spades"),
            Card(rank="Q", suit="Spades"),
            Card(rank="J", suit="Spades"),
            Card(rank="2", suit="Hearts"),
            Card(rank="3", suit="Hearts"),
            Card(rank="4", suit="Diamonds"),
            Card(rank="5", suit="Clubs"),
        ],
        blind=Blind(name="Small Blind", chips=300, scored=0),
        jokers=[Joker(name="Joker", mult=4)],
        discards_left=3,
        hands_left=4,
        money=10,
        ante=1,
    )
    evidence = build_evidence(state, 5)
    assert "deck_odds" in evidence
    assert "deck_remaining" in evidence
    assert evidence["deck_remaining"] == 44
    deck_odds = evidence["deck_odds"]
    assert deck_odds["best_suit"] == "S"
    assert deck_odds["best_suit_in_hand"] == 4
    assert deck_odds["flush_best_odds"] > 0.4
