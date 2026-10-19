"""Deterministic score simulation for the Balatro advisor."""

from decidex.balatro import BalatroState, Blind, Card, Joker, simulate_play


def state(hand, jokers=None, blind_chips=600, scored=0, discards_left=3, money=4):
    return BalatroState(
        hand=hand,
        blind=Blind(name="T", type="big", chips=blind_chips, scored=scored),
        jokers=jokers or [],
        hands_left=4, discards_left=discards_left, money=money,
    )


def plain(specs):
    return [Card(rank=r, suit=s) for r, s in specs]


def test_pair_filtering_scores_only_pair_cards_and_card_hooks():
    hand = [
        Card(rank="A", suit="S"),
        Card(rank="A", suit="H", enhancement="mult", seal="red"),
        Card(rank="K", suit="D", enhancement="bonus", seal="red"),
    ]
    r = simulate_play(hand, state(hand, [Joker(name="Scary Face")]))
    # Pair scores its two aces. The king kicker's bonus, retrigger and hook do not score.
    assert r.chips == 10 + 11 + 22
    assert r.mult == 2 + 8
    assert not any("K♦" in trigger for trigger in r.triggered)


def test_two_pair_filtering_ignores_kicker_enhancements_and_retriggers():
    hand = [
        Card(rank="K", suit="S"), Card(rank="K", suit="H"),
        Card(rank="8", suit="D"), Card(rank="8", suit="C"),
        Card(rank="A", suit="S", enhancement="bonus", seal="red"),
    ]
    r = simulate_play(hand, state(hand))
    assert r.chips == 20 + 10 + 10 + 8 + 8
    assert r.mult == 2


def test_psychic_counts_selected_cards_even_when_only_pair_scores():
    hand = [
        Card(rank="A", suit="S"), Card(rank="A", suit="H"),
        Card(rank="2", suit="C"), Card(rank="4", suit="D"), Card(rank="7", suit="S"),
    ]
    result = simulate_play(hand, state(hand, blind_chips=1000))
    assert result.hand_type == "Pair"
    assert result.score > 0

    psychic_state = state(hand, blind_chips=1000)
    psychic_state.blind.name = "The Psychic"
    psychic_result = simulate_play(hand[:4], psychic_state)
    assert psychic_result.score == 0
    assert "The Psychic" in psychic_result.triggered[0]


    hand = plain([("A", "S"), ("5", "H")])
    r = simulate_play([hand[0]], state(hand))
    # High card 5 base + 11 chips = 16 x 1.
    assert (r.hand_type, r.chips, r.mult, r.score) == ("High Card", 16, 1.0, 16)


def test_pair_with_static_joker():
    hand = plain([("7", "S"), ("7", "H"), ("2", "D")])
    jokers = [Joker(name="Joker", mult=4)]
    r = simulate_play([hand[0], hand[1]], state(hand, jokers))
    # Pair 10 base + 7+7 chips = 24; mult 2 + 4 = 6 -> 144.
    assert r.hand_type == "Pair"
    assert (r.chips, r.mult, r.score) == (24, 6.0, 144)


def test_greedy_joker_per_diamond():
    hand = plain([("5", "D"), ("J", "D"), ("9", "C")])
    jokers = [Joker(name="Greedy Joker")]
    r = simulate_play([hand[0], hand[1], hand[2]], state(hand, jokers))
    # High Card scores only the highest card (J): 5 base + 10 chips, plus Greedy's +3 mult.
    assert (r.hand_type, r.chips, r.mult) == ("High Card", 15, 4.0)
    assert r.score == 60


def test_bonus_foil_enhancements():
    hand = [Card(rank="5", suit="D", enhancement="bonus", edition="foil"), Card(rank="9", suit="C")]
    r = simulate_play([hand[0], hand[1]], state(hand))
    # High card scores only the highest 9: base 5 + 9 chips.
    assert (r.hand_type, r.chips) == ("High Card", 14)


def test_glass_and_holo_xmult():
    hand = [Card(rank="K", suit="H", enhancement="glass", edition="holo")]
    r = simulate_play([hand[0]], state(hand))
    # High card: chips 5+10=15; mult (1 + 10 holo) x2 glass = 22 -> 330.
    assert (r.chips, round(r.mult, 1), r.score) == (15, 22.0, 330)


def test_red_seal_retriggers():
    hand = [
        Card(rank="3", suit="H", enhancement="mult", seal="red"),
        Card(rank="3", suit="S"),
        Card(rank="K", suit="D"),
    ]
    r = simulate_play(hand, state(hand))
    # Both pair cards score (3+3 for red seal, 3 for 3S = 9 card chips + 10 base = 19); the red-sealed Mult card retriggers its enhancement.
    # The King kicker does not score chips.
    assert (r.hand_type, r.chips, r.mult) == ("Pair", 19, 10.0)


def test_steel_held_in_hand():
    hand = [Card(rank="2", suit="H"), Card(rank="9", suit="C"), Card(rank="K", suit="H", enhancement="steel")]
    r = simulate_play([hand[0], hand[1]], state(hand))
    # High Card scores only the highest 9; the held Steel card doubles mult.
    assert (r.chips, r.mult, r.score) == (14, 2.0, 28)


def test_baron_held_king_xmult():
    hand = [Card(rank="9", suit="C"), Card(rank="K", suit="S")]
    jokers = [Joker(name="Baron")]
    r = simulate_play([hand[0]], state(hand, jokers))
    # High card 5+9=14 chips; held K triggers Baron x1.5 -> mult 1.5.
    assert (r.chips, round(r.mult, 2)) == (14, 1.5)


def test_banner_scales_with_discards():
    hand = plain([("A", "S")])
    jokers = [Joker(name="Banner")]
    r3 = simulate_play([hand[0]], state(hand, jokers, discards_left=3))
    r0 = simulate_play([hand[0]], state(hand, jokers, discards_left=0))
    # Banner: +30 chips per remaining discard.
    assert r3.chips == 11 + 5 + 90
    assert r0.chips == 11 + 5


def test_unknown_joker_no_effect():
    hand = plain([("7", "H"), ("7", "D")])
    jokers = [Joker(name="Some Modded Joker", chips=0, mult=0)]
    r = simulate_play([hand[0], hand[1]], state(hand, jokers))
    assert (r.chips, r.mult) == (24, 2.0)


def test_blackboard_checks_held_not_played():
    """Blackboard x3 只看留下的手牌(原版语义),不看打出的牌。"""
    hand = [
        Card(rank="A", suit="S"),                      # 打出
        Card(rank="7", suit="S"),                      # 打出
        Card(rank="2", suit="H"),                      # 留手(红心)-> Blackboard 不触发
        Card(rank="K", suit="C"),                      # 留手
    ]
    jokers = [Joker(name="Blackboard")]
    r = simulate_play([hand[0], hand[1]], state(hand, jokers))
    assert r.mult == 1.0                                # High Card 基础 1,无 x3

    hand2 = [
        Card(rank="A", suit="S"),
        Card(rank="7", suit="S"),
        Card(rank="2", suit="C"),                       # 留手全黑桃/梅花
        Card(rank="K", suit="S"),
    ]
    r2 = simulate_play([hand2[0], hand2[1]], state(hand2, jokers))
    assert r2.mult == 3.0                               # 1 x3
