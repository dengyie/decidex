"""
Tests for Blind Selection, Tag Skipping Evaluation and Decision Logic in Balatro DecideX.
"""

from __future__ import annotations

import pytest

from decidex.balatro.advisor import build_evidence
from decidex.balatro.blind_eval import (
    TAG_CATALOG,
    evaluate_blind_choice,
)
from decidex.balatro.rules import choose_safe_default, enumerate_legal_actions
from decidex.balatro.state import BalatroState, Blind, Card, Joker


def _make_state(
    blind_type: str = "small",
    blind_name: str = "Small Blind",
    chips: int = 300,
    ante: int = 1,
    money: int = 4,
    jokers: list = None,
    tag: dict = None,
    state_name: str = "BLIND_SELECT",
    can_select_blind: bool = True,
    can_skip_blind: bool = True,
) -> BalatroState:
    return BalatroState(
        hand=[Card(rank="A", suit="S"), Card(rank="K", suit="S"), Card(rank="Q", suit="S")],
        blind=Blind(name=blind_name, type=blind_type, chips=chips, scored=0),
        jokers=jokers or [],
        ante=ante,
        money=money,
        state_name=state_name,
        can_select_blind=can_select_blind,
        can_skip_blind=can_skip_blind,
        blind_tag=tag,
    )


def test_tag_catalog_completeness():
    # Verify canonical Balatro tags exist and have valid tiers and categories
    assert "tag_negative" in TAG_CATALOG
    assert "tag_polychrome" in TAG_CATALOG
    assert "tag_investment" in TAG_CATALOG
    assert "tag_economy" in TAG_CATALOG
    assert TAG_CATALOG["tag_negative"].tier == "S"
    assert TAG_CATALOG["tag_negative"].base_ev >= 9.0


def test_boss_blind_unskippable():
    state = _make_state(
        blind_type="boss",
        blind_name="The Hook",
        chips=1000,
        ante=2,
        money=15,
        jokers=[Joker(name="Joker", mult=4)],
        tag={"name": "Negative Tag", "key": "tag_negative"},
    )
    res = evaluate_blind_choice(state)
    assert res.can_skip is False
    assert res.recommended_action == "select_blind"
    assert res.skip_blind_ev == 0.0

    actions = enumerate_legal_actions(state)
    assert any(a.metadata.get("action_type") == "select_blind" for a in actions)
    assert not any(a.metadata.get("action_type") == "skip_blind" for a in actions)


def test_god_tier_tag_skip_recommendation():
    # Established squad in Ante 2 facing Big Blind with Negative Tag
    state = _make_state(
        blind_type="big",
        blind_name="Big Blind",
        chips=800,
        ante=2,
        money=18,
        jokers=[Joker(name="Joker", mult=4), Joker(name="Bull", chips=60)],
        tag={"name": "Negative Tag", "key": "tag_negative"},
    )
    res = evaluate_blind_choice(state)
    assert res.can_skip is True
    assert res.recommended_action == "skip_blind"
    assert res.skip_blind_ev >= 9.0

    actions = enumerate_legal_actions(state)
    safe = choose_safe_default(actions, state)
    assert safe.metadata.get("action_type") == "skip_blind"
    assert safe.metadata.get("tag_key") == "tag_negative"


def test_low_tier_tag_select_blind_preferred():
    # Facing Small Blind with a mediocre Speed Tag
    state = _make_state(
        blind_type="small",
        blind_name="Small Blind",
        chips=300,
        ante=1,
        money=4,
        jokers=[Joker(name="Joker", mult=4)],
        tag={"name": "Speed Tag", "key": "tag_speed"},
    )
    res = evaluate_blind_choice(state)
    assert res.can_skip is True
    assert res.recommended_action == "select_blind"
    assert res.select_blind_ev > res.skip_blind_ev

    actions = enumerate_legal_actions(state)
    safe = choose_safe_default(actions, state)
    assert safe.metadata.get("action_type") == "select_blind"


def test_ante1_big_blind_zero_jokers_suicide_trap_prevented():
    # In Ante 1, player has 0 jokers. Skipping Big Blind would jump directly to Boss Blind!
    # Even with a high-tier tag like Investment, skipping must be severely penalized to avoid dying.
    state = _make_state(
        blind_type="big",
        blind_name="Big Blind",
        chips=450,
        ante=1,
        money=6,
        jokers=[],  # 0 Jokers!
        tag={"name": "Investment Tag", "key": "tag_investment"},
    )
    res = evaluate_blind_choice(state)
    assert res.risk_warning is not None
    assert "直面 Boss 极大概率暴毙" in res.risk_warning
    assert res.recommended_action == "select_blind"

    actions = enumerate_legal_actions(state)
    safe = choose_safe_default(actions, state)
    assert safe.metadata.get("action_type") == "select_blind"


def test_dynamic_economy_tag_scaling():
    # Rich player ($24) gets massive bonus from Economy Tag
    rich_state = _make_state(
        blind_type="small",
        ante=3,
        money=24,
        jokers=[Joker(name="Joker", mult=4), Joker(name="Cavendish", xmult=3.0)],
        tag={"name": "Economy Tag", "key": "tag_economy"},
    )
    rich_eval = evaluate_blind_choice(rich_state)
    assert rich_eval.skip_blind_ev >= 8.5
    assert rich_eval.recommended_action == "skip_blind"

    # Broke player ($3) gets tiny payout (+$3), better to enter blind and shop
    broke_state = _make_state(
        blind_type="small",
        ante=1,
        money=3,
        jokers=[Joker(name="Joker", mult=4)],
        tag={"name": "Economy Tag", "key": "tag_economy"},
    )
    broke_eval = evaluate_blind_choice(broke_state)
    assert broke_eval.skip_blind_ev <= 4.5
    assert broke_eval.recommended_action == "select_blind"


def test_advisor_evidence_injects_blind_choice():
    state = _make_state(
        blind_type="small",
        ante=2,
        money=12,
        jokers=[Joker(name="Joker", mult=4)],
        tag={"name": "Polychrome Tag", "key": "tag_polychrome"},
    )
    evidence = build_evidence(state, 2)
    assert "blind_evaluation" in evidence
    be = evidence["blind_evaluation"]
    assert be["blind_type"] == "small"
    assert be["tag_key"] == "tag_polychrome"
    assert be["recommended_action"] in ("select_blind", "skip_blind")
    assert "select_blind_ev" in be
    assert "skip_blind_ev" in be


def test_blind_eval_with_none_or_missing_type():
    # Defense against None blind.type or missing blind
    b = Blind(name="Custom Blind", chips=300, scored=0)
    b.type = None  # Explicitly simulate None type from WASM bridge
    state = BalatroState(
        hand=[Card(rank="A", suit="S")],
        blind=b,
        jokers=[],
        ante=1,
        money=4,
        state_name="BLIND_SELECT",
    )
    res = evaluate_blind_choice(state)
    assert res.blind_type == "small"
    assert res.recommended_action == "select_blind"
