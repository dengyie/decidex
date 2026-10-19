"""
Tests for Balatro Consumables and Booster Packs handling in DecideX.
Verifies intelligent ranking, priority ordering, pack card selection, and fallback safety.
"""

from __future__ import annotations

import pytest

from decidex.balatro.advisor import BalatroAdvisor, build_evidence
from decidex.balatro.rules import choose_safe_default, enumerate_legal_actions
from decidex.balatro.state import BalatroState, Blind, Card, Consumable, Joker
from decidex.providers.mock import MockReplayProvider


def _make_sample_state(
    consumables=None,
    state_name="SELECTING_HAND",
    money=12,
    ante=1,
    pack_cards=None,
    can_skip_booster=False,
    jokers=None,
    shop=None,
) -> BalatroState:
    return BalatroState(
        hand=[
            Card(rank="A", suit="Spades"),
            Card(rank="K", suit="Spades"),
            Card(rank="Q", suit="Spades"),
            Card(rank="J", suit="Spades"),
            Card(rank="10", suit="Spades"),
            Card(rank="2", suit="Hearts"),
            Card(rank="3", suit="Clubs"),
            Card(rank="4", suit="Diamonds"),
        ],
        blind=Blind(name="Big Blind", chips=600, scored=0),
        jokers=jokers or [Joker(name="Joker", mult=4)],
        consumables=consumables or [],
        hands_left=4,
        discards_left=3,
        money=money,
        ante=ante,
        state_name=state_name,
        pack_cards=pack_cards or [],
        can_skip_booster=can_skip_booster,
        shop=shop,
    )


def test_state_serialization_with_pack_cards():
    state = _make_sample_state(
        pack_cards=[
            {"index": 1, "name": "Jupiter", "cost": 0, "type": "Planet", "set": "Planet"},
            {"index": 2, "name": "Earth", "cost": 0, "type": "Planet", "set": "Planet"},
        ],
        can_skip_booster=True,
    )
    d = state.to_dict()
    assert len(d["pack_cards"]) == 2
    assert d["pack_cards"][0]["name"] == "Jupiter"
    assert d["can_skip_booster"] is True

    reconstituted = BalatroState.from_dict(d)
    assert len(reconstituted.pack_cards) == 2
    assert reconstituted.pack_cards[1]["name"] == "Earth"
    assert reconstituted.can_skip_booster is True


def test_planet_consumable_high_priority():
    # Planet card should score highly and be prioritized before playing
    state = _make_sample_state(
        consumables=[Consumable(name="Jupiter", type="planet")],
    )
    actions = enumerate_legal_actions(state)
    use_actions = [a for a in actions if a.metadata.get("action_type") == "use_consumable"]
    assert len(use_actions) == 1
    jupiter_action = use_actions[0]
    assert jupiter_action.heuristic_score >= 5.8
    assert "Jupiter" in jupiter_action.description

    # Safe default should recommend using the Planet card first to boost hand level
    safe = choose_safe_default(actions, state)
    assert safe.metadata.get("action_type") == "use_consumable"
    assert safe.metadata.get("name") == "Jupiter"


def test_the_hermit_economy_scaling():
    # When money >= 10, The Hermit gives max value and gets high priority
    rich_state = _make_sample_state(
        consumables=[Consumable(name="The Hermit", type="tarot")],
        money=15,
    )
    rich_actions = enumerate_legal_actions(rich_state)
    hermit_rich = [a for a in rich_actions if a.metadata.get("name") == "The Hermit"][0]
    assert hermit_rich.heuristic_score >= 5.5
    assert choose_safe_default(rich_actions, rich_state).metadata.get("name") == "The Hermit"

    # When money is low (< 5), The Hermit is scored lower to preserve it for later
    poor_state = _make_sample_state(
        consumables=[Consumable(name="The Hermit", type="tarot")],
        money=3,
    )
    poor_actions = enumerate_legal_actions(poor_state)
    hermit_poor = [a for a in poor_actions if a.metadata.get("name") == "The Hermit"][0]
    assert hermit_poor.heuristic_score <= 3.0


def test_temperance_and_wheel_of_fortune():
    # Temperance with multiple jokers
    jokers = [
        Joker(name="Joker", mult=4),
        Joker(name="Cavendish", xmult=3.0),
        Joker(name="Bull", chips=40),
    ]
    state = _make_sample_state(
        consumables=[
            Consumable(name="Temperance", type="tarot"),
            Consumable(name="The Wheel of Fortune", type="tarot"),
        ],
        jokers=jokers,
    )
    actions = enumerate_legal_actions(state)
    temperance_act = [a for a in actions if a.metadata.get("name") == "Temperance"][0]
    assert temperance_act.heuristic_score >= 5.0

    wheel_act = [a for a in actions if a.metadata.get("name") == "The Wheel of Fortune"][0]
    assert wheel_act.heuristic_score >= 4.5


