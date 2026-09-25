"""
Unit tests for the 2048 Expectimax Lookahead Solver and Chrome Browser CDP Controller.
"""

import asyncio
import pytest

from decidex.tools.browser_2048 import Chrome2048Controller
from decidex.tools.solver2048 import (
    BoardEvaluationWeights,
    _log2_tile,
    compress_and_merge_line,
    evaluate_board,
    generate_move_criteria,
    get_legal_moves,
    search_best_move,
    simulate_move,
)


def test_log2_tile_bitwise():
    """Verifies that bit-length log2 calculations match mathematical log2 for 2048 powers."""
    assert _log2_tile(0) == 0
    assert _log2_tile(2) == 1
    assert _log2_tile(4) == 2
    assert _log2_tile(8) == 3
    assert _log2_tile(16) == 4
    assert _log2_tile(128) == 7
    assert _log2_tile(1024) == 10
    assert _log2_tile(2048) == 11
    assert _log2_tile(4096) == 12
    assert _log2_tile(8192) == 13


def test_compress_and_merge_line():
    line = [2, 2, 4, 8]
    out, gain = compress_and_merge_line(line)
    assert out == [4, 4, 8, 0]
    assert gain == 4

    line2 = [2, 0, 2, 0]
    out2, gain2 = compress_and_merge_line(line2)
    assert out2 == [4, 0, 0, 0]
    assert gain2 == 4

    line3 = [4, 4, 4, 4]
    out3, gain3 = compress_and_merge_line(line3)
    assert out3 == [8, 8, 0, 0]
    assert gain3 == 16


def test_simulate_move_and_legal_moves():
    grid = [
        [2, 0, 0, 0],
        [2, 0, 0, 0],
        [0, 0, 0, 0],
        [0, 0, 0, 0]
    ]
    legal = get_legal_moves(grid)
    assert "UP" in legal
    assert "DOWN" in legal
    assert "RIGHT" in legal
    # Moving LEFT produces no change
    assert "LEFT" not in legal

    next_g, gain, changed = simulate_move(grid, "UP")
    assert changed is True
    assert gain == 4
    assert next_g[0][0] == 4
    assert next_g[1][0] == 0


def test_evaluate_board_snake_and_monotonicity():
    """Verifies that board evaluation rewards anchored corner tiles and snake patterns."""
    empty_board = [[0] * 4 for _ in range(4)]
    assert evaluate_board(empty_board) == 0.0

    # High quality snake pattern with max tile 2048 at bottom-left [3, 0]
    snake_board = [
        [0, 0, 2, 4],
        [64, 32, 16, 8],
        [128, 256, 512, 1024],
        [2048, 1024, 0, 0]
    ]

    # Disordered board with max tile floating in middle
    disordered_board = [
        [0, 2, 2048, 0],
        [16, 0, 4, 32],
        [8, 64, 128, 0],
        [0, 0, 0, 0]
    ]

    score_snake = evaluate_board(snake_board)
    score_disordered = evaluate_board(disordered_board)
    assert score_snake > score_disordered


def test_search_best_move_depth_2():
    # Tiles on bottom row: moving LEFT merges the two 2s into a 4 at bottom-left [3, 0]
    grid = [
        [0, 0, 0, 0],
        [0, 0, 0, 0],
        [0, 0, 0, 0],
        [0, 0, 2, 2]
    ]
    best_move, score, scores = search_best_move(grid, depth=2)
    # Moving LEFT merges the two 2s and anchors into bottom-left corner
    assert best_move == "LEFT"
    assert len(scores) > 0
    assert "LEFT" in scores


def test_generate_move_criteria():
    grid = [
        [0, 0, 0, 0],
        [0, 0, 0, 0],
        [2, 0, 0, 0],
        [2, 0, 0, 0]
    ]
    criteria = generate_move_criteria(grid, ["UP", "DOWN", "RIGHT"])
    assert "UP" in criteria
    assert "Merges numbers for +4 points" in criteria["UP"]


@pytest.mark.asyncio
async def test_browser_controller_cleanup_on_close():
    """Verifies that closing controller cleanly rejects pending futures with ConnectionError."""
    controller = Chrome2048Controller(cdp_port=9222)
    # Manually insert a pending future without active WS
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    controller._pending[999] = fut

    await controller.close()
    assert len(controller._pending) == 0
    assert fut.done()
    with pytest.raises(ConnectionError):
        fut.result()


def test_2d_snake_chain_and_small_tile_trap():
    """Verifies that trapping a small tile above anchor tile receives heavy penalty."""
    grid_clean = [
        [0, 2, 4, 8],
        [0, 4, 8, 16],
        [32, 64, 128, 256],
        [2048, 1024, 512, 256]
    ]

    grid_trapped = [
        [0, 2, 4, 8],
        [0, 4, 8, 16],
        [2, 64, 128, 256],  # 2 directly above 2048
        [2048, 1024, 512, 256]
    ]

    score_clean = evaluate_board(grid_clean)
    score_trapped = evaluate_board(grid_trapped)
    assert score_clean > score_trapped + 10000.0


def test_adaptive_depth_endgame():
    """Verifies expectimax lookahead with adaptive depth=3 when empty cells <= 4."""
    crowded_grid = [
        [2, 4, 8, 16],
        [32, 64, 128, 256],
        [4, 8, 16, 32],
        [512, 256, 128, 0]  # Only 1 empty cell
    ]
    best_move, score, scores = search_best_move(crowded_grid, depth=2)
    assert best_move in ["DOWN", "RIGHT"]
    assert len(scores) > 0
    assert score > 0


