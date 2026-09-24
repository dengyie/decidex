"""
Unit tests for MemoryHarness sliding window and oscillation detection.
"""

from decidex.memory import MemoryHarness


def test_memory_record_step_and_capacity():
    mem = MemoryHarness(capacity=3)
    mem.record_step("move_right", "EXECUTED")
    mem.record_step("jump", "EXECUTED")
    mem.record_step("move_right", "EXECUTED")
    assert len(mem.action_history) == 3

    # Exceed capacity
    mem.record_step("duck", "EXECUTED")
    assert len(mem.action_history) == 3
    assert mem.action_history[0]["action"] == "jump"
    assert mem.action_history[-1]["action"] == "duck"


def test_memory_record_hint_deduplication():
    mem = MemoryHarness(hint_capacity=3)
    mem.record_hint("obstacle_ahead")
    mem.record_hint("obstacle_ahead")  # consecutive duplicate should be ignored
    assert len(mem.recent_hints) == 1

    mem.record_hint("pit_below")
    assert len(mem.recent_hints) == 2


def test_memory_detect_oscillation_period_2():
    mem = MemoryHarness(capacity=10)
    for act in ["left", "right", "left", "right"]:
        mem.record_step(act)
    assert mem.detect_oscillation(k=4) is True


def test_memory_detect_oscillation_period_3():
    mem = MemoryHarness(capacity=10)
    for act in ["up", "right", "down", "up", "right", "down"]:
        mem.record_step(act)
    assert mem.detect_oscillation() is True


def test_memory_no_oscillation():
    mem = MemoryHarness(capacity=10)
    for act in ["right", "right", "jump", "right"]:
        mem.record_step(act)
    assert mem.detect_oscillation() is False


def test_memory_consecutive_action_count():
    mem = MemoryHarness(capacity=10)
    mem.record_step("jump")
    mem.record_step("right")
    mem.record_step("right")
    mem.record_step("right")
    assert mem.count_consecutive_action("right") == 3
    assert mem.count_consecutive_action("jump") == 0


def test_memory_export_context():
    mem = MemoryHarness()
    mem.record_step("jump", "EXECUTED")
    mem.record_hint("coin_above")
    ctx = mem.export_context()
    assert ctx["last_action"] == "jump"
    assert "jump (EXECUTED)" in ctx["recent_actions"]
    assert "coin_above" in ctx["recent_hints"]
    assert ctx["is_oscillating"] is False
