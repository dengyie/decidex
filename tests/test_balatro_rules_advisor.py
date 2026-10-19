"""Legal-action enumeration, safe default, and the BalatroAdvisor pipeline."""

import json
from pathlib import Path

import pytest

from decidex.balatro import (
    BalatroAdvisor,
    Blind,
    BalatroState,
    Card,
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
    st.played_hands = ["Pair", "Flush"]
    st.most_played_hand = "Pair"
    d = st.to_dict()
    restored = BalatroState.from_dict(d)
    assert restored.hand[0].label == st.hand[0].label
    assert restored.played_hands == ["Pair", "Flush"]
    assert restored.most_played_hand == "Pair"


@pytest.mark.parametrize(
    ("blind_name", "history", "most_played", "expected_reason"),
    [
        ("The Eye", ["Pair"], None, "本回合已打过"),
        ("The Mouth", ["Two Pair"], None, "限制本回合只能打出"),
        ("The Ox", [], "Pair", "将导致 $25 全部清零"),
    ],
)
def test_snapshot_history_reaches_action_safety(blind_name, history, most_played, expected_reason):
    st = BalatroState(
        hand=[Card(rank="K", suit="H"), Card(rank="K", suit="S")],
        blind=Blind(name=blind_name, chips=1000),
        played_hands=history,
        most_played_hand=most_played,
        money=25,
    )
    actions = enumerate_legal_actions(st)
    play = next(a for a in actions if a.metadata.get("action_type") == "play" and a.metadata.get("hand_type") == "Pair")
    if blind_name == "The Ox":
        assert expected_reason in play.description
        assert "全部清零" in play.metadata["safety_reason"]
        assert play.heuristic_score < 1.0
    else:
        assert play.metadata["fatal_violation"] is True
        assert expected_reason in play.metadata["safety_reason"]


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
    files = collect_snapshots([str(FIXTURES)])   # 测试显式指定 fixtures;产品代码只用真机 bridge 目录
    assert all("fixtures" in str(f) for f in files)
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


def test_weak_hand_still_sees_plays_in_envelope():
    """P1 回归: 低底注弱牌时,最佳出牌必须仍高于所有弃牌,不被 top_k 剪掉。"""
    st = BalatroState(
        hand=plain([("2", "H"), ("5", "D"), ("9", "C"), ("3", "S"),
                    ("6", "H"), ("J", "S"), ("4", "D"), ("K", "H")]),
        jokers=[],
        blind=Blind(name="Small Blind", type="small", chips=300, scored=0),
        hands_left=4, discards_left=3,
    )
    acts = enumerate_legal_actions(st)
    top = [c.candidate_id for c in sorted(acts, key=lambda c: -c.heuristic_score)[:10]]
    plays = [c for c in acts if c.metadata["action_type"] == "play"]
    best_play = max(plays, key=lambda c: c.heuristic_score)
    assert best_play.candidate_id in top, "最佳出牌被弃牌挤出 top-10"
    best_discard = max((c for c in acts if c.metadata["action_type"] == "discard"),
                       key=lambda c: c.heuristic_score)
    assert best_play.heuristic_score > best_discard.heuristic_score


def test_hopeless_hand_prefers_discards():
    """P1 回归: 无论怎么打都到不了目标时,弃牌应排到出牌前面。"""
    st = BalatroState(
        hand=plain([("2", "H"), ("3", "D"), ("5", "C"), ("7", "S"),
                    ("8", "H"), ("9", "D"), ("4", "C"), ("6", "S")]),
        jokers=[],
        blind=Blind(name="Boss", type="boss", chips=100000, scored=0),
        hands_left=1, discards_left=3,
    )
    acts = enumerate_legal_actions(st)
    best_discard = max((c for c in acts if c.metadata["action_type"] == "discard"),
                       key=lambda c: c.heuristic_score)
    best_play = max((c for c in acts if c.metadata["action_type"] == "play"),
                    key=lambda c: c.heuristic_score)
    assert best_discard.heuristic_score > best_play.heuristic_score


def test_example_default_dir_is_bridge_dir():
    """语义回归:示例与网页只读真机 bridge 目录,不回落 demo fixtures。"""
    from examples.balatro_real_replay import DEFAULT_DIR
    assert DEFAULT_DIR.name == "balatro_bridge"