def test_planet_pack_card_selection():
    # In PLANET_PACK, player must pick a planet card or skip
    pack_cards = [
        {"index": 1, "name": "Mercury", "cost": 0, "type": "Planet", "set": "Planet"},
        {"index": 2, "name": "Jupiter", "cost": 0, "type": "Planet", "set": "Planet"},
        {"index": 3, "name": "Pluto", "cost": 0, "type": "Planet", "set": "Planet"},
    ]
    state = _make_sample_state(
        state_name="PLANET_PACK",
        pack_cards=pack_cards,
        can_skip_booster=True,
    )
    actions = enumerate_legal_actions(state)
    assert any(a.candidate_id == "select_pack_card:1" for a in actions)
    assert any(a.candidate_id == "select_pack_card:2" for a in actions)
    assert any(a.candidate_id == "select_pack_card:3" for a in actions)
    assert any(a.candidate_id == "skip_booster" for a in actions)

    # Jupiter (Flush) receives higher heuristic priority than Pluto (High Card)
    jup = [a for a in actions if a.candidate_id == "select_pack_card:2"][0]
    pluto = [a for a in actions if a.candidate_id == "select_pack_card:3"][0]
    assert jup.heuristic_score > pluto.heuristic_score

    # Safe default chooses top planet card
    safe = choose_safe_default(actions, state)
    assert safe.candidate_id == "select_pack_card:2"


def test_buffoon_pack_card_synergy_selection():
    # In BUFFOON_PACK, player picks a Joker aligned with missing pillar
    pack_cards = [
        {"index": 1, "name": "Half Joker", "cost": 0, "type": "Joker", "set": "Joker"},
        {"index": 2, "name": "Cavendish", "cost": 0, "type": "Joker", "set": "Joker"},
    ]
    # Current team has Flat Mult only, needs X-Mult
    state = _make_sample_state(
        state_name="BUFFOON_PACK",
        pack_cards=pack_cards,
        can_skip_booster=True,
        jokers=[Joker(name="Gros Michel", mult=15)],
    )
    actions = enumerate_legal_actions(state)
    safe = choose_safe_default(actions, state)
    # Cavendish (X-Mult) fulfills the critical missing pillar and should be chosen
    assert safe.metadata.get("action_type") == "select_pack_card"
    assert safe.metadata.get("name") == "Cavendish"


def test_shop_booster_purchase_evaluation():
    shop = {
        "cards": [],
        "boosters": [
            {"index": 1, "name": "Celestial Pack", "cost": 4, "type": "booster"},
            {"index": 2, "name": "Buffoon Pack", "cost": 6, "type": "booster"},
        ],
    }
    # With healthy economy ($30), buying a $4 booster leaves $26 (preserving $5 max interest)
    state = _make_sample_state(
        state_name="SHOP",
        money=30,
        shop=shop,
    )
    actions = enumerate_legal_actions(state)
    booster_buys = [a for a in actions if a.metadata.get("action_type") == "buy_booster"]
    assert len(booster_buys) == 2
    assert booster_buys[0].metadata.get("budget_allowed") is True

    # With poverty ($3), booster buy should be penalized
    poor_state = _make_sample_state(
        state_name="SHOP",
        money=3,
        shop=shop,
    )
    poor_actions = enumerate_legal_actions(poor_state)
    poor_booster = [a for a in poor_actions if a.metadata.get("action_type") == "buy_booster"][0]
    assert poor_booster.metadata.get("budget_allowed") is False


@pytest.mark.asyncio
async def test_advisor_evidence_and_pack_recommendation():
    pack_cards = [
        {"index": 1, "name": "The Hermit", "cost": 0, "type": "Tarot", "set": "Tarot"},
        {"index": 2, "name": "The Fool", "cost": 0, "type": "Tarot", "set": "Tarot"},
    ]
    state = _make_sample_state(
        state_name="TAROT_PACK",
        money=20,
        pack_cards=pack_cards,
        can_skip_booster=True,
    )

    evidence = build_evidence(state, 3)
    assert evidence["pack_cards"] == ["The Hermit", "The Fool"]
    assert evidence["can_skip_booster"] is True

    def solver(payload, question):
        return "select_pack_card:1", 0.95, {"select_pack_card:1": 0.95, "select_pack_card:2": 0.05}

    advisor = BalatroAdvisor(provider=MockReplayProvider(solver_fn=solver))
    rec = await advisor.recommend(state)
    assert rec.candidate_id == "select_pack_card:1"
    assert rec.action_type == "select_pack_card"


