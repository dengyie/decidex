"""
Tests for Balatro Filtering Play (出牌附带杂牌 / 伪弃牌) and Hand Type Safety.
"""

from decidex.balatro import (
    BalatroState,
    Blind,
    Card,
    Joker,
    detect_hand_type,
    enumerate_legal_actions,
    get_scoring_cards,
)
from decidex.balatro.constraints import detect_active_constraints, evaluate_play_safety
from decidex.balatro.poker import can_attach_junk, find_safe_junk_combinations
from decidex.balatro.rules import choose_safe_default


def _card(rank: str, suit: str, **kwargs) -> Card:
    return Card(rank=rank, suit=suit, **kwargs)


def test_get_scoring_cards_subsets():
    """Verify get_scoring_cards accurately isolates scoring cards from kickers/junk cards."""
    # 1. Pair + 3 kickers
    hand_pair = [_card("A", "S"), _card("A", "H"), _card("2", "D"), _card("4", "C"), _card("7", "S")]
    scoring_pair = get_scoring_cards(hand_pair, "Pair")
    assert len(scoring_pair) == 2
    assert {c.rank for c in scoring_pair} == {"A"}

    # 2. Two Pair + 1 kicker
    hand_two_pair = [_card("K", "S"), _card("K", "H"), _card("8", "D"), _card("8", "C"), _card("3", "S")]
    scoring_two_pair = get_scoring_cards(hand_two_pair, "Two Pair")
    assert len(scoring_two_pair) == 4
    assert {c.rank for c in scoring_two_pair} == {"K", "8"}

    # 3. Three of a Kind + 2 kickers
    hand_trips = [_card("J", "S"), _card("J", "H"), _card("J", "D"), _card("2", "C"), _card("5", "S")]
    scoring_trips = get_scoring_cards(hand_trips, "Three of a Kind")
    assert len(scoring_trips) == 3
    assert {c.rank for c in scoring_trips} == {"J"}

    # 4. Four of a Kind + 1 kicker
    hand_quads = [_card("10", "S"), _card("10", "H"), _card("10", "D"), _card("10", "C"), _card("2", "S")]
    scoring_quads = get_scoring_cards(hand_quads, "Four of a Kind")
    assert len(scoring_quads) == 4
    assert {c.rank for c in scoring_quads} == {"10"}

    # 5. Flush (all 5 score)
    hand_flush = [_card("2", "H"), _card("5", "H"), _card("7", "H"), _card("9", "H"), _card("K", "H")]
    scoring_flush = get_scoring_cards(hand_flush, "Flush")
    assert len(scoring_flush) == 5

    # 6. High Card + 4 kickers
    hand_high = [_card("A", "S"), _card("8", "D"), _card("6", "C"), _card("4", "H"), _card("2", "S")]
    scoring_high = get_scoring_cards(hand_high, "High Card")
    assert len(scoring_high) == 1
    assert scoring_high[0].rank == "A"


def test_can_attach_junk_hand_type_fidelity():
    """Verify can_attach_junk prevents accidental hand type changes or corruptions."""
    core_pair = [_card("8", "S"), _card("8", "H")]
    target_type = "Pair"

    # Attaching uncoordinated singletons -> Safe (stays Pair)
    safe_junk = [_card("2", "D"), _card("3", "C")]
    assert can_attach_junk(core_pair, target_type, safe_junk) is True

    # Attaching a third 8 -> Unsafe (becomes Three of a Kind, not Pair)
    trips_junk = [_card("8", "D")]
    assert can_attach_junk(core_pair, target_type, trips_junk) is False

    # Attaching another pair [4D, 4C] -> Unsafe (becomes Two Pair, not Pair)
    two_pair_junk = [_card("4", "D"), _card("4", "C")]
    assert can_attach_junk(core_pair, target_type, two_pair_junk) is False

    # Attaching 4 cards when core is 2 (total > 5) -> Unsafe
    too_many_junk = [_card("2", "D"), _card("3", "C"), _card("4", "H"), _card("5", "S")]
    assert can_attach_junk(core_pair, target_type, too_many_junk) is False


