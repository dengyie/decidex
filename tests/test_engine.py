"""
Unit tests for the central DecisionEngine orchestrator.
"""

import json
from typing import Any, List
import pytest

from decidex.engine import DecisionEngine
from decidex.guards import CalibratedDecisionGuard
from decidex.memory import MemoryHarness
from decidex.providers.mock import MockReplayProvider
from decidex.types import PrimitiveType, QuestionSpec


@pytest.mark.asyncio
async def test_engine_multi_head_step(tmp_path):
    journal_file = str(tmp_path / "decision_journal.jsonl")
    provider = MockReplayProvider(
        preset_verdicts={
            "act": {"selected": "jump", "confidence": 0.95},
            "is_risky": {"selected": True, "confidence": 0.88},
            "eval_score": {"selected": 7.5, "confidence": 0.90}
        }
    )

    # Test context manager protocol
    async with DecisionEngine(provider=provider, journal_path=journal_file) as engine:
        questions = [
            QuestionSpec(
                id="act",
                primitive=PrimitiveType.CHOICE,
                description="Choose movement",
                options=["jump", "walk"]
            ),
            QuestionSpec(
                id="is_risky",
                primitive=PrimitiveType.NOUL,
                description="Is state risky?"
            ),
            QuestionSpec(
                id="eval_score",
                primitive=PrimitiveType.SCORE,
                description="Board score",
                scale=(0.0, 10.0)
            )
        ]

        verdicts = await engine.step(
            domain_state={"player_x": 50},
            questions=questions,
            legal_actions=["jump", "walk"]
        )

        assert "act" in verdicts
        assert verdicts["act"].selected == "jump"
        assert not verdicts["act"].is_fallback

        assert "is_risky" in verdicts
        assert verdicts["is_risky"].selected is True

        assert "eval_score" in verdicts
        assert verdicts["eval_score"].selected == 7.5

    # Verify journal file was written with step_id
    with open(journal_file, "r", encoding="utf-8") as f:
        lines = f.readlines()
        assert len(lines) == 3
        first_entry = json.loads(lines[0])
        assert "step_id" in first_entry
        assert "observation" in first_entry
        assert "verdict" in first_entry


@pytest.mark.asyncio
async def test_engine_guard_fallback():
    # Low confidence triggers guard rejection and fallback
    provider = MockReplayProvider(
        preset_verdicts={
            "act": {"selected": "run", "confidence": 0.40, "distribution": {"run": 0.40, "walk": 0.60}}
        }
    )
    guard = CalibratedDecisionGuard(min_confidence=0.60)
    engine = DecisionEngine(provider=provider, guard=guard)

    questions = [
        QuestionSpec(
            id="act",
            primitive=PrimitiveType.CHOICE,
            description="Select action",
            options=["walk", "run"]
        )
    ]

    verdicts = await engine.step(
        domain_state={"danger": True},
        questions=questions,
        legal_actions=["walk", "run"]
    )

    verdict = verdicts["act"]
    assert verdict.is_fallback is True
    assert "LOW_CONFIDENCE" in (verdict.fallback_reason or "")
    assert verdict.selected == "walk"  # First legal action fallback
    # Verify fallback resets confidence to 0.0 and clears distribution to prevent downstream misuse
    assert verdict.calibrated_confidence == 0.0
    assert verdict.raw_confidence == 0.0
    assert verdict.distribution == {}


@pytest.mark.asyncio
async def test_engine_feedback_recording_and_journal_write(tmp_path):
    journal_file = str(tmp_path / "feedback_journal.jsonl")
    provider = MockReplayProvider(
        preset_verdicts={"act": "jump"}
    )
    mem = MemoryHarness()
    engine = DecisionEngine(provider=provider, memory=mem, journal_path=journal_file)

    questions = [
        QuestionSpec(
            id="act",
            primitive=PrimitiveType.CHOICE,
            description="Action",
            options=["jump", "walk"]
        )
    ]

    await engine.step({"x": 10}, questions, ["jump", "walk"])
    assert mem.action_history[-1]["action"] == "jump"
    assert mem.action_history[-1]["outcome"] == "PENDING"

    # Record real feedback
    engine.record_feedback("REWARD_COLLECTED", {"score_delta": +100})
    assert mem.action_history[-1]["outcome"] == "REWARD_COLLECTED"
    assert mem.action_history[-1]["metadata"]["score_delta"] == 100

    engine.close()

    # Read journal and verify feedback record was appended
    with open(journal_file, "r", encoding="utf-8") as f:
        records = [json.loads(line) for line in f]

    assert len(records) == 2
    decision_rec, feedback_rec = records[0], records[1]
    assert "step_id" in decision_rec
    assert feedback_rec.get("event") == "feedback"
    assert feedback_rec.get("step_id") == decision_rec["step_id"]
    assert feedback_rec.get("outcome") == "REWARD_COLLECTED"


