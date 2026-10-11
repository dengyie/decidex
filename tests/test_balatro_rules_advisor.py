"""Legal-action enumeration, safe default, and the BalatroAdvisor pipeline."""

import json
from pathlib import Path

import pytest

from decidex.balatro import (
    BalatroAdvisor,
    BalatroState,
    enumerate_legal_actions,
    choose_safe_default,
    load_state,
)
from decidex.providers.mock import MockReplayProvider

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "balatro"


def plain(specs):
    from decidex.balatro import Card
    return [Card(rank=r, suit=s) for r, s in specs]


def test_load_state_from_bridge_fixture():
    st = load_state(FIXTURES / "state-0001.json")
    assert len(st.hand) == 8
    assert st.blind.chips == 600
    assert [j.name for j in st.jokers] == ["Joker", "Greedy Joker"]
    assert st.actual is None
    d = st.to_dict()
    assert BalatroState.from_dict(d).hand[0].label == st.hand[0].label


def test_enumeration_shape():
    st = load_state(FIXTURES / "state-0001.json")
    acts = enumerate_legal_actions(st)
    kinds = [a.metadata["action_type"] for a in acts]
    assert kinds.count("play") == 218                      # C(8,1..5)
    assert kinds.count("discard") == 218
    assert kinds.count("use_consumable") == 1              # The Hermit
    # Every play is pre-scored by the simulator.
    best = max((a for a in acts if a.metadata["action_type"] == "play"),
               key=lambda a: a.metadata["score"])
    assert best.metadata["clears"] is True                 # royal flush > 600
    assert best.metadata["score"] == 1812                  # 151 x 12


def test_discards_absent_when_exhausted():
    st = load_state(FIXTURES / "state-0001.json")
    st.discards_left = 0
    acts = enumerate_legal_actions(st)
    assert all(a.metadata["action_type"] != "discard" for a in acts)


def test_safe_default_prefers_guaranteed_clear():
    st = load_state(FIXTURES / "state-0001.json")
    acts = enumerate_legal_actions(st)
    safe = choose_safe_default(acts, st)
    assert safe.metadata["action_type"] == "play"
    assert safe.metadata["clears"] is True


def _solver(pick_first=True):
    def solver(payload, question):
        opts = question.options or []
        if not opts:
            return "end", 0.4, {}
        return opts[0], 0.9, {o: (0.9 if o == opts[0] else 0.01) for o in opts}
    return solver


@pytest.mark.asyncio
async def test_advisor_recommends_clearing_play():
    st = load_state(FIXTURES / "state-0001.json")
    advisor = BalatroAdvisor(provider=MockReplayProvider(solver_fn=_solver()), top_k=10)
    rec = await advisor.recommend(st)
    assert rec.action_type == "play"
    assert rec.clears_blind is True
    assert rec.candidate_id == "play:0-1-2-3-4"           # royal flush is top-heuristic
    assert rec.is_fallback is False
    assert len(rec.legal_actions) == 10
    advisor.close()


@pytest.mark.asyncio
async def test_advisor_falls_back_when_model_picks_illegal():
    st = load_state(FIXTURES / "state-0001.json")

    def bad_solver(payload, question):
        return "play:99-99", 0.99, {"play:99-99": 0.99}   # not in the legal envelope

    advisor = BalatroAdvisor(provider=MockReplayProvider(solver_fn=bad_solver), top_k=10)
    rec = await advisor.recommend(st)
    assert rec.is_fallback is True
    assert rec.fallback_reason and rec.fallback_reason.startswith("ILLEGAL_ACTION")
    # Deterministic safe default still clears the blind.
    assert rec.clears_blind is True
    advisor.close()


@pytest.mark.asyncio
async def test_real_replay_pairs_human_action():
    """Full fixture pairing: state-0002 records the human's royal-flush play."""
    from examples.balatro_real_replay import collect_snapshots, heuristic_solver
    files = collect_snapshots([])
    assert len(files) == 4
    states = [(f.name, load_state(f)) for f in files]
    pres = [s for _, s in states if s.actual is None]
    actions = [s for _, s in states if s.actual is not None]
    assert len(pres) == 2 and len(actions) == 2
    assert actions[0].actual == {"type": "play", "cards": [0, 1, 2, 3, 4]}

    advisor = BalatroAdvisor(provider=MockReplayProvider(solver_fn=heuristic_solver), top_k=10)
    rec = await advisor.recommend(pres[0])
    assert rec.candidate_id == "play:0-1-2-3-4"           # matches the human
    advisor.close()
