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


def test_held_in_hand_steel_and_gold_card_protection():
    """Verify that Steel, Gold, and Blue Seal cards are protected from junk dumping."""
    hand = [
        _card("A", "S"),
        _card("A", "H"),
        _card("2", "D", enhancement="steel"),
        _card("3", "C", enhancement="gold"),
        _card("4", "S"),
    ]
    state = BalatroState(
        hand=hand,
        blind=Blind(name="Small Blind", chips=600, scored=0),
        jokers=[Joker(name="Joker", mult=4)],
        hands_left=3,
        discards_left=0,
    )

    actions = enumerate_legal_actions(state)
    plays = [a for a in actions if a.metadata.get("action_type") == "play"]

    # Play dumping unenhanced 4S
    clean_play = next(
        p for p in plays
        if "A♠" in p.metadata["cards"] and "A♥" in p.metadata["cards"] and "4♠" in p.metadata["cards"]
        and "2♦" not in p.metadata["cards"] and "3♣" not in p.metadata["cards"]
    )

    # Play dumping the Steel 2D
    steel_dump_play = next(
        p for p in plays
        if "A♠" in p.metadata["cards"] and "A♥" in p.metadata["cards"] and "2♦" in p.metadata["cards"]
        and "4♠" not in p.metadata["cards"] and "3♣" not in p.metadata["cards"]
    )

    # Clean play must score significantly higher than the play that burns a Steel card!
    assert clean_play.heuristic_score > steel_dump_play.heuristic_score * 1.5


def test_baron_king_protection_from_junk_dumping():
    """When Baron is present, Kings held in hand provide x1.5 mult each and must not be dumped."""
    hand = [
        _card("A", "S"),
        _card("A", "H"),
        _card("K", "D"),  # Baron King
        _card("5", "C"),  # Ordinary junk
    ]
    state = BalatroState(
        hand=hand,
        blind=Blind(name="Big Blind", chips=600, scored=0),
        jokers=[Joker(name="Baron")],
        hands_left=2,
        discards_left=0,
    )

    actions = enumerate_legal_actions(state)
    plays = [a for a in actions if a.metadata.get("action_type") == "play"]

    junk_play = next(
        p for p in plays
        if "A♠" in p.metadata["cards"] and "A♥" in p.metadata["cards"] and "5♣" in p.metadata["cards"]
        and "K♦" not in p.metadata["cards"]
    )

    king_dump_play = next(
        p for p in plays
        if "A♠" in p.metadata["cards"] and "A♥" in p.metadata["cards"] and "K♦" in p.metadata["cards"]
        and "5♣" not in p.metadata["cards"]
    )

    assert junk_play.heuristic_score > king_dump_play.heuristic_score * 1.8


def test_the_psychic_5_card_filtering_bonus():
    """Under The Psychic, plays must contain 5 cards. Padding a Pair with 3 junk cards is rewarded."""
    hand = [
        _card("A", "S"),
        _card("A", "H"),
        _card("2", "D"),
        _card("3", "C"),
        _card("4", "S"),
    ]
    state = BalatroState(
        hand=hand,
        blind=Blind(name="The Psychic", chips=1000, scored=0),
        jokers=[Joker(name="Joker", mult=4)],
        hands_left=2,
        discards_left=0,
    )

    actions = enumerate_legal_actions(state)
    plays = [a for a in actions if a.metadata.get("action_type") == "play"]

    # 5-card padded play
    padded_play = next(
        p for p in plays
        if len(p.metadata["cards"]) == 5 and "A♠" in p.metadata["cards"] and "A♥" in p.metadata["cards"]
    )

    # 2-card bare play
    bare_play = next(
        p for p in plays
        if len(p.metadata["cards"]) == 2 and "A♠" in p.metadata["cards"] and "A♥" in p.metadata["cards"]
    )

    # Under The Psychic, 2-card play is fatal/disallowed or severely penalized, while 5-card play succeeds
    assert padded_play.heuristic_score > bare_play.heuristic_score * 3.0