@pytest.mark.asyncio
async def test_engine_multi_head_dict_isolation():
    """Verifies that legal_actions as a dict routes correctly without cross-head pollution."""
    provider = MockReplayProvider(
        preset_verdicts={
            "card": {"selected": "play_ace", "confidence": 0.9},
            "target": {"selected": "enemy_boss", "confidence": 0.85}
        }
    )
    engine = DecisionEngine(provider=provider)

    questions = [
        QuestionSpec(
            id="card",
            primitive=PrimitiveType.CHOICE,
            description="Pick a card",
            options=["play_ace", "play_king"]
        ),
        QuestionSpec(
            id="target",
            primitive=PrimitiveType.CHOICE,
            description="Pick target",
            options=["enemy_boss", "enemy_minion"]
        )
    ]

    # Pass dictionary of legal actions keyed by question.id
    legal_map = {
        "card": ["play_ace", "play_king"],
        "target": ["enemy_boss"]
    }

    verdicts = await engine.step(
        domain_state={"turn": 1},
        questions=questions,
        legal_actions=legal_map
    )

    assert verdicts["card"].selected == "play_ace"
    assert verdicts["card"].is_fallback is False
    assert verdicts["target"].selected == "enemy_boss"
    assert verdicts["target"].is_fallback is False

    # Now simulate an invalid action selected by model for target that violates legal_map["target"]
    provider_bad = MockReplayProvider(
        preset_verdicts={
            "card": {"selected": "play_ace", "confidence": 0.9},
            "target": {"selected": "enemy_minion", "confidence": 0.85}  # not in legal_map["target"]
        }
    )
    engine_bad = DecisionEngine(provider=provider_bad)
    verdicts_bad = await engine_bad.step(
        domain_state={"turn": 1},
        questions=questions,
        legal_actions=legal_map
    )

    assert verdicts_bad["card"].selected == "play_ace"
    assert verdicts_bad["target"].is_fallback is True
    assert verdicts_bad["target"].selected == "enemy_boss"  # Falls back to only legal target


@pytest.mark.asyncio
async def test_engine_non_serializable_domain_state(tmp_path):
    """Verifies that non-JSON serializable objects (sets, custom objects) in state don't crash journaling."""
    journal_file = str(tmp_path / "custom_state_journal.jsonl")
    provider = MockReplayProvider(
        preset_verdicts={"act": "jump"}
    )

    class CustomGameObject:
        def __init__(self, name: str):
            self.name = name

        def __repr__(self):
            return f"GameObject({self.name})"

    engine = DecisionEngine(provider=provider, journal_path=journal_file)
    questions = [
        QuestionSpec(
            id="act",
            primitive=PrimitiveType.CHOICE,
            description="Choose move",
            options=["jump", "walk"]
        )
    ]

    custom_state = {
        "tags": {"active", "invincible"},  # set is not standard JSON serializable
        "obj": CustomGameObject("player1")
    }

    verdicts = await engine.step(domain_state=custom_state, questions=questions)
    assert verdicts["act"].selected == "jump"
    engine.close()

    # Read journal
    with open(journal_file, "r", encoding="utf-8") as f:
        records = [json.loads(line) for line in f]
    assert len(records) == 1
    assert "GameObject(player1)" in records[0]["observation"]["obj"]


