"""
Execution planners for sequential turn loops, dual-rate scheduling,
and non-blocking predictive frame interpolation.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Dict, List, Optional, Protocol

from decidex.types import DecisionVerdict, PrimitiveType, QuestionSpec


class GameClientProtocol(Protocol):
    """Protocol for game environments interacting with SequentialTurnPlanner."""
    def get_state(self) -> Dict[str, Any]: ...
    def get_legal_actions(self) -> List[str]: ...
    def dispatch_action(self, action: str) -> None: ...
    async def wait_for_settled(self) -> None: ...


class SequentialTurnPlanner:
    """
    Executes a multi-step sequence of micro-actions within a single turn
    (e.g., Hearthstone: play card -> target minion -> attack hero -> end turn).
    """

    def __init__(self, engine: Any, max_sub_steps: int = 15):
        self.engine = engine
        self.max_sub_steps = max_sub_steps

    async def execute_turn(
        self,
        game_client: GameClientProtocol,
        step_callback: Optional[Callable[[int, str, DecisionVerdict], None]] = None
    ) -> List[DecisionVerdict]:
        verdicts_history: List[DecisionVerdict] = []
        step_idx = 0

        while step_idx < self.max_sub_steps:
            state = game_client.get_state()
            legal_actions = game_client.get_legal_actions()

            # Terminal condition: empty actions
            if not legal_actions:
                break

            # Terminal condition: only end_turn remaining
            if legal_actions == ["end_turn"]:
                game_client.dispatch_action("end_turn")
                break

            # Single forced legal action: short-circuit without asking model
            # (prevents QuestionSpec CHOICE validation error when len(options) < 2)
            if len(legal_actions) == 1:
                forced_act = legal_actions[0]
                if step_callback:
                    # Synthetic verdict for logging
                    syn_verdict = DecisionVerdict(
                        id="sub_action",
                        primitive=PrimitiveType.CHOICE,
                        selected=forced_act,
                        raw_confidence=1.0,
                        calibrated_confidence=1.0,
                        distribution={forced_act: 1.0}
                    )
                    step_callback(step_idx, forced_act, syn_verdict)

                game_client.dispatch_action(forced_act)
                await game_client.wait_for_settled()
                step_idx += 1
                if forced_act == "end_turn":
                    break
                continue

            questions = [
                QuestionSpec(
                    id="sub_action",
                    primitive=PrimitiveType.CHOICE,
                    description="Select the optimal micro-action to maximize turn tempo and value, or end turn.",
                    options=legal_actions
                ),
                QuestionSpec(
                    id="should_end_turn",
                    primitive=PrimitiveType.NOUL,
                    description="Is our turn objectively complete?"
                )
            ]

            decisions = await self.engine.step(state, questions, legal_actions)
            action_verdict = decisions["sub_action"]
            end_turn_verdict = decisions["should_end_turn"]
            verdicts_history.append(action_verdict)

            action = str(action_verdict.selected)
            should_end = bool(end_turn_verdict.selected)
            end_conf = end_turn_verdict.calibrated_confidence

            if step_callback:
                step_callback(step_idx, action, action_verdict)

            # Check termination
            if action == "end_turn" or (should_end and end_conf > 0.80):
                game_client.dispatch_action("end_turn")
                break

            # Dispatch action and wait for animation/physics to settle
            game_client.dispatch_action(action)
            await game_client.wait_for_settled()
            step_idx += 1

        return verdicts_history


class DualRateScheduler:
    """
    Decouples strategic Macro Planning (low frequency, e.g. 0.5Hz)
    from tactical Micro Execution (high frequency, e.g. 10Hz).
    Pattern inspired by phyous/tsai-sc StarCraft controller.
    """

    def __init__(
        self,
        macro_interval_s: float = 2.0,
        micro_interval_s: float = 0.1,
        initial_intent: str = "balance"
    ):
        self.macro_interval_s = macro_interval_s
        self.micro_interval_s = micro_interval_s
        self.last_macro_time: float = 0.0
        self.last_micro_time: float = 0.0
        self.current_macro_intent: str = initial_intent

    def should_update_macro(self, current_time: float) -> bool:
        return (current_time - self.last_macro_time) >= self.macro_interval_s

    def should_update_micro(self, current_time: float) -> bool:
        return (current_time - self.last_micro_time) >= self.micro_interval_s

    def update_macro_intent(self, intent: str, current_time: float) -> None:
        self.current_macro_intent = intent
        self.last_macro_time = current_time

    def mark_micro_executed(self, current_time: float) -> None:
        self.last_micro_time = current_time


class PredictiveFrameController:
    """
    Maintains continuous frame action execution during in-flight async inference
    to ensure 60 FPS games never freeze or stutter.
    """

    def __init__(self, default_macro: str = "noop", action_head_id: Optional[str] = None):
        self.default_macro = default_macro
        self.action_head_id = action_head_id
        self._current_task: Optional[asyncio.Task] = None
        self._latest_macro: str = default_macro

    def _resolve_target_head_id(self, questions: List[QuestionSpec]) -> str:
        if self.action_head_id is not None:
            return self.action_head_id
        for q in questions:
            if q.primitive == PrimitiveType.CHOICE:
                return q.id
        if questions:
            return questions[0].id
        return "action"

    async def advance_tick(
        self,
        engine: Any,
        domain_state: Dict[str, Any],
        questions: List[QuestionSpec],
        legal_macros: List[str]
    ) -> str:
        """
        Advances by one tick. If a background inference task is already in-flight,
        returns the latest macro action to maintain inertia.
        When finished, updates the active macro and spawns the next background decision.
        """
        target_id = self._resolve_target_head_id(questions)

        # Harvest completed background decision if available
        if self._current_task and self._current_task.done():
            if not self._current_task.cancelled():
                try:
                    decisions = self._current_task.result()
                    if target_id in decisions:
                        self._latest_macro = str(decisions[target_id].selected)
                except (Exception, asyncio.CancelledError):
                    # Retain previous macro on error
                    pass
            self._current_task = None

        # Spawn next decision cycle if no task is currently in-flight
        if self._current_task is None:
            self._current_task = asyncio.create_task(
                engine.step(domain_state, questions, legal_macros)
            )

        return self._latest_macro

    def cancel(self) -> None:
        """Cancels any pending in-flight inference task."""
        if self._current_task and not self._current_task.done():
            self._current_task.cancel()
            self._current_task = None
