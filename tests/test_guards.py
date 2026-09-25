"""
Unit tests for StateSettlementGuard and CalibratedDecisionGuard.
"""

import math
import pytest

from decidex.guards import CalibratedDecisionGuard, StateSettlementGuard
from decidex.memory import MemoryHarness
from decidex.types import DecisionVerdict, PrimitiveType


def test_state_settlement_guard_stability_and_autoreset():
    guard = StateSettlementGuard(min_stable_frames=3, max_wait_timeout_s=1.0)
    state_hash_moving = 12345
    state_hash_still = 99999

    # Frame 1: moving
    assert guard.is_state_settled(state_hash_moving, current_time=0.0) is False

    # Frame 2: changed to still (frame 1 of still)
    assert guard.is_state_settled(state_hash_still, current_time=0.1) is False

    # Frame 3: still (frame 2 of still)
    assert guard.is_state_settled(state_hash_still, current_time=0.2) is False

    # Frame 4: still (frame 3 of still -> settled!)
    assert guard.is_state_settled(state_hash_still, current_time=0.3) is True

    # Next cycle at t = 2.0 (new action started): must NOT immediately return True
    # It must require min_stable_frames again!
    assert guard.is_state_settled(88888, current_time=2.0) is False
    assert guard.is_state_settled(88888, current_time=2.1) is False
    assert guard.is_state_settled(88888, current_time=2.2) is True


def test_state_settlement_guard_timeout():
    guard = StateSettlementGuard(min_stable_frames=5, max_wait_timeout_s=0.5)
    # Always changing hashes, but time reaches timeout
    assert guard.is_state_settled(1, current_time=0.0) is False
    assert guard.is_state_settled(2, current_time=0.2) is False
    assert guard.is_state_settled(3, current_time=0.6) is True  # 0.6 >= 0.5


def test_calibrated_decision_guard_temperature_validation():
    with pytest.raises(ValueError, match="strictly positive"):
        CalibratedDecisionGuard(temperature=0.0)

    with pytest.raises(ValueError, match="strictly positive"):
        CalibratedDecisionGuard(temperature=-0.5)


def test_calibrated_decision_guard_temperature_softening():
    guard = CalibratedDecisionGuard(temperature=1.25)
    raw_dist = {"opt_a": 0.90, "opt_b": 0.10}
    cal_dist, top_val = guard.calibrate_distribution(raw_dist)

    # Temperature > 1.0 must soften/reduce overconfidence
    assert top_val < 0.90
    assert cal_dist["opt_a"] + cal_dist["opt_b"] == pytest.approx(1.0, rel=1e-4)


def test_calibrated_decision_guard_enforce_legal():
    guard = CalibratedDecisionGuard(enforce_legal=True)
    verdict = DecisionVerdict(
        id="act",
        primitive=PrimitiveType.CHOICE,
        selected="teleport",
        raw_confidence=0.99,
        calibrated_confidence=0.99
    )
    is_valid, reason = guard.verify(verdict, legal_actions=["walk", "jump"])
    assert is_valid is False
    assert "ILLEGAL_ACTION" in reason

    # When legal_actions is an explicitly empty list (e.g. player stunned/no legal moves), any action is illegal
    is_valid_empty, reason_empty = guard.verify(verdict, legal_actions=[])
    assert is_valid_empty is False
    assert "ILLEGAL_ACTION" in reason_empty


def test_calibrated_decision_guard_low_confidence():
    guard = CalibratedDecisionGuard(min_confidence=0.70)
    verdict = DecisionVerdict(
        id="act",
        primitive=PrimitiveType.CHOICE,
        selected="walk",
        raw_confidence=0.50,
        calibrated_confidence=0.50
    )
    is_valid, reason = guard.verify(verdict, legal_actions=["walk", "jump"])
    assert is_valid is False
    assert "LOW_CONFIDENCE" in reason


