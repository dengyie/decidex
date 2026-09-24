"""
Unit tests for execution planners: SequentialTurnPlanner, DualRateScheduler, PredictiveFrameController.
"""

import asyncio
import pytest

from decidex.engine import DecisionEngine
from decidex.planners import (
    DualRateScheduler,
    PredictiveFrameController,
    SequentialTurnPlanner,
)
from decidex.providers.mock import MockReplayProvider
from decidex.types import PrimitiveType, QuestionSpec


class MockGameClient:
    def __init__(self, actions_sequence):
        self.actions_sequence = list(actions_sequence)
        self.dispatched = []

    def get_state(self):
        return {"turn": 1, "remaining": len(self.actions_sequence)}

    def get_legal_actions(self):
        if not self.actions_sequence:
            return ["end_turn"]
        return [self.actions_sequence[0], "end_turn"]

    def dispatch_action(self, action: str):
        self.dispatched.append(action)
        if self.actions_sequence and action == self.actions_sequence[0]:
            self.actions_sequence.pop(0)

    async def wait_for_settled(self):
        await asyncio.sleep(0.001)


class SingleActionGameClient:
    def __init__(self):
        self.step = 0
        self.dispatched = []

    def get_state(self):
        return {"step": self.step}

    def get_legal_actions(self):
        if self.step == 0:
            return ["mandatory_target"]  # Single forced legal action
        elif self.step == 1:
            return ["end_turn"]
        return []

    def dispatch_action(self, action: str):
        self.dispatched.append(action)
        self.step += 1

    async def wait_for_settled(self):
        await asyncio.sleep(0.001)


@pytest.mark.asyncio
async def test_sequential_turn_planner():
    client = MockGameClient(["play_card", "attack_minion"])
    provider = MockReplayProvider()
    engine = DecisionEngine(provider=provider)
    planner = SequentialTurnPlanner(engine=engine, max_sub_steps=5)

    step_count = 0

    def dynamic_solver(payload, question):
        nonlocal step_count
        if question.id == "sub_action":
            if step_count == 0:
                return "play_card", 0.95, {"play_card": 0.95, "end_turn": 0.05}
            elif step_count == 1:
                return "attack_minion", 0.95, {"attack_minion": 0.95, "end_turn": 0.05}
            else:
                return "end_turn", 0.99, {"end_turn": 0.99}
        else:
            if step_count < 2:
                return False, 0.85, {"false": 0.85, "true": 0.15}
            return True, 0.95, {"true": 0.95, "false": 0.05}

    provider.solver_fn = dynamic_solver

    def on_step(idx, act, verdict):
        nonlocal step_count
        step_count += 1

    verdicts = await planner.execute_turn(client, step_callback=on_step)
    assert len(verdicts) >= 2
    assert "play_card" in client.dispatched
    assert "attack_minion" in client.dispatched
    assert "end_turn" in client.dispatched


@pytest.mark.asyncio
async def test_sequential_turn_planner_single_forced_action():
    # Tests that when only 1 legal action is available, it does not crash QuestionSpec CHOICE validation
    client = SingleActionGameClient()
    provider = MockReplayProvider()
    engine = DecisionEngine(provider=provider)
    planner = SequentialTurnPlanner(engine=engine, max_sub_steps=5)

    await planner.execute_turn(client)
    assert client.dispatched == ["mandatory_target", "end_turn"]


def test_dual_rate_scheduler():
    scheduler = DualRateScheduler(
        macro_interval_s=2.0,
        micro_interval_s=0.1,
        initial_intent="explore"
    )

    t0 = 100.0
    scheduler.update_macro_intent("attack", t0)
    scheduler.mark_micro_executed(t0)

    # 50ms later -> micro not ready, macro not ready
    assert scheduler.should_update_micro(t0 + 0.05) is False
    assert scheduler.should_update_macro(t0 + 0.05) is False

    # 120ms later -> micro ready, macro not ready
    assert scheduler.should_update_micro(t0 + 0.12) is True
    assert scheduler.should_update_macro(t0 + 0.12) is False

    # 2.1s later -> macro ready
    assert scheduler.should_update_macro(t0 + 2.1) is True


@pytest.mark.asyncio
async def test_predictive_frame_controller_with_custom_head():
    # Verifies that custom action_head_id correctly updates _latest_macro
    controller = PredictiveFrameController(default_macro="idle", action_head_id="controller_macro")
    provider = MockReplayProvider(
        preset_verdicts={"controller_macro": {"selected": "RUN_JUMP_RIGHT", "confidence": 0.95}},
        simulated_latency_ms=10.0
    )
    engine = DecisionEngine(provider=provider)

    questions = [
        QuestionSpec(
            id="controller_macro",
            primitive=PrimitiveType.CHOICE,
            description="Select macro",
            options=["idle", "RUN_RIGHT", "RUN_JUMP_RIGHT"]
        )
    ]

    # Tick 1: background task initiated, returns initial macro
    m1 = await controller.advance_tick(engine, {"x": 10}, questions, ["idle", "RUN_RIGHT", "RUN_JUMP_RIGHT"])
    assert m1 == "idle"

    # Wait for completion
    await asyncio.sleep(0.02)

    # Tick 2: result harvested, returns updated macro
    m2 = await controller.advance_tick(engine, {"x": 15}, questions, ["idle", "RUN_RIGHT", "RUN_JUMP_RIGHT"])
    assert m2 == "RUN_JUMP_RIGHT"

    controller.cancel()