def test_targeted_tarot_hand_selection():
    # Hand: A♠, K♠, Q♠, J♠, 10♠, 2♥, 3♣, 4♦
    state = _make_sample_state(
        consumables=[
            Consumable(name="The Hanged Man", type="tarot"),
            Consumable(name="Death", type="tarot"),
            Consumable(name="The Chariot", type="tarot"),
        ],
    )
    actions = enumerate_legal_actions(state)

    # 1. The Hanged Man targets 2 worst unenhanced low-rank cards (2♥, 3♣ at indices 5, 6)
    hanged = [a for a in actions if a.metadata.get("name") == "The Hanged Man"][0]
    assert hanged.heuristic_score >= 5.0
    targets_hanged = hanged.metadata.get("target_indices", [])
    assert len(targets_hanged) == 2
    assert targets_hanged == [5, 6]  # 2♥ and 3♣

    # 2. Death targets [worst, best] -> 2♥ (index 5) and A♠ (index 0)
    death = [a for a in actions if a.metadata.get("name") == "Death"][0]
    assert death.heuristic_score >= 5.0
    targets_death = death.metadata.get("target_indices", [])
    assert len(targets_death) == 2
    assert targets_death == [5, 0]  # worst=2♥, best=A♠

    # 3. The Chariot gives Steel Card (x1.5 Mult held in hand) -> targets low-rank kept card (2♥ at index 5)
    chariot = [a for a in actions if a.metadata.get("name") == "The Chariot"][0]
    assert chariot.heuristic_score >= 5.0
    targets_chariot = chariot.metadata.get("target_indices", [])
    assert targets_chariot == [5]  # 2♥ kept in hand for passive x1.5 Mult

    # 3b. When Baron is present, The Chariot prioritizes King (K♠ at index 1)
    state_baron = _make_sample_state(
        consumables=[Consumable(name="The Chariot", type="tarot")],
        jokers=[Joker(name="Baron")],
    )
    actions_baron = enumerate_legal_actions(state_baron)
    chariot_baron = [a for a in actions_baron if a.metadata.get("name") == "The Chariot"][0]
    assert chariot_baron.metadata.get("target_indices") == [1]  # K♠

    # 4. Justice gives Glass Card (2x Mult scored) -> targets highest rank card (A♠ at index 0)
    state_justice = _make_sample_state(
        consumables=[Consumable(name="Justice", type="tarot")],
    )
    actions_justice = enumerate_legal_actions(state_justice)
    justice_act = [a for a in actions_justice if a.metadata.get("name") == "Justice"][0]
    assert justice_act.metadata.get("target_indices") == [0]  # A♠

    # 5. Suit converter (The Sun -> Hearts) targets off-suit cards
    state_sun = _make_sample_state(
        consumables=[Consumable(name="The Sun", type="tarot")],
    )
    actions_sun = enumerate_legal_actions(state_sun)
    sun_act = [a for a in actions_sun if a.metadata.get("name") == "The Sun"][0]
    assert len(sun_act.metadata.get("target_indices", [])) == 3

    # 6. Spectral Cryptid duplicates best card (A♠ at index 0)
    state_cryptid = _make_sample_state(
        consumables=[Consumable(name="Cryptid", type="spectral")],
    )
    actions_cryptid = enumerate_legal_actions(state_cryptid)
    cryptid_act = [a for a in actions_cryptid if a.metadata.get("name") == "Cryptid"][0]
    assert cryptid_act.heuristic_score >= 6.0
    assert cryptid_act.metadata.get("target_indices") == [0]