def test_calibrated_decision_guard_ambiguous_margin():
    guard = CalibratedDecisionGuard(min_confidence=0.40, min_margin=0.20, temperature=1.0)
    # top1 is 0.52, top2 is 0.48 -> margin is 0.04 < 0.20
    verdict = DecisionVerdict(
        id="act",
        primitive=PrimitiveType.CHOICE,
        selected="walk",
        raw_confidence=0.52,
        distribution={"walk": 0.52, "jump": 0.48}
    )
    is_valid, reason = guard.verify(verdict, legal_actions=["walk", "jump"])
    assert is_valid is False
    assert "AMBIGUOUS_MARGIN" in reason


def test_calibrated_decision_guard_stuck_consecutive_action():
    guard = CalibratedDecisionGuard(max_consecutive_action=3)
    mem = MemoryHarness()
    for _ in range(3):
        mem.record_step("jump")

    verdict = DecisionVerdict(
        id="act",
        primitive=PrimitiveType.CHOICE,
        selected="jump",
        raw_confidence=0.95
    )
    is_valid, reason = guard.verify(verdict, legal_actions=["walk", "jump"], memory=mem)
    assert is_valid is False
    assert "STUCK_CONSECUTIVE_LIMIT" in reason


def test_calibrated_decision_guard_anti_oscillation():
    guard = CalibratedDecisionGuard(anti_oscillation=True)
    mem = MemoryHarness()
    # History: [left, right, left, right]
    for act in ["left", "right", "left", "right"]:
        mem.record_step(act)

    # 1. Action that continues oscillation cycle (left) MUST be tripped and rejected
    verdict_continue = DecisionVerdict(
        id="act",
        primitive=PrimitiveType.CHOICE,
        selected="left",
        raw_confidence=0.95
    )
    is_valid, reason = guard.verify(verdict_continue, legal_actions=["left", "right", "jump"], memory=mem)
    assert is_valid is False
    assert "ANTI_OSCILLATION_TRIPPED" in reason

    # 2. Action that breaks oscillation (jump) MUST NOT be flagged as oscillation
    verdict_break = DecisionVerdict(
        id="act",
        primitive=PrimitiveType.CHOICE,
        selected="jump",
        raw_confidence=0.95
    )
    is_valid, reason = guard.verify(verdict_break, legal_actions=["left", "right", "jump"], memory=mem)
    assert is_valid is True
    assert reason is None


def test_calibrated_decision_guard_extreme_temperature_numerical_stability():
    """Verifies that extreme low temperature (e.g. T=0.001) does not cause zero division or NaN."""
    guard = CalibratedDecisionGuard(temperature=0.001, min_confidence=0.5, min_margin=0.0)
    raw_dist = {"A": 1e-10, "B": 1.0, "C": 1e-15}

    calibrated, top_prob = guard.calibrate_distribution(raw_dist)
    assert not math.isnan(top_prob)
    assert pytest.approx(sum(calibrated.values()), 1e-6) == 1.0
    assert calibrated["B"] > 0.999
    assert calibrated["A"] < 1e-6


def test_calibrated_decision_guard_noul_casing():
    """Verifies that NOUL boolean verdict matches lowercase 'true'/'false' keys in distribution."""
    guard = CalibratedDecisionGuard(temperature=1.0, min_confidence=0.5)
    verdict = DecisionVerdict(
        id="is_valid",
        primitive=PrimitiveType.NOUL,
        selected=True,
        raw_confidence=0.85,
        distribution={"true": 0.85, "false": 0.15}
    )

    is_valid, reason = guard.verify(verdict, legal_actions=[])
    assert is_valid is True
    assert pytest.approx(verdict.calibrated_confidence, 1e-4) == 0.85


