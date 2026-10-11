"""Poker-hand detection for the Balatro advisor (vanilla rules)."""

from decidex.balatro import Card, base_values, detect_hand_type, enumerate_selections
from decidex.balatro.state import RANK_CHIPS


def c(rank, suit, **kw):
    return Card(rank=rank, suit=suit, **kw)


def test_base_values_vanilla():
    assert base_values("Flush Five") == (160, 16)
    assert base_values("Straight Flush") == (100, 8)
    assert base_values("Pair") == (10, 2)
    assert base_values("High Card") == (5, 1)


def test_royal_flush_is_straight_flush():
    hand = [c("A", "S"), c("K", "S"), c("Q", "S"), c("J", "S"), c("10", "S")]
    assert detect_hand_type(hand) == "Straight Flush"


def test_ace_low_wheel():
    hand = [c("A", "S"), c("2", "H"), c("3", "D"), c("4", "S"), c("5", "S")]
    assert detect_hand_type(hand) == "Straight"


def test_flush_house_and_flush_five():
    fh = [c("K", "S"), c("K", "S"), c("K", "S"), c("5", "S"), c("5", "S")]
    assert detect_hand_type(fh) == "Flush House"
    ff = [c("K", "S"), c("K", "S"), c("K", "S"), c("K", "S"), c("K", "S")]
    assert detect_hand_type(ff) == "Flush Five"


def test_five_of_a_kind_no_flush():
    hand = [c("7", "S"), c("7", "H"), c("7", "D"), c("7", "C"), c("7", "S")]
    assert detect_hand_type(hand) == "Five of a Kind"


def test_full_house_straight_flush_precedence():
    full = [c("9", "S"), c("9", "H"), c("9", "D"), c("4", "S"), c("4", "H")]
    assert detect_hand_type(full) == "Full House"
    sf = [c("9", "S"), c("8", "S"), c("7", "S"), c("6", "S"), c("5", "S")]
    assert detect_hand_type(sf) == "Straight Flush"


def test_pairs_trips_two_pair():
    assert detect_hand_type([c("7", "S"), c("7", "H")]) == "Pair"
    assert detect_hand_type([c("7", "S"), c("7", "H"), c("7", "D")]) == "Three of a Kind"
    assert detect_hand_type([c("7", "S"), c("7", "H"), c("2", "D"), c("2", "C")]) == "Two Pair"
    assert detect_hand_type([c("A", "S"), c("5", "H")]) == "High Card"


def test_wild_card_completes_flush():
    # 4 hearts + 1 wild = flush (wild counts as any suit).
    hand = [c("2", "H"), c("5", "H"), c("9", "H"), c("K", "H"), c("3", "D", enhancement="wild")]
    assert detect_hand_type(hand) == "Flush"


def test_rank_chip_values():
    assert RANK_CHIPS["A"] == 11
    assert RANK_CHIPS["K"] == 10
    assert RANK_CHIPS["7"] == 7


def test_enumerate_selections_counts():
    hand = [c("A", "S")] * 8
    sels = enumerate_selections(hand, max_played=5)
    # C(8,1)+C(8,2)+C(8,3)+C(8,4)+C(8,5)
    assert len(sels) == 8 + 28 + 56 + 70 + 56