def test_extended_tarot_and_spectral_targeting():
    # Test The Devil, The Tower, The Magician, Black Hole, Aura, Deja Vu, Trance, Talisman, Medium
    state = _make_sample_state(
        consumables=[
            Consumable(name="The Devil", type="tarot"),
            Consumable(name="The Tower", type="tarot"),
            Consumable(name="The Magician", type="tarot"),
            Consumable(name="Black Hole", type="spectral"),
            Consumable(name="Deja Vu", type="spectral"),
            Consumable(name="Trance", type="spectral"),
        ]
    )
    actions = enumerate_legal_actions(state)

    # 1. The Devil (Gold Card $3 in hand) -> targets low card (2♥ at index 5)
    devil = [a for a in actions if a.metadata.get("name") == "The Devil"][0]
    assert devil.metadata.get("target_indices") == [5]

    # 2. The Tower (Stone Card +50) -> targets low junk card (2♥ at index 5)
    tower = [a for a in actions if a.metadata.get("name") == "The Tower"][0]
    assert tower.metadata.get("target_indices") == [5]

    # 3. The Magician (Lucky Card) -> targets top 2 cards (A♠, K♠ at [0, 1])
    magician = [a for a in actions if a.metadata.get("name") == "The Magician"][0]
    assert magician.metadata.get("target_indices") == [0, 1]

    # 4. Black Hole (upgrade all hands) -> score 6.2, highest priority
    black_hole = [a for a in actions if a.metadata.get("name") == "Black Hole"][0]
    assert black_hole.heuristic_score >= 6.2
    safe = choose_safe_default(actions, state)
    assert safe.metadata.get("name") == "Black Hole"

    # 5. Deja Vu (Red Seal) -> targets top scoring card (A♠ at index 0)
    deja_vu = [a for a in actions if a.metadata.get("name") == "Deja Vu"][0]
    assert deja_vu.metadata.get("target_indices") == [0]

    # 6. Trance (Blue Seal) -> targets low card kept in hand (2♥ at index 5)
    trance = [a for a in actions if a.metadata.get("name") == "Trance"][0]
    assert trance.metadata.get("target_indices") == [5]


def test_shop_voucher_budget_and_priority():
    shop = {
        "cards": [],
        "boosters": [],
        "vouchers": [
            {"index": 1, "name": "Antimatter", "cost": 10},
            {"index": 2, "name": "Blank", "cost": 10},
        ],
    }
    # With $35 money, buying $10 voucher leaves $25 (interest line intact)
    rich_state = _make_sample_state(
        state_name="SHOP",
        money=35,
        shop=shop,
    )
    actions = enumerate_legal_actions(rich_state)
    voucher_acts = [a for a in actions if a.metadata.get("action_type") == "buy_voucher"]
    assert len(voucher_acts) == 2

    antimatter = [a for a in voucher_acts if a.metadata.get("name") == "Antimatter"][0]
    blank = [a for a in voucher_acts if a.metadata.get("name") == "Blank"][0]
    assert antimatter.metadata.get("budget_allowed") is True
    assert antimatter.heuristic_score > blank.heuristic_score

    # Safe default in rich shop chooses top tier voucher
    safe = choose_safe_default(actions, rich_state)
    assert safe.metadata.get("action_type") == "buy_voucher"
    assert safe.metadata.get("name") == "Antimatter"


def test_shop_full_joker_replacement_sell():
    # 5 full jokers including an exhausted/low value joker
    owned_jokers = [
        Joker(name="Ice Cream", chips=15),  # Severely depleted (score <= 48)
        Joker(name="Cavendish", xmult=3.0),
        Joker(name="Blueprint"),
        Joker(name="Brainstorm"),
        Joker(name="Bull", chips=100),
    ]
    # S-Tier joker in shop
    shop = {
        "cards": [
            {"index": 1, "name": "Card Sharp", "cost": 6, "xmult": 3.0},
        ],
        "boosters": [],
        "vouchers": [],
    }
    state = _make_sample_state(
        state_name="SHOP",
        money=28,
        jokers=owned_jokers,
        shop=shop,
    )
    actions = enumerate_legal_actions(state)
    sell_acts = [a for a in actions if a.metadata.get("action_type") == "sell_joker"]
    assert len(sell_acts) == 1
    assert sell_acts[0].metadata.get("sold_joker") == "Ice Cream"
    assert sell_acts[0].metadata.get("target_joker") == "Card Sharp"
    assert sell_acts[0].heuristic_score >= 5.0

    # Safe default chooses selling weakest joker to make room for god-tier candidate
    safe = choose_safe_default(actions, state)
    assert safe.metadata.get("action_type") == "sell_joker"
    assert safe.metadata.get("sold_joker") == "Ice Cream"


def test_shop_reroll_generation():
    shop = {
        "cards": [],
        "boosters": [],
        "vouchers": [],
        "reroll_cost": 5,
    }
    # 1. Wealthy player (> $25 + reroll cost) with empty/mediocre shop gets reroll action
    rich_state = _make_sample_state(
        state_name="SHOP",
        money=32,
        shop=shop,
    )
    rich_actions = enumerate_legal_actions(rich_state)
    rerolls = [a for a in rich_actions if a.metadata.get("action_type") == "reroll_shop"]
    assert len(rerolls) == 1
    assert rerolls[0].heuristic_score >= 3.8

    # 2. Tight funds ($18) should NOT reroll to protect $15 interest line
    tight_state = _make_sample_state(
        state_name="SHOP",
        money=18,
        shop=shop,
        ante=3,
        jokers=[Joker(name="Joker", mult=4)],
    )
    tight_actions = enumerate_legal_actions(tight_state)
    assert not any(a.metadata.get("action_type") == "reroll_shop" for a in tight_actions)