def test_calibrated_decision_guard_consecutive_disabled_and_unproductive_filtering():
    """Verifies that max_consecutive_action <= 0 disables limit, and unproductive_only filters productive moves."""
    mem = MemoryHarness(capacity=20)
    # Record 6 productive 'DOWN' moves
    for _ in range(6):
        mem.record_step(action="DOWN", outcome="SUCCESS")

    verdict = DecisionVerdict(
        id="act",
        primitive=PrimitiveType.CHOICE,
        selected="DOWN",
        raw_confidence=0.9
    )

    # 1. Guard with max_consecutive_action = 0 disables consecutive check entirely
    guard_disabled = CalibratedDecisionGuard(max_consecutive_action=0)
    is_valid, reason = guard_disabled.verify(verdict, legal_actions=["DOWN", "UP"], memory=mem)
    assert is_valid is True
    assert reason is None

    # 2. Guard with default (unproductive_only=False) rejects 6 consecutive moves
    guard_strict = CalibratedDecisionGuard(max_consecutive_action=5, consecutive_unproductive_only=False)
    is_valid, reason = guard_strict.verify(verdict, legal_actions=["DOWN", "UP"], memory=mem)
    assert is_valid is False
    assert "STUCK_CONSECUTIVE_LIMIT" in reason

    # 3. Guard with unproductive_only=True allows productive moves
    guard_smart = CalibratedDecisionGuard(max_consecutive_action=5, consecutive_unproductive_only=True)
    is_valid, reason = guard_smart.verify(verdict, legal_actions=["DOWN", "UP"], memory=mem)
    assert is_valid is True
    assert reason is None

    # 4. Now record 5 unproductive moves (e.g. BLOCKED)
    for _ in range(5):
        mem.record_step(action="DOWN", outcome="BLOCKED")

    is_valid, reason = guard_smart.verify(verdict, legal_actions=["DOWN", "UP"], memory=mem)
    assert is_valid is False
    assert "STUCK_CONSECUTIVE_LIMIT" in reason


def test_state_settlement_guard_anti_ghost_transition():
    """Verifies that mark_action_dispatched prevents settlement until state transitions away from pre-action baseline."""
    guard = StateSettlementGuard(min_stable_frames=2, max_wait_timeout_s=1.0)
    pre_hash = 12345
    new_hash = 67890

    # Mark action dispatched with pre_hash
    guard.mark_action_dispatched(pre_action_state_hash=pre_hash)

    # Frame 1: state is still pre_hash (engine hasn't updated yet) -> MUST NOT SETTLE
    assert guard.is_state_settled(pre_hash, current_time=0.01) is False

    # Frame 2: state is still pre_hash -> MUST NOT SETTLE even after 2 frames!
    assert guard.is_state_settled(pre_hash, current_time=0.02) is False

    # Frame 3: state transitions to new_hash (1st stable frame of new state)
    assert guard.is_state_settled(new_hash, current_time=0.03) is False

    # Frame 4: state remains new_hash (2nd stable frame of new state) -> NOW SETTLED!
    assert guard.is_state_settled(new_hash, current_time=0.04) is True


def test_calibrate_distribution_nan_and_inf_safe():
    """Verifies that calibrate_distribution handles NaN, Inf, and invalid types without crashing."""
    guard_scaled = CalibratedDecisionGuard(temperature=0.5)
    raw_dist_with_nan = {"UP": float("nan"), "DOWN": 0.8, "LEFT": float("inf"), "RIGHT": "invalid"}

    calibrated, top_p = guard_scaled.calibrate_distribution(raw_dist_with_nan)
    assert not math.isnan(top_p)
    assert 0.0 <= top_p <= 1.0
    for k, v in calibrated.items():
        assert not math.isnan(v)
        assert 0.0 <= v <= 1.0
    assert math.isclose(sum(calibrated.values()), 1.0, rel_tol=1e-5)

    # Also test temperature == 1.0 fast path
    guard_t1 = CalibratedDecisionGuard(temperature=1.0)
    calibrated_t1, top_p1 = guard_t1.calibrate_distribution(raw_dist_with_nan)
    assert not math.isnan(top_p1)
    for k, v in calibrated_t1.items():
        assert not math.isnan(v)