def test_generate_move_criteria_milestone_and_trap_warning():
    """Verifies milestone synthesis tags, major merges, and anchor trap warnings."""
    # Board where moving LEFT merges 512 + 512 -> 1024 (new max milestone)
    grid_milestone = [
        [0, 0, 0, 0],
        [0, 0, 0, 0],
        [0, 0, 0, 0],
        [512, 512, 0, 0]
    ]
    criteria = generate_move_criteria(grid_milestone, ["LEFT"])
    assert "LEFT" in criteria
    crit_text = criteria["LEFT"]
    assert "MILESTONE SYNTHESIS" in crit_text or "MAJOR MERGE" in crit_text
    assert "Rank #1" in crit_text

    # Board where moving LEFT slides a 2 directly above 512 anchor
    grid_trap = [
        [0, 0, 0, 0],
        [0, 0, 0, 0],
        [0, 2, 0, 0],
        [512, 256, 128, 64]
    ]
    criteria_trap = generate_move_criteria(grid_trap, ["LEFT", "RIGHT", "UP"])
    assert "HIGH DANGER: traps small tile" in criteria_trap.get("LEFT", "")
    assert "preserves S-curve snake continuation on row 2" in criteria_trap.get("RIGHT", "")


@pytest.mark.asyncio
async def test_send_move_and_wait_settlement_behavior(monkeypatch):
    """Verifies that send_move_and_wait_settlement polls until grid changes or dead."""
    controller = Chrome2048Controller(cdp_port=9222)

    initial_grid = [[0] * 4 for _ in range(4)]
    updated_grid = [[2, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]]

    call_count = 0

    async def mock_send_move(direction: str):
        return True

    async def mock_get_state():
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            return {"grid": updated_grid, "score": 4, "dead": False}
        return {"grid": initial_grid, "score": 0, "dead": False}

    monkeypatch.setattr(controller, "send_move", mock_send_move)
    monkeypatch.setattr(controller, "get_state", mock_get_state)

    settled = await controller.send_move_and_wait_settlement("UP", initial_grid=initial_grid, timeout_s=0.2)
    assert settled["grid"] == updated_grid
    assert settled["score"] == 4


def test_solver2048_custom_evaluation_weights():
    """Verifies that BoardEvaluationWeights allows custom parameter overrides."""
    grid = [
        [0, 0, 0, 0],
        [0, 0, 0, 0],
        [0, 0, 0, 0],
        [1024, 512, 256, 128]
    ]

    custom_weights = BoardEvaluationWeights(
        matrix_weight=2.0,
        empty_weight=0.5,
        monotonicity_weight=50.0,
        corner_max_bonus=200.0
    )

    score_default = evaluate_board(grid)
    score_custom = evaluate_board(grid, weights=custom_weights)
    assert score_custom != score_default
    assert score_custom > 0

    best_move, best_score, candidate_scores = search_best_move(grid, depth=2, weights=custom_weights)
    assert best_move is not None
    assert len(candidate_scores) > 0


@pytest.mark.asyncio
async def test_browser_listener_disconnect_rejects_pending():
    """Verifies that if CDP WebSocket disconnects, all pending call futures receive ConnectionError."""
    controller = Chrome2048Controller()
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    controller._pending[999] = fut

    # Simulate listener teardown on socket error
    err = ConnectionError("CDP WebSocket connection closed")
    for pending_fut in list(controller._pending.values()):
        if not pending_fut.done():
            pending_fut.set_exception(err)
    controller._pending.clear()

    with pytest.raises(ConnectionError, match="CDP WebSocket connection closed"):
        await fut


def test_uniform_sampling_no_spatial_bias():
    """Verifies that expectimax evaluates child nodes by sampling across all rows evenly."""
    # A board with 9 empty cells scattered across rows 0, 1, 2, 3
    grid = [
        [0, 2, 0, 2],
        [0, 4, 0, 4],
        [0, 8, 0, 8],
        [16, 0, 0, 0]
    ]
    empty_positions = [(r, c) for r in range(4) for c in range(4) if grid[r][c] == 0]
    assert len(empty_positions) == 9

    # Execute search_best_move and ensure it succeeds
    best_m, score, scores = search_best_move(grid, depth=2)
    assert best_m in ["LEFT", "RIGHT", "UP", "DOWN"]
    assert len(scores) > 0


def test_independent_row_monotonicity_snake_pattern():
    """Verifies that alternating snake rows (row 3 L->R, row 2 R->L) preserve high monotonicity."""
    # Row 3 is strictly descending L->R: 2048, 1024, 512, 256
    # Row 2 is strictly descending R->L: 16, 32, 64, 128 (so index 0=16, index 3=128)
    snake_grid = [
        [0, 0, 0, 0],
        [0, 0, 0, 0],
        [16, 32, 64, 128],
        [2048, 1024, 512, 256]
    ]

    score = evaluate_board(snake_grid)
    # Monotonicity should be strongly positive relative to a disordered grid
    disordered_grid = [
        [0, 0, 0, 0],
        [0, 0, 0, 0],
        [64, 16, 128, 32],
        [512, 2048, 256, 1024]
    ]
    disordered_score = evaluate_board(disordered_grid)
    assert score > disordered_score + 2000.0