def test_discard_held_card_protection_steel_and_gold():
    """Verify that discarding protected held-in-hand cards (Steel, Gold) is heavily penalized."""
    hand = [
        _card("A", "S"),
        _card("K", "S"),
        _card("2", "D", enhancement="steel"),  # Should not discard
        _card("3", "C", enhancement="gold"),   # Should not discard
        _card("4", "H"),                      # Safe to discard
    ]
    state = BalatroState(
        hand=hand,
        blind=Blind(name="Small Blind", chips=600, scored=0),
        jokers=[Joker(name="Joker", mult=4)],
        hands_left=3,
        discards_left=2,
    )

    actions = enumerate_legal_actions(state)
    discards = [a for a in actions if a.metadata.get("action_type") == "discard"]

    # Discarding safe 4H (index 4)
    safe_discard = next(d for d in discards if d.metadata["indices"] == [4])

    # Discarding Steel 2D (index 2)
    steel_discard = next(d for d in discards if d.metadata["indices"] == [2])

    assert safe_discard.heuristic_score > steel_discard.heuristic_score * 2.0


def test_discard_draw_odds_flush_hunting():
    """Verify that discarding off-suit cards to hunt a 4-card flush receives odds-weighted boost."""
    hand = [
        _card("A", "S"),
        _card("K", "S"),
        _card("9", "S"),
        _card("6", "S"),
        _card("2", "H"),  # Off-suit
        _card("3", "D"),  # Off-suit
    ]
    state = BalatroState(
        hand=hand,
        blind=Blind(name="Small Blind", chips=600, scored=0),
        jokers=[],
        hands_left=3,
        discards_left=2,
    )

    actions = enumerate_legal_actions(state)
    discards = [a for a in actions if a.metadata.get("action_type") == "discard"]

    # Discarding the two off-suit cards (indices 4, 5) keeping 4 Spades
    flush_chase_discard = next(d for d in discards if set(d.metadata["indices"]) == {4, 5})
    assert "追同花" in flush_chase_discard.description
    assert flush_chase_discard.metadata.get("draw_bonus", 0.0) > 0.0

    # Discarding 2 Spades (indices 0, 1) destroying the flush draw
    spade_dump_discard = next(d for d in discards if set(d.metadata["indices"]) == {0, 1})

    # Flush-chasing discard must score higher than ruining the 4-card flush
    assert flush_chase_discard.heuristic_score > spade_dump_discard.heuristic_score


def test_glass_lucky_and_high_card_protection_from_junk_dumping():
    """Verify that Glass, Lucky, Mult, Edition, and High face cards (A, K, Q, J) are protected from being dumped as junk."""
    hand = [
        _card("8", "S"),
        _card("8", "H"),
        _card("A", "D"),                         # High Card (Ace) - should not be dumped as junk
        _card("K", "C", enhancement="glass"),    # Glass King - critical scoring asset!
        _card("2", "C"),                         # Low rank white card - ideal junk
    ]
    state = BalatroState(
        hand=hand,
        blind=Blind(name="Small Blind", chips=600, scored=0),
        jokers=[Joker(name="Joker", mult=4)],
        hands_left=2,
        discards_left=0,
    )

    actions = enumerate_legal_actions(state)
    plays = [a for a in actions if a.metadata.get("action_type") == "play"]

    # Play Pair 8s dumping low 2C
    low_junk_play = next(
        p for p in plays
        if "8♠" in p.metadata["cards"] and "8♥" in p.metadata["cards"] and "2♣" in p.metadata["cards"]
        and "A♦" not in p.metadata["cards"] and "K♣" not in p.metadata["cards"]
    )

    # Play Pair 8s dumping Ace as junk
    ace_dump_play = next(
        p for p in plays
        if "8♠" in p.metadata["cards"] and "8♥" in p.metadata["cards"] and "A♦" in p.metadata["cards"]
        and "2♣" not in p.metadata["cards"] and "K♣" not in p.metadata["cards"]
    )

    # Play Pair 8s dumping Glass King as junk
    glass_dump_play = next(
        p for p in plays
        if "8♠" in p.metadata["cards"] and "8♥" in p.metadata["cards"] and "K♣" in p.metadata["cards"]
        and "2♣" not in p.metadata["cards"] and "A♦" not in p.metadata["cards"]
    )

    # Dumping isolated low 2C must score significantly higher than burning Ace or Glass card!
    assert low_junk_play.heuristic_score > ace_dump_play.heuristic_score * 1.5
    assert low_junk_play.heuristic_score > glass_dump_play.heuristic_score * 2.5


