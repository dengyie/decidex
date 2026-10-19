"""
Tests for Balatro Boss Blind Catalog, Directives, and Safety Evaluation.
"""

from decidex.balatro import (
    BalatroAdvisor,
    BalatroState,
    Blind,
    Card,
    Joker,
    build_evidence,
    detect_active_constraints,
    evaluate_play_safety,
    simulate_play,
)
from decidex.balatro.constraints import BOSS_CATALOG, BossRule
from decidex.balatro.rules import choose_safe_default, enumerate_legal_actions


def _card(rank: str, suit: str) -> Card:
    return Card(rank=rank, suit=suit)


def test_boss_catalog_integrity():
    """Verify that BOSS_CATALOG defines key bosses with explicit directives and rules."""
    assert "The Psychic" in BOSS_CATALOG
    assert "The Eye" in BOSS_CATALOG
    assert "The Mouth" in BOSS_CATALOG
    assert "The Water" in BOSS_CATALOG
    assert "The Needle" in BOSS_CATALOG
    assert "The Ox" in BOSS_CATALOG
    assert "The Flint" in BOSS_CATALOG
    assert "The Plant" in BOSS_CATALOG
    assert "The Tooth" in BOSS_CATALOG
    assert "The Club" in BOSS_CATALOG
    assert "Verdant Leaf" in BOSS_CATALOG

    psychic = BOSS_CATALOG["The Psychic"]
    assert psychic.requires_exact_5_cards is True
    assert "5 张牌" in psychic.strategic_directive

    eye = BOSS_CATALOG["The Eye"]
    assert eye.no_repeat_hand_type is True

    mouth = BOSS_CATALOG["The Mouth"]
    assert mouth.single_hand_type_only is True


def test_the_psychic_scoring_and_safety():
    """The Psychic requires 5 cards: < 5 cards scores 0 and is fatal; 5 cards scores normally."""
    hand = [_card("K", "H"), _card("K", "S"), _card("2", "D"), _card("4", "C"), _card("7", "H")]
    st = BalatroState(
        hand=hand,
        blind=Blind(name="The Psychic", chips=1000),
    )
    constraints = detect_active_constraints([], blind_name="The Psychic")
    assert constraints.is_the_psychic is True

    # 1. Playing only the pair (2 cards) -> Fatal violation & 0 score
    bare_pair = [hand[0], hand[1]]
    score_2 = simulate_play(bare_pair, st)
    assert score_2.score == 0
    safety_2 = evaluate_play_safety(bare_pair, score_2.hand_type, constraints)
    assert safety_2.is_fatal_violation is True
    assert "必须打满 5 张牌" in safety_2.reason

    # 2. Playing the pair with 3 junk cards attached (5 cards total) -> Valid & scores!
    five_cards = hand
    score_5 = simulate_play(five_cards, st)
    assert score_5.score > 0
    safety_5 = evaluate_play_safety(five_cards, score_5.hand_type, constraints)
    assert safety_5.is_fatal_violation is False

    # 3. Safe default policy selects the 5-card play
    actions = enumerate_legal_actions(st)
    safe = choose_safe_default(actions, st)
    assert len(safe.metadata["indices"]) == 5
    assert safe.metadata["fatal_violation"] is False


def test_the_eye_no_repeat_hand_type():
    """The Eye forbids playing the same hand type more than once."""
    hand = [_card("K", "H"), _card("K", "S"), _card("Q", "D"), _card("Q", "C"), _card("2", "H")]
    st = BalatroState(
        hand=hand,
        blind=Blind(name="The Eye", chips=1000),
    )
    # Already played Pair this round
    constraints = detect_active_constraints([], blind_name="The Eye", played_hands={"Pair"})
    assert constraints.is_the_eye is True

    # Playing Pair again -> Fatal
    pair_play = [hand[0], hand[1]]
    safety_pair = evaluate_play_safety(pair_play, "Pair", constraints)
    assert safety_pair.is_fatal_violation is True
    assert "本回合已打过 'Pair'" in safety_pair.reason

    # Playing Two Pair -> Legal
    two_pair_play = [hand[0], hand[1], hand[2], hand[3]]
    safety_two_pair = evaluate_play_safety(two_pair_play, "Two Pair", constraints)
    assert safety_two_pair.is_fatal_violation is False


def test_the_mouth_single_hand_type():
    """The Mouth locks the first played hand type as the only legal hand type."""
    hand = [_card("K", "H"), _card("K", "S"), _card("Q", "D"), _card("Q", "C"), _card("2", "H")]
    # First played hand was Two Pair
    constraints = detect_active_constraints([], blind_name="The Mouth", played_hands={"Two Pair"})
    assert constraints.is_the_mouth is True

    # Playing Two Pair -> Legal
    two_pair_play = [hand[0], hand[1], hand[2], hand[3]]
    safety_two_pair = evaluate_play_safety(two_pair_play, "Two Pair", constraints)
    assert safety_two_pair.is_fatal_violation is False

    # Playing Pair -> Fatal
    pair_play = [hand[0], hand[1]]
    safety_pair = evaluate_play_safety(pair_play, "Pair", constraints)
    assert safety_pair.is_fatal_violation is True
    assert "限制本回合只能打出 'Two Pair'" in safety_pair.reason


