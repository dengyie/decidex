"""
DecideX Demo 3: Lookahead State Simulation Grid Game (2048-style).
Demonstrates:
- 1-step lookahead simulation (LookaheadEvaluator)
- Projected metrics (monotonicity, smoothness, empty tiles)
- Continuous evaluation loop over consecutive board transitions
- Integration with DecisionEngine and Telemetry Journaling
"""

import asyncio
import math
import os
from typing import Any, Dict, List, Tuple

from decidex.engine import DecisionEngine
from decidex.guards import CalibratedDecisionGuard
from decidex.lookahead import LookaheadEvaluator
from decidex.providers.mock import MockReplayProvider
from decidex.tools.solver2048 import simulate_move
from decidex.types import PrimitiveType, QuestionSpec


def simulate_2048_transition(grid: List[List[int]], move: str) -> Tuple[List[List[int]], Dict[str, Any]]:
    """
    Simulates a 2048 transition using the production solver engine and computes metrics.
    """
    g, total_gain, _ = simulate_move(grid, move)

    # Calculate heuristic metrics on projected grid
    empty_cells = sum(row.count(0) for row in g)
    max_tile = max(max(row) for row in g)
    max_in_corner = 1.0 if (g[0][0] == max_tile or g[3][0] == max_tile) else 0.0
    monotonic_row0 = 1.0 if g[0] == sorted(g[0], reverse=True) else 0.0
    bias = 15.0 if move == "UP" else (8.0 if move == "LEFT" else 0.0)

    metrics = {
        "score_gain": total_gain,
        "empty_cells": empty_cells,
        "max_tile": max_tile,
        "corner_bonus": max_in_corner,
        "heuristic_composite": total_gain * 3.0 + empty_cells * 10.0 + max_in_corner * 40.0 + monotonic_row0 * 30.0 + bias
    }
    return g, metrics


async def run_2048_simulation():
    print("\n" + "=" * 60)
    print("🔢 Starting 2048 Lookahead State Simulation Demo")
    print("=" * 60)

    journal_path = os.path.join(os.path.dirname(__file__), "game2048_journal.jsonl")
    if os.path.exists(journal_path):
        os.remove(journal_path)

    # Provider that evaluates projected metrics from lookahead
    def solver(payload, question):
        obs = payload.observation
        simulated = obs.get("simulated_candidate_moves", {})
        # Pick move with highest composite score
        best_move = max(simulated.keys(), key=lambda m: simulated[m]["projected_metrics"]["heuristic_composite"])

        # Softmax over heuristic scores
        max_score = max(simulated[m]["projected_metrics"]["heuristic_composite"] for m in simulated)
        exp_dist = {
            m: math.exp((simulated[m]["projected_metrics"]["heuristic_composite"] - max_score) / 20.0)
            for m in simulated
        }
        tot = sum(exp_dist.values())
        norm_dist = {k: v / tot for k, v in exp_dist.items()}

        if question.id == "move_direction":
            return best_move, 0.95, norm_dist
        elif question.id == "grid_in_danger":
            current_empty = obs.get("current_state", {}).get("empty_cells", 4)
            is_danger = current_empty <= 2
            return is_danger, 0.90, {"true": 0.90 if is_danger else 0.10, "false": 0.10 if is_danger else 0.90}
        return "LEFT", 0.90, {}

    provider = MockReplayProvider(solver_fn=solver)
    guard = CalibratedDecisionGuard(temperature=1.20, min_confidence=0.45, min_margin=0.08, max_consecutive_action=10)
    engine = DecisionEngine(provider=provider, guard=guard, journal_path=journal_path)

    candidate_moves = ["UP", "DOWN", "LEFT", "RIGHT"]
    questions = [
        QuestionSpec(
            id="move_direction",
            primitive=PrimitiveType.CHOICE,
            description="Select best slide direction maximizing corner monotonicity and empty tiles",
            options=candidate_moves
        ),
        QuestionSpec(
            id="grid_in_danger",
            primitive=PrimitiveType.NOUL,
            description="Is the grid near full lockdown?"
        )
    ]

    # Initial 4x4 grid
    current_grid = [
        [256, 128, 64, 32],
        [16,  8,   4,  2],
        [2,   0,   0,  0],
        [0,   0,   0,  0]
    ]

    print("Initial Grid:")
    for row in current_grid:
        print("  ", row)
    print("-" * 60)

    for step_num in range(1, 6):
        # 1. 1-step lookahead across all 4 candidate directions
        simulated_results = LookaheadEvaluator.simulate_moves(
            current_state=current_grid,
            candidate_moves=candidate_moves,
            transition_fn=simulate_2048_transition
        )

        observation = LookaheadEvaluator.format_lookahead_observation(
            current_state_summary={
                "empty_cells": sum(r.count(0) for r in current_grid),
                "max_tile": max(max(r) for r in current_grid)
            },
            simulated_moves=simulated_results
        )

        # 2. Decide optimal direction
        verdicts = await engine.step(
            domain_state=observation,
            questions=questions,
            legal_actions=candidate_moves
        )

        move_v = verdicts["move_direction"]
        danger_v = verdicts["grid_in_danger"]
        chosen_move = str(move_v.selected)

        # 3. Apply chosen transition to game state
        next_grid, metrics = simulate_2048_transition(current_grid, chosen_move)
        # Add a new '2' tile in first available spot to simulate real game spawn
        spawned = False
        for r in range(3, -1, -1):
            for c in range(3, -1, -1):
                if next_grid[r][c] == 0:
                    next_grid[r][c] = 2
                    spawned = True
                    break
            if spawned:
                break
        current_grid = next_grid

        print(f"Step {step_num}: Chosen [{chosen_move}] (Conf: {move_v.calibrated_confidence:.2f}, Danger: {danger_v.selected})")
        print(f"         Projected Score Gain: +{metrics['score_gain']}, Empty Cells: {metrics['empty_cells']}")

    print("-" * 60)
    print("Final Grid:")
    for row in current_grid:
        print("  ", row)

    engine.close()
    print("-" * 60)
    print(f"✅ 2048 demo completed.")
    print(f"📁 Telemetry journal saved to: {journal_path}\n")


if __name__ == "__main__":
    asyncio.run(run_2048_simulation())