@pytest.mark.asyncio
async def test_engine_domain_state_aware_fallback_resolver():
    """Verifies that fallback_resolver receives domain_state when available, and respects 3-arg signatures."""
    captured_state = {}

    def smart_resolver(q: QuestionSpec, legal_actions: List[str], reason: str, domain_state: Any = None):
        captured_state.update(domain_state or {})
        if domain_state and "preferred_action" in domain_state:
            return domain_state["preferred_action"]
        return legal_actions[0]

    # Model returns empty or invalid verdict to force fallback
    provider_fail = MockReplayProvider(inject_error=True)
    engine_4arg = DecisionEngine(provider=provider_fail, fallback_resolver=smart_resolver)

    questions = [
        QuestionSpec(id="move", primitive=PrimitiveType.CHOICE, description="Move", options=["UP", "DOWN", "LEFT"])
    ]
    state_data = {"preferred_action": "LEFT", "score": 2048}

    verdicts = await engine_4arg.step(
        domain_state=state_data,
        questions=questions,
        legal_actions=["UP", "DOWN", "LEFT"]
    )
    assert verdicts["move"].is_fallback is True
    assert verdicts["move"].selected == "LEFT"
    assert captured_state == state_data

    # Test backward compatibility with legacy 3-argument resolver
    legacy_called = []

    def legacy_resolver(q: QuestionSpec, legal_actions: List[str], reason: str):
        legacy_called.append((q.id, reason))
        return "DOWN"

    engine_3arg = DecisionEngine(provider=provider_fail, fallback_resolver=legacy_resolver)
    verdicts_3arg = await engine_3arg.step(
        domain_state=state_data,
        questions=questions,
        legal_actions=["UP", "DOWN", "LEFT"]
    )
    assert verdicts_3arg["move"].selected == "DOWN"
    assert len(legacy_called) == 1


@pytest.mark.asyncio
async def test_engine_trivial_single_option_deterministic_fast_path():
    """Verifies that 1-option questions are resolved deterministically without calling provider."""
    # A provider that raises an exception if called
    provider_strict = MockReplayProvider(inject_error=True)
    engine = DecisionEngine(provider=provider_strict)

    forced_q = QuestionSpec(
        id="forced_action",
        primitive=PrimitiveType.CHOICE,
        description="Only one action available",
        options=["CONTINUE"]
    )

    verdicts = await engine.step(
        domain_state={"turn": 5},
        questions=[forced_q],
        legal_actions=["CONTINUE"]
    )

    v = verdicts["forced_action"]
    assert v.selected == "CONTINUE"
    assert v.raw_confidence == 1.0
    assert v.calibrated_confidence == 1.0
    assert v.is_fallback is False
    assert v.latency_ms == 0.0
    assert engine.memory.action_history[-1]["action"] == "CONTINUE"


@pytest.mark.asyncio
async def test_engine_trivial_empty_options_fallback():
    """Verifies that 0-option choice questions immediately resolve to fallback without crashing."""
    provider_strict = MockReplayProvider(inject_error=True)
    engine = DecisionEngine(provider=provider_strict)

    empty_q = QuestionSpec(
        id="act",
        primitive=PrimitiveType.CHOICE,
        description="No options",
        options=[]
    )

    verdicts = await engine.step(
        domain_state={"turn": 5},
        questions=[empty_q]
    )

    v = verdicts["act"]
    assert v.is_fallback is True
    assert v.fallback_reason == "EMPTY_OPTIONS_SET"


@pytest.mark.asyncio
async def test_engine_raw_matrix_domain_state(tmp_path):
    """Verifies that 2D raw lists (like 2048 grid) work as domain_state without schema errors."""
    journal_file = str(tmp_path / "matrix_journal.jsonl")
    provider = MockReplayProvider(preset_verdicts={"dir": "UP"})
    engine = DecisionEngine(provider=provider, journal_path=journal_file)

    q = QuestionSpec(
        id="dir",
        primitive=PrimitiveType.CHOICE,
        description="Swipe",
        options=["UP", "DOWN", "LEFT", "RIGHT"]
    )
    raw_grid = [[2, 0, 0, 0], [0, 4, 0, 0], [0, 0, 8, 0], [0, 0, 0, 16]]

    verdicts = await engine.step(
        domain_state=raw_grid,
        questions=[q],
        legal_actions=["UP", "DOWN"]
    )

    assert verdicts["dir"].selected == "UP"
    assert verdicts["dir"].is_fallback is False
    engine.close()

    with open(journal_file, "r", encoding="utf-8") as f:
        records = [json.loads(line) for line in f]
    assert len(records) == 1
    assert records[0]["observation"] == raw_grid



