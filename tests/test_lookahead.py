"""
Unit tests for LookaheadEvaluator 1-step simulation.
"""

from decidex.lookahead import LookaheadEvaluator


def test_lookahead_simulation():
    current_grid = [[2, 2], [0, 0]]

    def mock_transition(grid, move):
        if move == "left":
            return [[4, 0], [0, 0]], {"monotonicity": 0.8, "empty_cells": 3}
        elif move == "right":
            return [[0, 4], [0, 0]], {"monotonicity": 0.7, "empty_cells": 3}
        else:
            return grid, {"monotonicity": 0.1, "empty_cells": 2}

    results = LookaheadEvaluator.simulate_moves(
        current_state=current_grid,
        candidate_moves=["left", "right", "up"],
        transition_fn=mock_transition
    )

    assert "left" in results
    assert results["left"]["projected_metrics"]["empty_cells"] == 3
    assert results["left"]["projected_state"] == [[4, 0], [0, 0]]

    obs = LookaheadEvaluator.format_lookahead_observation(
        current_state_summary={"score": 100},
        simulated_moves=results
    )
    assert obs["current_state"]["score"] == 100
    assert "left" in obs["simulated_candidate_moves"]


def test_generate_move_criteria_2048():
    from decidex.tools.solver2048 import generate_move_criteria, get_legal_moves

    grid = [
        [0, 0, 0, 0],
        [0, 0, 0, 0],
        [2, 2, 0, 0],
        [128, 64, 32, 0]
    ]
    legal = get_legal_moves(grid)
    criteria = generate_move_criteria(grid, legal)

    assert "LEFT" in criteria
    assert "Merges numbers" in criteria["LEFT"] or "points" in criteria["LEFT"]
    assert "corner" in criteria["LEFT"]
    # UP moves tiles upward away from corner anchor
    if "UP" in criteria:
        assert "displaces max tile" in criteria["UP"] or "safely maintains" in criteria["UP"] or "cells" in criteria["UP"]