def test_pure_play_preferred_when_discards_left():
    """When discards_left > 0, pure hand plays without unnecessary junk dumping are preferred."""
    hand = [
        _card("9", "S"),
        _card("9", "H"),
        _card("3", "D"),
        _card("4", "C"),
    ]
    # State with 2 discards available
    st = BalatroState(
        hand=hand,
        blind=Blind(name="Big Blind", chips=600, scored=0),
        jokers=[Joker(name="Joker", mult=4)],
        hands_left=3,
        discards_left=2,
    )
    actions = enumerate_legal_actions(st)
    plays = [a for a in actions if a.metadata.get("action_type") == "play"]

    pure_pair = next(p for p in plays if len(p.metadata["cards"]) == 2 and set(p.metadata["cards"]) == {"9♠", "9♥"})
    padded_pair = next(p for p in plays if len(p.metadata["cards"]) == 3 and "9♠" in p.metadata["cards"] and "9♥" in p.metadata["cards"])

    # When discards remain, keeping the hand pure avoids burning cards unnecessarily
    assert pure_pair.heuristic_score > padded_pair.heuristic_score


def test_hack_and_fibonacci_card_protection_from_junk_dumping():
    """Cards that trigger Hack (2,3,4,5) or Fibonacci (A,2,3,5,8) must not be dumped as junk."""
    hand = [
        _card("9", "S"),
        _card("9", "H"),
        _card("3", "D"),  # Triggers Hack and Fibonacci
        _card("6", "C"),  # Ordinary isolated low rank card
    ]
    # State with Hack joker and 0 discards left
    st = BalatroState(
        hand=hand,
        blind=Blind(name="Small Blind", chips=600, scored=0),
        jokers=[Joker(name="Hack")],
        hands_left=2,
        discards_left=0,
    )
    actions = enumerate_legal_actions(st)
    plays = [a for a in actions if a.metadata.get("action_type") == "play"]

    # Play dumping the 6C
    dump_6_play = next(
        p for p in plays
        if "9♠" in p.metadata["cards"] and "9♥" in p.metadata["cards"] and "6♣" in p.metadata["cards"]
        and "3♦" not in p.metadata["cards"]
    )

    # Play dumping the 3D (which is protected by Hack)
    dump_3_play = next(
        p for p in plays
        if "9♠" in p.metadata["cards"] and "9♥" in p.metadata["cards"] and "3♦" in p.metadata["cards"]
        and "6♣" not in p.metadata["cards"]
    )

    assert dump_6_play.heuristic_score > dump_3_play.heuristic_score * 3.0


def test_even_steven_protection_from_junk_dumping():
    """When Even Steven is active, even ranks (2,4,6,8,10) are protected from being dumped as junk."""
    hand = [
        _card("K", "S"),
        _card("K", "H"),
        _card("4", "D"),  # Even rank (triggers Even Steven)
        _card("5", "C"),  # Odd rank (uncoordinated junk)
    ]
    st = BalatroState(
        hand=hand,
        blind=Blind(name="Big Blind", chips=800, scored=0),
        jokers=[Joker(name="Even Steven")],
        hands_left=2,
        discards_left=0,
    )
    actions = enumerate_legal_actions(st)
    plays = [a for a in actions if a.metadata.get("action_type") == "play"]

    dump_odd_play = next(
        p for p in plays
        if "K♠" in p.metadata["cards"] and "K♥" in p.metadata["cards"] and "5♣" in p.metadata["cards"]
        and "4♦" not in p.metadata["cards"]
    )
    dump_even_play = next(
        p for p in plays
        if "K♠" in p.metadata["cards"] and "K♥" in p.metadata["cards"] and "4♦" in p.metadata["cards"]
        and "5♣" not in p.metadata["cards"]
    )

    assert dump_odd_play.heuristic_score > dump_even_play.heuristic_score * 3.0