def test_spectral_pack_evaluation_ankh_and_immolate():
    pack_cards = [
        {"index": 1, "name": "Immolate", "cost": 0, "type": "Spectral", "set": "Spectral"},
        {"index": 2, "name": "Ankh", "cost": 0, "type": "Spectral", "set": "Spectral"},
        {"index": 3, "name": "Black Hole", "cost": 0, "type": "Spectral", "set": "Spectral"},
    ]
    # Scenario A: Player has only 1 god-tier joker (Cavendish)
    single_joker_state = _make_sample_state(
        state_name="SPECTRAL_PACK",
        pack_cards=pack_cards,
        can_skip_booster=True,
        jokers=[Joker(name="Cavendish", xmult=3.0)],
    )
    actions = enumerate_legal_actions(single_joker_state)
    black_hole = [a for a in actions if a.metadata.get("name") == "Black Hole"][0]
    immolate = [a for a in actions if a.metadata.get("name") == "Immolate"][0]
    ankh = [a for a in actions if a.metadata.get("name") == "Ankh"][0]

    assert black_hole.heuristic_score >= 7.0
    assert immolate.heuristic_score >= 6.8
    assert ankh.heuristic_score >= 7.0  # Ankh is godly with 1 joker!

    # Scenario B: Player has 4 established jokers -> Ankh becomes destructive suicide!
    multi_joker_state = _make_sample_state(
        state_name="SPECTRAL_PACK",
        pack_cards=pack_cards,
        can_skip_booster=True,
        jokers=[
            Joker(name="Cavendish", xmult=3.0),
            Joker(name="Blueprint"),
            Joker(name="Brainstorm"),
            Joker(name="Bull", chips=100),
        ],
    )
    multi_actions = enumerate_legal_actions(multi_joker_state)
    ankh_multi = [a for a in multi_actions if a.metadata.get("name") == "Ankh"][0]
    assert ankh_multi.heuristic_score <= 1.0  # Heavily penalized to prevent squad destruction!


def test_standard_pack_card_enhancement_and_seal_evaluation():
    pack_cards = [
        {"index": 1, "name": "Ace of Spades", "rank": "A", "suit": "S", "edition": "poly", "seal": "red", "enhancement": "steel"},
        {"index": 2, "name": "2 of Hearts", "rank": "2", "suit": "H"},
    ]
    state = _make_sample_state(
        state_name="STANDARD_PACK",
        pack_cards=pack_cards,
        can_skip_booster=True,
    )
    actions = enumerate_legal_actions(state)
    god_card = [a for a in actions if a.candidate_id == "select_pack_card:1"][0]
    junk_card = [a for a in actions if a.candidate_id == "select_pack_card:2"][0]

    assert god_card.heuristic_score >= 9.0  # Polychrome + Red Seal + Steel
    assert junk_card.heuristic_score < 3.0  # White 2 of Hearts penalized
    safe = choose_safe_default(actions, state)
    assert safe.candidate_id == "select_pack_card:1"


def test_the_fool_usability_gate():
    # When The Fool has can_use=False (e.g. no prior tarot/planet was used this run),
    # it must receive a heavily discounted heuristic score and never be chosen as safe default
    state_disabled = _make_sample_state(
        consumables=[Consumable(name="The Fool", type="tarot", can_use=False)],
    )
    actions_disabled = enumerate_legal_actions(state_disabled)
    fool_disabled = [a for a in actions_disabled if a.metadata.get("name") == "The Fool"][0]
    assert fool_disabled.heuristic_score <= 1.0
    safe_disabled = choose_safe_default(actions_disabled, state_disabled)
    assert safe_disabled.metadata.get("name") != "The Fool"

    # When The Fool is usable (can_use=True), it retains high heuristic priority
    state_enabled = _make_sample_state(
        consumables=[Consumable(name="The Fool", type="tarot", can_use=True)],
    )
    actions_enabled = enumerate_legal_actions(state_enabled)
    fool_enabled = [a for a in actions_enabled if a.metadata.get("name") == "The Fool"][0]
    assert fool_enabled.heuristic_score >= 5.0



