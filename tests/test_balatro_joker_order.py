"""
Tests for Joker Ordering, Left-to-Right Evaluation, Edition Weighting,
and Copy Joker Dynamic Alignment in Balatro DecideX.
"""

from __future__ import annotations

import pytest

from decidex.balatro.joker_order import (
    _classify_joker_position_tier,
    compute_optimal_joker_order,
    estimate_ordering_gain,
)
from decidex.balatro.rules import enumerate_legal_actions
from decidex.balatro.state import BalatroState, Blind, Card, Joker


def test_standard_left_to_right_tier_ordering():
    # Setup: 1 Flat Mult (Gros Michel), 1 Chips (Ice Cream), 1 X-Mult (Cavendish)
    # Incorrect initial order: Cavendish (X-Mult) -> Gros Michel (+Mult) -> Ice Cream (Chips)
    jokers = [
        Joker(name="Cavendish", xmult=3.0),
        Joker(name="Gros Michel", mult=15),
        Joker(name="Ice Cream", chips=100),
    ]

    opt_order, needs_reorder, reason = compute_optimal_joker_order(jokers, "play")
    assert needs_reorder is True
    assert "严重排位缺陷" in reason

    # Expected order: Ice Cream (index 2) -> Gros Michel (index 1) -> Cavendish (index 0)
    assert opt_order == [2, 1, 0]

    # Gain should show massive mult increase
    gain = estimate_ordering_gain(jokers)
    assert gain > 1.3  # > 30% gain


def test_polychrome_flat_mult_ordering():
    # If a Flat Mult joker has Polychrome (e.g. Half Joker +20 mult, x1.5 mult),
    # it should be placed AFTER pure Flat Mult jokers (e.g. Gros Michel +15 mult)
    # so its flat mult adds, and then multiplies both flat mults!
    half_joker = Joker(name="Half Joker", mult=20)
    half_joker.edition = "polychrome"

    gros_michel = Joker(name="Gros Michel", mult=15)

    jokers = [half_joker, gros_michel]
    opt_order, needs_reorder, reason = compute_optimal_joker_order(jokers, "play")
    assert needs_reorder is True
    # Gros Michel (1) should come before Polychrome Half Joker (0)
    assert opt_order == [1, 0]


def test_polychrome_chips_or_economy_in_xmult_block():
    # If a Chips or Economy joker has Polychrome, it has no flat mult,
    # so it should be placed at the rightmost position alongside X-Mult!
    ice_cream = Joker(name="Ice Cream", chips=100)
    ice_cream.edition = "polychrome"

    gros_michel = Joker(name="Gros Michel", mult=15)

    jokers = [ice_cream, gros_michel]
    opt_order, needs_reorder, reason = compute_optimal_joker_order(jokers, "play")
    assert needs_reorder is True
    # Gros Michel (1) -> Polychrome Ice Cream (0)
    assert opt_order == [1, 0]


def test_blueprint_and_brainstorm_copy_alignment():
    # Brainstorm copies leftmost (slot 0), Blueprint copies joker to its right
    # Squad: Blueprint, Brainstorm, Cavendish (x3), Gros Michel (+15)
    cavendish = Joker(name="Cavendish", xmult=3.0)
    gros_michel = Joker(name="Gros Michel", mult=15)
    blueprint = Joker(name="Blueprint")
    brainstorm = Joker(name="Brainstorm")

    jokers = [blueprint, brainstorm, gros_michel, cavendish]
    opt_order, needs_reorder, reason = compute_optimal_joker_order(jokers, "play")
    assert needs_reorder is True
    assert "Blueprint" in reason or "Brainstorm" in reason

    # Slot 0 should be Gros Michel (+Flat Mult) for Brainstorm to duplicate first,
    # and Blueprint should be placed directly before Cavendish (X-Mult) to magnify at the end
    reordered_names = [jokers[i].name for i in opt_order]
    assert reordered_names[0] == "Gros Michel"
    assert reordered_names[-1] == "Cavendish"
    assert reordered_names[-2] == "Blueprint"


@pytest.mark.parametrize("phase", ["shop", "cash_out"])
def test_economy_copy_order_is_a_permutation_with_valid_blueprint_direction(phase):
    jokers = [
        Joker(name="Blueprint"),
        Joker(name="Golden Joker"),
        Joker(name="Brainstorm"),
        Joker(name="Cloud 9"),
        Joker(name="Cavendish", xmult=3.0),
    ]

    order, needs_reorder, _ = compute_optimal_joker_order(jokers, phase)

    assert needs_reorder
    assert sorted(order) == list(range(len(jokers)))
    names = [jokers[index].name for index in order]
    # Brainstorm copies the leftmost joker; Blueprint copies the next slot.
    assert names[0] in {"Golden Joker", "Cloud 9"}
    blueprint_position = names.index("Blueprint")
    assert blueprint_position + 1 < len(names)
    assert names[blueprint_position + 1] != "Blueprint"


def test_blueprint_alone_is_immediately_left_of_economy_target():
    jokers = [Joker(name="Golden Joker"), Joker(name="Blueprint"), Joker(name="Cavendish", xmult=3.0)]

    order, _, _ = compute_optimal_joker_order(jokers, "shop")

    names = [jokers[index].name for index in order]
    blueprint_position = names.index("Blueprint")
    assert names[blueprint_position + 1] == "Golden Joker"
    assert sorted(order) == list(range(len(jokers)))


def test_reorder_action_candidate_generated():
    # Test that reorder_jokers candidate is emitted in enumerate_legal_actions
    st = BalatroState(
        hand=[Card(rank="A", suit="S"), Card(rank="K", suit="S")],
        jokers=[
            Joker(name="Cavendish", xmult=3.0),
            Joker(name="Gros Michel", mult=15),
        ],
        blind=Blind(name="Small Blind", type="small", chips=300, scored=0),
        hands_left=4, discards_left=3,
    )
    actions = enumerate_legal_actions(st)
    reorder_act = next((a for a in actions if a.metadata.get("action_type") == "reorder_jokers"), None)
    assert reorder_act is not None
    assert reorder_act.metadata["optimal_order"] == [1, 0]
    assert reorder_act.heuristic_score > 6.0
