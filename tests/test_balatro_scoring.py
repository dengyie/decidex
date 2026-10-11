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


def test_high_card_ace_bare():
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
    # High card 5 + (5+10+9) = 29 chips; mult 1 + 3 + 3 (two diamonds) = 7.
    assert (r.hand_type, r.chips, r.mult) == ("High Card", 29, 7.0)
    assert r.score == 29 * 7


def test_bonus_foil_enhancements():
    hand = [Card(rank="5", suit="D", enhancement="bonus", edition="foil"), Card(rank="9", suit="C")]
    r = simulate_play([hand[0], hand[1]], state(hand))
    # 5 base + (5+30+50) + 9 = 99 chips; High Card mult 1.
    assert (r.hand_type, r.chips) == ("High Card", 99)


def test_glass_and_holo_xmult():
    hand = [Card(rank="K", suit="H", enhancement="glass", edition="holo")]
    r = simulate_play([hand[0]], state(hand))
    # High card: chips 5+10=15; mult (1 + 10 holo) x2 glass = 22 -> 330.
    assert (r.chips, round(r.mult, 1), r.score) == (15, 22.0, 330)


def test_red_seal_retriggers():
    hand = [Card(rank="K", suit="H"), Card(rank="3", suit="S", enhancement="mult", seal="red")]
    r = simulate_play([hand[0], hand[1]], state(hand))
    # High card: chips 5 + (10) + (3)*2 retriggers = 21; mult 1 + 4 + 4 = 9.
    assert (r.hand_type, r.chips, r.mult) == ("High Card", 21, 9.0)


def test_steel_held_in_hand():
    hand = [Card(rank="2", suit="H"), Card(rank="9", suit="C"), Card(rank="K", suit="H", enhancement="steel")]
    r = simulate_play([hand[0], hand[1]], state(hand))
    # High card 5+2+9=16 chips; mult (1 x2 held steel) = 2 -> 32.
    assert (r.chips, r.mult, r.score) == (16, 2.0, 32)


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