def test_the_ox_money_wipe():
    """The Ox warns and heavily penalizes playing the most played hand if money > 0."""
    hand = [_card("K", "H"), _card("K", "S")]
    constraints = detect_active_constraints(
        [],
        blind_name="The Ox",
        most_played_hand="Pair",
        money=25,
    )
    assert constraints.is_the_ox is True

    safety = evaluate_play_safety(hand, "Pair", constraints)
    assert safety.is_fatal_violation is False
    assert safety.penalty_multiplier < 0.20
    assert "打出主力牌型 'Pair' 将导致 $25 全部清零" in safety.reason


def test_the_flint_halves_base_chips_and_mult():
    """The Flint halves the base chips and base mult of played hands."""
    hand = [_card("9", "H"), _card("9", "S")]
    st_normal = BalatroState(hand=hand, blind=Blind(name="Small Blind", chips=300))
    st_flint = BalatroState(hand=hand, blind=Blind(name="The Flint", chips=300))

    score_normal = simulate_play(hand, st_normal)
    score_flint = simulate_play(hand, st_flint)

    # Normal Pair: base (10, 2) + cards (9+9=18) -> 28 chips x 2 mult = 56
    assert score_normal.chips == 28
    assert score_normal.mult == 2.0

    # Flint Pair: base (5, 1) + cards (9+9=18) -> 23 chips x 1 mult = 23
    assert score_flint.chips == 23
    assert score_flint.mult == 1.0
    assert score_flint.score < score_normal.score


def test_suit_debuff_and_plant():
    """Test suit debuff (The Club) and face debuff (The Plant)."""
    club_card = _card("10", "C")
    st_club = BalatroState(hand=[club_card], blind=Blind(name="The Club", chips=300))
    score_club = simulate_play([club_card], st_club)
    # The Club debuffs clubs: card chips become 0, only base High Card (5) counts
    assert score_club.chips == 5

    face_card = _card("K", "H")
    st_plant = BalatroState(hand=[face_card], blind=Blind(name="The Plant", chips=300))
    score_plant = simulate_play([face_card], st_plant)
    # The Plant debuffs face cards: K chips (10) become 0, only base High Card (5) counts
    assert score_plant.chips == 5


def test_build_evidence_injects_boss_information():
    """build_evidence must inject rich boss_blind context for LLM DecisionEngine."""
    hand = [_card("A", "S"), _card("2", "H")]
    st = BalatroState(
        hand=hand,
        blind=Blind(name="The Psychic", chips=1500),
        money=18,
        ante=2,
    )
    evidence = build_evidence(st, 10)
    assert "boss_blind" in evidence
    boss_info = evidence["boss_blind"]
    assert boss_info["name"] == "The Psychic"
    assert boss_info["has_boss"] is True
    assert boss_info["requires_5_cards"] is True
    assert "必须打出 5 张牌" in boss_info["description"]
    assert "附带低价值杂牌补足 5 张牌" in boss_info["strategic_directive"]


def test_upcoming_boss_shop_adaptation_plant():
    """Upcoming boss is The Plant: Scary Face (face cards) should be penalized in shop."""
    from decidex.balatro.synergy import analyze_synergy, score_joker_candidate

    profile = analyze_synergy([])
    # Under normal circumstances, Scary Face (A-tier chips) is recommended
    adv_normal = score_joker_candidate("Scary Face", profile, cost=4, current_money=15, has_slot=True, ante=1)
    assert adv_normal.recommendation in ("强烈推荐", "值得考虑")
    assert adv_normal.score >= 60

    # With upcoming The Plant, Scary Face is heavily penalized
    adv_plant = score_joker_candidate("Scary Face", profile, cost=4, current_money=15, has_slot=True, ante=1, upcoming_boss="The Plant")
    assert adv_plant.score < adv_normal.score
    assert adv_plant.recommendation == "暂不推荐"
    assert "The Plant" in adv_plant.reason


def test_upcoming_boss_shop_adaptation_suit_debuff():
    """Upcoming boss is The Head (Hearts debuffed): Lusty Joker (Hearts) is penalized."""
    from decidex.balatro.synergy import analyze_synergy, score_joker_candidate

    profile = analyze_synergy([])
    adv_head = score_joker_candidate("Lusty Joker", profile, cost=4, current_money=15, has_slot=True, ante=1, upcoming_boss="The Head")
    assert "The Head" in adv_head.reason
    assert "红桃" in adv_head.reason
    assert adv_head.recommendation == "暂不推荐"


def test_upcoming_boss_shop_adaptation_needle():
    """Upcoming boss is The Needle (1 hand only): Burglar (+hands) & Ice Cream (+chips) receive priority boost."""
    from decidex.balatro.synergy import analyze_synergy, score_joker_candidate

    profile = analyze_synergy([])
    adv_burglar = score_joker_candidate("Burglar", profile, cost=5, current_money=20, has_slot=True, ante=2, upcoming_boss="The Needle")
    assert "The Needle" in adv_burglar.reason
    assert adv_burglar.score > 60

    adv_icecream = score_joker_candidate("Ice Cream", profile, cost=5, current_money=20, has_slot=True, ante=2, upcoming_boss="The Needle")
    assert "The Needle" in adv_icecream.reason
    assert adv_icecream.recommendation == "强烈推荐"


def test_upcoming_boss_in_evidence():
    """build_evidence exposes upcoming_boss from blind for predictive planning."""
    st = BalatroState(
        hand=[_card("A", "S")],
        blind=Blind(name="Small Blind", chips=300, upcoming_boss="The Water"),
        money=20,
        ante=2,
    )
    evidence = build_evidence(st, 5)
    assert evidence["blind"]["upcoming_boss"] == "The Water"
    assert evidence["boss_blind"]["upcoming_boss"] == "The Water"

