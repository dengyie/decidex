"""
DecideX Demo 1: Real-time Reactive Action Platformer (Mario-style).
Demonstrates:
- Predictive non-blocking frame controller (PredictiveFrameController)
- Dual-rate scheduling (DualRateScheduler: 0.5Hz macro intent + 10Hz micro controller actions)
- State settlement & anti-oscillation guards
- Telemetry recording into JSONL
"""

import asyncio
import os
import time

from decidex.engine import DecisionEngine
from decidex.guards import CalibratedDecisionGuard
from decidex.planners import DualRateScheduler, PredictiveFrameController
from decidex.providers.mock import MockReplayProvider
from decidex.types import PrimitiveType, QuestionSpec


async def run_mario_simulation():
    print("\n" + "=" * 60)
    print("🎮 Starting Mario Reactive Real-time Platformer Demo")
    print("=" * 60)

    journal_path = os.path.join(os.path.dirname(__file__), "mario_journal.jsonl")
    if os.path.exists(journal_path):
        os.remove(journal_path)

    # Provider with realistic simulated network inference latency (35ms)
    def mario_solver(payload, question):
        obs = payload.observation
        danger_dist = obs.get("nearest_enemy_dist", 999)
        pit_dist = obs.get("nearest_pit_dist", 999)

        if question.id == "controller_macro":
            if danger_dist < 40 or pit_dist < 30:
                return "RUN_JUMP_RIGHT", 0.94, {
                    "RUN_JUMP_RIGHT": 0.94,
                    "RUN_RIGHT": 0.04,
                    "DUCK": 0.02
                }
            return "RUN_RIGHT", 0.96, {
                "RUN_RIGHT": 0.96,
                "RUN_JUMP_RIGHT": 0.03,
                "WALK_RIGHT": 0.01
            }
        elif question.id == "is_emergency":
            is_emerg = danger_dist < 30 or pit_dist < 20
            conf = 0.95 if is_emerg else 0.88
            return is_emerg, conf, {"true": conf, "false": 1.0 - conf}
        return "RUN_RIGHT", 0.90, {}

    provider = MockReplayProvider(solver_fn=mario_solver, simulated_latency_ms=25.0)
    guard = CalibratedDecisionGuard(temperature=1.20, min_confidence=0.60, min_margin=0.15)
    engine = DecisionEngine(provider=provider, guard=guard, journal_path=journal_path)

    scheduler = DualRateScheduler(macro_interval_s=1.0, micro_interval_s=0.1, initial_intent="speedrun_safe")
    frame_controller = PredictiveFrameController(default_macro="RUN_RIGHT", action_head_id="controller_macro")

    legal_macros = ["RUN_RIGHT", "WALK_RIGHT", "RUN_JUMP_RIGHT", "DUCK", "IDLE"]
    questions = [
        QuestionSpec(
            id="controller_macro",
            primitive=PrimitiveType.CHOICE,
            description="Select optimal 8-frame gamepad button macro",
            options=legal_macros
        ),
        QuestionSpec(
            id="is_emergency",
            primitive=PrimitiveType.NOUL,
            description="Is player in imminent lethal danger?"
        )
    ]

    # Run 25 physical ticks (simulating 60 FPS loop with periodic obstacles)
    player_x = 100
    enemy_x = 220
    pit_x = 350

    print(f"{'Tick':<6} | {'Mario X':<8} | {'Obstacle Ahead':<16} | {'Macro Dispatched':<16} | {'Latency':<8}")
    print("-" * 62)

    for tick in range(1, 26):
        now = time.time()
        nearest_enemy_dist = max(0, enemy_x - player_x)
        nearest_pit_dist = max(0, pit_x - player_x)

        # Macro scheduler check
        if scheduler.should_update_macro(now):
            scheduler.update_macro_intent("speedrun_safe", now)

        state = {
            "player_x": player_x,
            "player_y": 0,
            "nearest_enemy_dist": nearest_enemy_dist,
            "nearest_pit_dist": nearest_pit_dist,
            "macro_intent": scheduler.current_macro_intent
        }

        # Non-blocking predictive tick advance
        t_start = time.perf_counter()
        dispatched_macro = await frame_controller.advance_tick(engine, state, questions, legal_macros)
        t_elapsed = (time.perf_counter() - t_start) * 1000.0

        obstacle_desc = f"Enemy({nearest_enemy_dist}px)" if nearest_enemy_dist < 80 else "Clear"

        print(f"{tick:<6} | {player_x:<8} | {obstacle_desc:<16} | {dispatched_macro:<16} | {t_elapsed:.2f}ms")

        # Actuation physics progression
        if dispatched_macro == "RUN_RIGHT":
            player_x += 12
        elif dispatched_macro == "RUN_JUMP_RIGHT":
            player_x += 15
        else:
            player_x += 6

        # Sleep briefly to simulate game tick
        await asyncio.sleep(0.015)

    engine.close()
    print("-" * 62)
    print(f"✅ Mario simulation completed successfully.")
    print(f"📁 Telemetry journal saved to: {journal_path}\n")


if __name__ == "__main__":
    asyncio.run(run_mario_simulation())