def test_find_safe_junk_combinations():
    """find_safe_junk_combinations finds legal combinations of junk cards ordered by lowest keep priority."""
    hand = [
        _card("A", "S"), _card("A", "H"),  # Core Pair (indices 0, 1)
        _card("2", "C"),                   # Low trash (index 2)
        _card("3", "D"),                   # Low trash (index 3)
        _card("K", "H", enhancement="steel"), # High keep priority steel card (index 4)
    ]
    combos = find_safe_junk_combinations(hand, core_indices=[0, 1], target_hand_type="Pair", max_total_cards=4)
    assert len(combos) > 0
    # First combination to dump should prioritize the lowest value cards (2C and 3D) before the steel card
    first_combo = combos[0]
    assert 2 in first_combo or 3 in first_combo


def test_enumerate_legal_actions_identifies_filtering_plays():
    """enumerate_legal_actions marks plays with extra non-scoring cards as is_filtering_play."""
    hand = [
        _card("9", "S"), _card("9", "H"),  # Pair
        _card("2", "C"), _card("3", "D"), _card("4", "H"),  # Junk
    ]
    st = BalatroState(hand=hand, blind=Blind(name="Small Blind", chips=300), discards_left=0)

    actions = enumerate_legal_actions(st)
    play_actions = [a for a in actions if a.metadata.get("action_type") == "play"]

    filtering_plays = [a for a in play_actions if a.metadata.get("is_filtering_play") is True]
    assert len(filtering_plays) > 0

    # A 5-card play containing the pair + 3 junk cards
    five_card_play = next(a for a in filtering_plays if len(a.metadata["indices"]) == 5)
    assert five_card_play.metadata["hand_type"] == "Pair"
    assert len(five_card_play.metadata["scoring_cards"]) == 2
    assert len(five_card_play.metadata["dumped_cards"]) == 3
    assert "附带弃牌" in five_card_play.description


def test_filtering_play_priority_when_zero_discards():
    """When discards_left == 0, filtering plays that dump junk receive a heuristic bonus."""
    hand = [
        _card("9", "S"), _card("9", "H"),  # Pair
        _card("2", "C"), _card("3", "D"), _card("4", "H"),  # Junk
    ]
    # State with 0 discards left
    st_zero = BalatroState(hand=hand, blind=Blind(name="Small Blind", chips=1000), discards_left=0)
    actions_zero = enumerate_legal_actions(st_zero)

    bare_pair = next(a for a in actions_zero if len(a.metadata.get("indices", [])) == 2 and a.metadata.get("hand_type") == "Pair")
    filtering_pair = next(a for a in actions_zero if len(a.metadata.get("indices", [])) == 5 and a.metadata.get("hand_type") == "Pair")

    # The filtering pair should have a higher heuristic score than the bare pair because it clears junk
    assert filtering_pair.heuristic_score > bare_pair.heuristic_score


def test_ride_the_bus_with_non_scoring_face_card():
    """Ride the Bus resets mult only on SCORING face cards. Non-scoring face cards do not reset."""
    hand = [_card("8", "S"), _card("8", "H"), _card("K", "D")]
    constraints = detect_active_constraints([Joker(name="Ride the Bus")])

    # Core play: Pair of 8s (scoring cards are [8S, 8H], K is non-scoring)
    scoring = get_scoring_cards(hand, "Pair")
    assert len(scoring) == 2
    assert all(c.rank == "8" for c in scoring)

    safety = evaluate_play_safety(hand, "Pair", constraints, scoring_cards=scoring)
    # Since the King is NOT a scoring card, it is NOT a fatal violation!
    assert safety.is_fatal_violation is False

    # But if someone plays King as High Card (where King IS a scoring card) -> Fatal!
    hand_k = [_card("K", "D"), _card("2", "S")]
    scoring_k = get_scoring_cards(hand_k, "High Card")
    safety_k = evaluate_play_safety(hand_k, "High Card", constraints, scoring_cards=scoring_k)
    assert safety_k.is_fatal_violation is True
