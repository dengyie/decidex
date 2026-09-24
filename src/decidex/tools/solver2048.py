"""
High-performance 2048 game simulator and heuristic lookahead evaluator.
Provides deterministic transitions, geometric heuristics (monotonicity, smoothness, corner anchoring),
and multi-step expectimax rollout.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Dict, List, Optional, Tuple


def compress_and_merge_line(line: List[int]) -> Tuple[List[int], int]:
    """Slides and merges a single 4-element line leftwards."""
    non_zeros = [x for x in line if x != 0]
    out: List[int] = []
    score_gain = 0
    i = 0
    while i < len(non_zeros):
        if i + 1 < len(non_zeros) and non_zeros[i] == non_zeros[i + 1]:
            val = non_zeros[i] * 2
            out.append(val)
            score_gain += val
            i += 2
        else:
            out.append(non_zeros[i])
            i += 1
    while len(out) < 4:
        out.append(0)
    return out, score_gain


def simulate_move(grid: List[List[int]], direction: str) -> Tuple[List[List[int]], int, bool]:
    """
    Simulates a move on a 4x4 grid in direction ('LEFT', 'RIGHT', 'UP', 'DOWN').
    Returns:
        (next_grid, score_gain, is_changed)
    """
    g = [row[:] for row in grid]
    total_gain = 0
    changed = False

    if direction == "LEFT":
        for r in range(4):
            new_row, gain = compress_and_merge_line(g[r])
            if new_row != g[r]:
                changed = True
            g[r] = new_row
            total_gain += gain
    elif direction == "RIGHT":
        for r in range(4):
            rev = g[r][::-1]
            new_rev, gain = compress_and_merge_line(rev)
            new_row = new_rev[::-1]
            if new_row != g[r]:
                changed = True
            g[r] = new_row
            total_gain += gain
    elif direction == "UP":
        for c in range(4):
            col = [g[r][c] for r in range(4)]
            new_col, gain = compress_and_merge_line(col)
            for r in range(4):
                if g[r][c] != new_col[r]:
                    changed = True
                g[r][c] = new_col[r]
            total_gain += gain
    elif direction == "DOWN":
        for c in range(4):
            col = [g[r][c] for r in range(4)][::-1]
            new_col_rev, gain = compress_and_merge_line(col)
            new_col = new_col_rev[::-1]
            for r in range(4):
                if g[r][c] != new_col[r]:
                    changed = True
                g[r][c] = new_col[r]
            total_gain += gain

    return g, total_gain, changed


def get_legal_moves(grid: List[List[int]]) -> List[str]:
    """Returns list of moves that actually change the board."""
    legal = []
    for d in ["LEFT", "DOWN", "RIGHT", "UP"]:
        _, _, changed = simulate_move(grid, d)
        if changed:
            legal.append(d)
    return legal


# Canonical S-curve snake path winding across the 4x4 grid from bottom-left [3, 0]
SNAKE_PATH: List[Tuple[int, int]] = [
    (3, 0), (3, 1), (3, 2), (3, 3),  # Row 3 (left -> right)
    (2, 3), (2, 2), (2, 1), (2, 0),  # Row 2 (right -> left)
    (1, 0), (1, 1), (1, 2), (1, 3),  # Row 1 (left -> right)
    (0, 3), (0, 2), (0, 1), (0, 0)   # Row 0 (right -> left)
]

SNAKE_INVERSION_WEIGHTS: List[float] = [
    1200.0, 1200.0, 1200.0,  # Row 3 internal chain
    800.0,                   # Bridge (3, 3) -> (2, 3)
    600.0, 600.0, 600.0,     # Row 2 internal chain
    400.0,                   # Bridge (2, 0) -> (1, 0)
    200.0, 200.0, 200.0,     # Row 1 internal chain
    100.0,                   # Bridge (1, 3) -> (0, 3)
    50.0, 50.0, 50.0         # Row 0 internal chain
]


@dataclass(frozen=True)
class BoardEvaluationWeights:
    """Configurable weights and penalties for 2048 Expectimax board evaluation."""
    matrix_weight: float = 1.0
    empty_weight: float = 1.5
    monotonicity_weight: float = 35.0
    smoothness_weight: float = 2.5
    corner_max_bonus: float = 100.0
    row3_valley_penalty: float = 2500.0
    row2_valley_penalty: float = 1500.0
    anchor_trap_penalty: float = 1500.0


DEFAULT_EVALUATION_WEIGHTS = BoardEvaluationWeights()

# Precomputed bonus table for empty cells [0..16] to eliminate math.log runtime overhead
_EMPTY_CELLS_BONUS: Tuple[float, ...] = tuple(
    (i * 200.0 + (math.log(i + 1) * 150.0 if i > 0 else 0.0)) for i in range(17)
)


def _log2_tile(val: int) -> int:
    """Exact log2 of positive power-of-2 tile via bit length."""
    return (val.bit_length() - 1) if val > 0 else 0


def evaluate_board(
    grid: List[List[int]],
    weights: BoardEvaluationWeights = DEFAULT_EVALUATION_WEIGHTS
) -> float:
    """
    Evaluates board quality using standard 2048 mathematical heuristics:
    1. Monotonicity along snake path to corner (bottom-left [3, 0] or top-left [0, 0]).
    2. Smoothness (minimizing log2 difference between adjacent tiles).
    3. Empty cells (exponential reward for breathing room).
    4. Max tile in corner bonus.
    """
    empty_cells = sum(row.count(0) for row in grid)
    if empty_cells == 16:
        return 0.0

    max_tile = max(max(row) for row in grid)

    # 1. Smoothness: adjacent tile differences in log2 scale
    smoothness = 0.0
    for r in range(4):
        for c in range(4):
            val = grid[r][c]
            if val == 0:
                continue
            log_val = _log2_tile(val)
            # Compare with right neighbor
            if c < 3:
                r_val = grid[r][c + 1]
                if r_val > 0:
                    smoothness -= abs(log_val - _log2_tile(r_val))
            # Compare with bottom neighbor
            if r < 3:
                b_val = grid[r + 1][c]
                if b_val > 0:
                    smoothness -= abs(log_val - _log2_tile(b_val))

    # 2. Monotonicity: computed per row and per column to preserve natural snake pattern
    mono_h = 0.0
    for r in range(4):
        row_l = 0.0
        row_r = 0.0
        for c in range(3):
            v1 = _log2_tile(grid[r][c])
            v2 = _log2_tile(grid[r][c + 1])
            if v1 > v2:
                row_l += (v1 - v2)
            else:
                row_r += (v2 - v1)
        mono_h -= min(row_l, row_r)

    mono_v = 0.0
    for c in range(4):
        col_u = 0.0
        col_d = 0.0
        for r in range(3):
            v1 = _log2_tile(grid[r][c])
            v2 = _log2_tile(grid[r + 1][c])
            if v1 > v2:
                col_u += (v1 - v2)
            else:
                col_d += (v2 - v1)
        mono_v -= min(col_u, col_d)
    monotonicity = mono_h + mono_v

    # 3. Corner bonus for max tile (prefer bottom-left [3, 0] or top-left [0, 0])
    corner_bonus = 0.0
    log_max = _log2_tile(max_tile)
    if grid[3][0] == max_tile or grid[0][0] == max_tile:
        corner_bonus = log_max * weights.corner_max_bonus
    elif grid[3][3] == max_tile or grid[0][3] == max_tile:
        corner_bonus = log_max * (weights.corner_max_bonus * 0.5)

    # 4. Weight matrix heuristic (classic snake weight matrix favoring bottom-left)
    snake_weights = [
        [0.01, 0.03, 0.08, 0.20],
        [3.00, 1.20, 0.50, 0.25],
        [8.00, 20.0, 50.0, 120.0],
        [3000.0, 1200.0, 500.0, 200.0]
    ]
    matrix_score = 0.0
    for r in range(4):
        for c in range(4):
            val = grid[r][c]
            if val > 0:
                matrix_score += _log2_tile(val) * snake_weights[r][c]

    # Empty cells bonus (breathing room)
    empty_score = _EMPTY_CELLS_BONUS[empty_cells] if empty_cells < len(_EMPTY_CELLS_BONUS) else (empty_cells * 200.0)

    # 5. Full 2D S-curve snake chain penalty & anchor trap penalties
    snake_pen = 0.0
    for i in range(len(SNAKE_PATH) - 1):
        r1, c1 = SNAKE_PATH[i]
        r2, c2 = SNAKE_PATH[i + 1]
        v1 = _log2_tile(grid[r1][c1])
        v2 = _log2_tile(grid[r2][c2])
        if v2 > 0 and v1 < v2:
            snake_pen += (v2 - v1) * SNAKE_INVERSION_WEIGHTS[i]

    # Valleys in Row 3 (disrupting primary merge line)
    row3 = grid[3]
    for c in range(1, 3):
        v0 = _log2_tile(row3[c - 1])
        v1 = _log2_tile(row3[c])
        v2 = _log2_tile(row3[c + 1])
        if v1 < v0 and v1 < v2 and v1 > 0 and v2 > 0:
            snake_pen += (min(v0, v2) - v1) * weights.row3_valley_penalty

    # Valleys in Row 2 along snake flow: (2, 3) -> (2, 2) -> (2, 1) -> (2, 0)
    row2_snake = [grid[2][3], grid[2][2], grid[2][1], grid[2][0]]
    for idx in range(1, 3):
        v0 = _log2_tile(row2_snake[idx - 1])
        v1 = _log2_tile(row2_snake[idx])
        v2 = _log2_tile(row2_snake[idx + 1])
        if v1 < v0 and v1 < v2 and v1 > 0 and v2 > 0:
            snake_pen += (min(v0, v2) - v1) * weights.row2_valley_penalty

    # Anchor trap penalty: trapping small tile (<= 4) directly on top of anchor (>= 64)
    anchor_val = grid[3][0]
    above_val = grid[2][0]
    if anchor_val >= 64 and 0 < above_val <= 4:
        snake_pen += (_log2_tile(anchor_val) - _log2_tile(above_val)) * weights.anchor_trap_penalty

    total_score = (
        matrix_score * weights.matrix_weight +
        empty_score * weights.empty_weight +
        monotonicity * weights.monotonicity_weight +
        smoothness * weights.smoothness_weight +
        corner_bonus -
        snake_pen
    )
    return total_score


def search_best_move(
    grid: List[List[int]],
    depth: int = 2,
    weights: BoardEvaluationWeights = DEFAULT_EVALUATION_WEIGHTS
) -> Tuple[Optional[str], float, Dict[str, float]]:
    """
    Performs expectimax search up to given depth with adaptive endgame expansion.
    When empty cells <= 4, automatically expands to depth=3 to calculate endgame death risks.
    Returns (best_move, best_score, candidate_scores).
    """
    legal_moves = get_legal_moves(grid)
    if not legal_moves:
        return None, -1e9, {}

    empty_count = sum(row.count(0) for row in grid)
    effective_depth = 3 if (depth == 2 and empty_count <= 4) else depth

    move_scores: Dict[str, float] = {}

    for move in legal_moves:
        next_grid, gain, _ = simulate_move(grid, move)
        # Depth 1: immediate evaluation
        if effective_depth <= 1:
            score = evaluate_board(next_grid, weights=weights) + gain * 2.0
            move_scores[move] = score
        elif effective_depth == 2:
            # Depth 2: evaluate under random tile placements
            empty_positions = [(r, c) for r in range(4) for c in range(4) if next_grid[r][c] == 0]
            if not empty_positions:
                score = evaluate_board(next_grid, weights=weights) + gain * 2.0
            else:
                # Uniformly sample up to 4 empty positions across entire grid to eliminate spatial bias
                k = len(empty_positions)
                if k <= 4:
                    sample_pos = empty_positions
                else:
                    step = k / 4.0
                    sample_pos = [empty_positions[int(i * step)] for i in range(4)]
                child_scores = []
                for (r, c) in sample_pos:
                    # Spawn 2 (90% prob)
                    test_grid = [row[:] for row in next_grid]
                    test_grid[r][c] = 2
                    sub_legal = get_legal_moves(test_grid)
                    if sub_legal:
                        sub_best = max(evaluate_board(simulate_move(test_grid, sm)[0], weights=weights) for sm in sub_legal)
                        child_scores.append(sub_best)
                    else:
                        child_scores.append(evaluate_board(test_grid, weights=weights) - 5000.0)

                avg_child = sum(child_scores) / len(child_scores) if child_scores else 0.0
                score = evaluate_board(next_grid, weights=weights) * 0.2 + avg_child * 0.8 + gain * 2.0

            move_scores[move] = score
        else:
            # Depth 3: endgame 2-step stochastic lookahead with depth-3 terminal evaluation
            empty_positions = [(r, c) for r in range(4) for c in range(4) if next_grid[r][c] == 0]
            if not empty_positions:
                score = evaluate_board(next_grid, weights=weights) + gain * 2.0
            else:
                k = len(empty_positions)
                if k <= 3:
                    sample_pos = empty_positions
                else:
                    step = k / 3.0
                    sample_pos = [empty_positions[int(i * step)] for i in range(3)]
                child_scores = []
                for (r, c) in sample_pos:
                    test_grid = [row[:] for row in next_grid]
                    test_grid[r][c] = 2
                    sub_legal = get_legal_moves(test_grid)
                    if sub_legal:
                        sub_scores = []
                        for sm in sub_legal:
                            s_grid, s_gain, _ = simulate_move(test_grid, sm)
                            sub_scores.append(evaluate_board(s_grid, weights=weights) + s_gain * 2.0)
                        child_scores.append(max(sub_scores))
                    else:
                        child_scores.append(evaluate_board(test_grid, weights=weights) - 5000.0)

                avg_child = sum(child_scores) / len(child_scores) if child_scores else 0.0
                score = evaluate_board(next_grid, weights=weights) * 0.2 + avg_child * 0.8 + gain * 2.0

            move_scores[move] = score

    best_move = max(move_scores.keys(), key=lambda m: move_scores[m])
    return best_move, move_scores[best_move], move_scores


def generate_move_criteria(
    grid: List[List[int]],
    legal_moves: List[str],
    lookahead_scores: Optional[Dict[str, float]] = None
) -> Dict[str, str]:
    """
    Synthesizes rich, semantic, lookahead-grounded criteria for each legal move.
    Helps System One classification models (Jev/NanoJev) make decisions based on
    future geometric and heuristic properties rather than blind direction strings.
    """
    criteria: Dict[str, str] = {}
    if not grid or not legal_moves:
        return criteria

    max_before = max(max(row) for row in grid) if grid else 0
    empty_before = sum(row.count(0) for row in grid)
    # Check four corners for maximum tile anchoring
    is_corner_before = (
        grid[3][0] == max_before or grid[0][0] == max_before or
        grid[3][3] == max_before or grid[0][3] == max_before
    )

    # Compute lookahead scores if not pre-computed
    if lookahead_scores is None:
        _, _, lookahead_scores = search_best_move(grid, depth=2)

    # Rank moves by lookahead rating
    sorted_moves = sorted(legal_moves, key=lambda m: lookahead_scores.get(m, -1e9), reverse=True)
    rank_map = {m: i + 1 for i, m in enumerate(sorted_moves)}

    for move in legal_moves:
        next_grid, gain, changed = simulate_move(grid, move)
        if not changed:
            continue
        empty_after = sum(row.count(0) for row in next_grid)
        max_after = max(max(row) for row in next_grid)
        is_corner_after = (
            next_grid[3][0] == max_after or next_grid[0][0] == max_after or
            next_grid[3][3] == max_after or next_grid[0][3] == max_after
        )

        parts = []

        # 1. Lookahead evaluation ranking and score
        score = lookahead_scores.get(move, 0.0)
        rank = rank_map.get(move, len(legal_moves))
        if rank == 1:
            parts.append(f"Rank #1 (Optimal, rating {score:,.0f})")
        elif rank == 2:
            parts.append(f"Rank #2 (Secondary, rating {score:,.0f})")
        else:
            parts.append(f"Rank #{rank} (Suboptimal, rating {score:,.0f})")

        # 2. Milestone synthesis & merge gain tagging
        if max_after > max_before and max_after >= 64:
            parts.append(f"MILESTONE SYNTHESIS: creates new peak tile {max_after}!")
        elif gain >= 64:
            parts.append(f"MAJOR MERGE: combines tiles for +{gain} points into high-tier number")
        elif gain > 0:
            parts.append(f"Merges numbers for +{gain} points")
        else:
            parts.append("Shifts tiles with 0 points gain")

        # 3. Free cell delta
        net_empty = empty_after - empty_before
        parts.append(f"leaves {empty_after} free cells (net {net_empty:+d})")

        # 4. Corner anchor safety and risk analysis
        if is_corner_before and not is_corner_after:
            parts.append(f"CRITICAL RISK: displaces max tile ({max_before}) away from corner anchor")
        elif is_corner_after:
            parts.append(f"safely maintains max tile anchored in corner ({max_after})")

        # 5. Trap small tile directly above anchor tile
        if next_grid[3][0] >= 64 and 0 < next_grid[2][0] <= 4:
            parts.append(f"HIGH DANGER: traps small tile ({next_grid[2][0]}) directly above corner anchor ({next_grid[3][0]}), blocking vertical column flow")

        # 6. Dangerous upward shifts when anchoring to bottom
        if move == "UP" and max_before >= 64 and grid[3][0] == max_before:
            row3_full_after = all(next_grid[3][c] != 0 for c in range(4))
            if not row3_full_after:
                parts.append("HIGH DANGER: moving UP risks lifting bottom anchor row and trapping empty spaces beneath max tile")

        # 7. S-curve snake order check (Row 3 and Row 2)
        row3 = next_grid[3]
        if row3[0] >= row3[1] >= row3[2] >= row3[3] and row3[0] == max_after:
            parts.append("maintains descending monotonic order on bottom row")
        elif row3[0] < row3[1] or (row3[1] < row3[2] and row3[2] > 0):
            parts.append("SEVERE WARNING: disrupts descending monotonic chain on bottom row")

        row2 = next_grid[2]
        if row2[3] >= row2[2] >= row2[1] >= row2[0] and (row3[3] >= row2[3] or row2[3] == 0):
            parts.append("preserves S-curve snake continuation on row 2")
        elif row2[0] > row2[1] and row2[0] > 0 and row2[1] > 0:
            parts.append("WARNING: inverts row 2 snake direction")

        criteria[move] = "; ".join(parts)

    return criteria