def test_suit_joker_protection_from_junk_dumping():
    """When a suit Joker (e.g. Lusty Joker for Hearts) is active, that suit is protected from junk dumping."""
    hand = [
        _card("10", "S"),
        _card("10", "D"),
        _card("3", "H"),  # Hearts card (triggers Lusty Joker)
        _card("3", "C"),  # Clubs card (unscored off-suit junk)
    ]
    st = BalatroState(
        hand=hand,
        blind=Blind(name="Big Blind", chips=800, scored=0),
        jokers=[Joker(name="Lusty Joker")],
        hands_left=2,
        discards_left=0,
    )
    actions = enumerate_legal_actions(st)
    plays = [a for a in actions if a.metadata.get("action_type") == "play"]

    dump_club_play = next(
        p for p in plays
        if "10♠" in p.metadata["cards"] and "10♦" in p.metadata["cards"] and "3♣" in p.metadata["cards"]
        and "3♥" not in p.metadata["cards"]
    )
    dump_heart_play = next(
        p for p in plays
        if "10♠" in p.metadata["cards"] and "10♦" in p.metadata["cards"] and "3♥" in p.metadata["cards"]
        and "3♣" not in p.metadata["cards"]
    )

    assert dump_club_play.heuristic_score > dump_heart_play.heuristic_score * 2.0


def test_straight_connector_protection_from_junk_dumping():
    """Cards forming a straight connector in hand are protected from being discarded or dumped as junk."""
    hand = [
        _card("K", "S"),
        _card("K", "H"),
        _card("5", "S"),  # Connector part (5-6-7)
        _card("6", "D"),  # Connector part (5-6-7)
        _card("7", "C"),  # Connector part (5-6-7)
        _card("2", "C"),  # Isolated low card
    ]
    st = BalatroState(
        hand=hand,
        blind=Blind(name="Big Blind", chips=800, scored=0),
        jokers=[Joker(name="Joker", mult=4)],
        hands_left=2,
        discards_left=0,
    )
    actions = enumerate_legal_actions(st)
    plays = [a for a in actions if a.metadata.get("action_type") == "play"]

    dump_isolated_2 = next(
        p for p in plays
        if "K♠" in p.metadata["cards"] and "K♥" in p.metadata["cards"] and "2♣" in p.metadata["cards"]
        and not any(c in p.metadata["cards"] for c in ("5♠", "6♦", "7♣"))
    )
    dump_connector_6 = next(
        p for p in plays
        if "K♠" in p.metadata["cards"] and "K♥" in p.metadata["cards"] and "6♦" in p.metadata["cards"]
        and not any(c in p.metadata["cards"] for c in ("5♠", "7♣", "2♣"))
    )

    assert dump_isolated_2.heuristic_score > dump_connector_6.heuristic_score * 1.5


def test_half_joker_protection_from_junk_dumping():
    """Verify that Half Joker (+20 Mult if hand <= 3) strictly vetoes padding with junk beyond 3 cards."""
    hand = [
        _card("10", "S"),
        _card("10", "D"),
        _card("2", "C"),
        _card("3", "H"),
        _card("4", "S"),
    ]
    st = BalatroState(
        hand=hand,
        blind=Blind(name="Big Blind", chips=2000, scored=0),
        jokers=[Joker(name="Half Joker")],
        hands_left=2,
        discards_left=0,
    )
    actions = enumerate_legal_actions(st)
    plays = [a for a in actions if a.metadata.get("action_type") == "play"]

    # 2-card pure play (triggers Half Joker)
    pure_pair_play = next(
        p for p in plays
        if len(p.metadata["cards"]) == 2 and "10♠" in p.metadata["cards"] and "10♦" in p.metadata["cards"]
    )

    # 4-card or 5-card play with junk dumped (breaks Half Joker)
    padded_plays = [
        p for p in plays
        if len(p.metadata["cards"]) > 3 and "10♠" in p.metadata["cards"] and "10♦" in p.metadata["cards"]
    ]
    assert len(padded_plays) > 0
    for p in padded_plays:
        assert pure_pair_play.heuristic_score > p.heuristic_score * 50.0




