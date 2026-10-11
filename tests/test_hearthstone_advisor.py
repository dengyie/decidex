"""
Tests for the DecideX Hearthstone advisor (read-only best-play recommender).
Covers the deterministic legality rules and the fallback path through the engine.
"""

import pytest

from decidex.hearthstone import BoardState, HandCard, HearthstoneAdvisor, Hero, Minion
from decidex.hearthstone.rules import enumerate_legal_actions
from decidex.providers.mock import MockReplayProvider


def _base_state() -> BoardState:
    return BoardState(
        turn=5,
        mana=5,
        max_mana=5,
        my_hero=Hero(name="MyHero", health=30, max_health=30),
        opp_hero=Hero(name="OppHero", health=30, max_health=30),
    )


def _ids(candidates):
    return [c.candidate_id for c in candidates]


# --- legality rules ---------------------------------------------------------

def test_mana_gate_blocks_expensive_cards():
    state = _base_state()
    state.mana = 3
    state.hand = [HandCard(id="expensive", name="Big Drop", cost=7, card_type="minion", attack=7, health=7)]
    ids = _ids(enumerate_legal_actions(state))
    assert not any(i.startswith("play:expensive") for i in ids)
    assert "end_turn" in ids


def test_summoning_sickness_prevents_attack():
    state = _base_state()
    state.my_minions = [
        Minion(id="fresh", name="Fresh", attack=3, health=3, max_health=3, can_attack=False),
        Minion(id="ready", name="Ready", attack=3, health=3, max_health=3, can_attack=True),
    ]
    ids = _ids(enumerate_legal_actions(state))
    assert not any(i.startswith("attack:fresh") for i in ids)
    assert any(i.startswith("attack:ready") for i in ids)


def test_taunt_absorbs_attacks_and_blocks_face():
    state = _base_state()
    state.my_minions = [Minion(id="atk", name="Atk", attack=2, health=2, max_health=2, can_attack=True)]
    state.opp_minions = [
        Minion(id="t", name="Taunt", attack=1, health=4, max_health=4, taunt=True),
        Minion(id="n", name="Normal", attack=5, health=5, max_health=5),
    ]
    ids = _ids(enumerate_legal_actions(state))
    # Only the taunt minion may be attacked; face and non-taunt are blocked.
    assert "attack:atk->t" in ids
    assert "attack:atk->n" not in ids
    assert "attack:atk->OppHero" not in ids


def test_targeted_spell_generates_per_target_candidates():
    state = _base_state()
    state.mana = 4
    state.hand = [HandCard(id="fire", name="Fireball", cost=4, card_type="spell",
                           target_scopes=["enemy_minion", "enemy_hero"])]
    state.opp_minions = [Minion(id="e1", name="Enemy", attack=2, health=3, max_health=3)]
    ids = _ids(enumerate_legal_actions(state))
    assert "play:fire->e1" in ids
    assert "play:fire->OppHero" in ids


def test_hero_power_respects_cost_and_usage():
    state = _base_state()
    state.mana = 1
    ids = _ids(enumerate_legal_actions(state))
    assert "hero_power" not in ids  # cost 2 > mana 1

    state.mana = 2
    ids = _ids(enumerate_legal_actions(state))
    assert "hero_power" in ids

    state.my_hero.hero_power_usable = False
    ids = _ids(enumerate_legal_actions(state))
    assert "hero_power" not in ids


# --- advisor: model choice + fallback ---------------------------------------

def _adv(solver, min_confidence=0.55, min_margin=0.10):
    return HearthstoneAdvisor(
        provider=MockReplayProvider(solver_fn=solver),
        min_confidence=min_confidence,
        min_margin=min_margin,
    )


async def test_advisor_returns_model_choice():
    state = _base_state()
    state.mana = 3
    state.hand = [HandCard(id="c", name="Card", cost=3, card_type="minion", attack=4, health=4)]

    def solver(payload, q):
        return "play:c", 0.9, {"play:c": 0.9, "end_turn": 0.1}

    advisor = _adv(solver)
    rec = await advisor.recommend(state)
    advisor.close()
    assert rec.candidate_id == "play:c"
    assert not rec.is_fallback
    assert rec.confidence > 0.5


async def test_advisor_falls_back_on_low_confidence():
    state = _base_state()
    state.mana = 3
    state.hand = [HandCard(id="c", name="Card", cost=3, card_type="minion", attack=4, health=4)]

    def solver(payload, q):
        # Model is unsure (below min_confidence) -> engine should fall back.
        return "play:c", 0.3, {"play:c": 0.3, "end_turn": 0.7}

    advisor = _adv(solver, min_confidence=0.55)
    rec = await advisor.recommend(state)
    advisor.close()
    assert rec.is_fallback


async def test_advisor_rejects_illegal_model_pick():
    state = _base_state()
    state.mana = 2
    state.hand = [HandCard(id="expensive", name="Expensive", cost=7, card_type="minion", attack=7, health=7)]

    def solver(payload, q):
        # Model hallucinates a play that is not in the legal envelope.
        return "play:expensive", 0.99, {"play:expensive": 0.99, "end_turn": 0.01}

    advisor = _adv(solver)
    rec = await advisor.recommend(state)
    advisor.close()
    # Illegal pick must never surface; the engine substitutes a legal fallback.
    assert rec.candidate_id != "play:expensive"
    assert rec.candidate_id in [a["id"] for a in rec.legal_actions]
    assert rec.is_fallback


async def test_single_legal_option_is_forced_without_model():
    # Empty board, no mana: the only legal action is end_turn, resolved fast-path.
    state = _base_state()
    state.mana = 0
    state.hand = []
    advisor = _adv(lambda payload, q: (None, 0.0, {}))
    rec = await advisor.recommend(state)
    advisor.close()
    assert rec.candidate_id == "end_turn"
