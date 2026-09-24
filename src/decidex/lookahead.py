"""
1-step lookahead and state simulation evaluator for discrete board games (2048, grid puzzles).
Simulates future state transitions and formats geometric heuristics for model evaluation.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Tuple


class LookaheadEvaluator:
    """
    Executes a 1-step lookahead transition for candidate actions,
    collects projected heuristic metrics, and formats them into an observation payload.
    """

    @staticmethod
    def simulate_moves(
        current_state: Any,
        candidate_moves: List[str],
        transition_fn: Callable[[Any, str], Tuple[Any, Dict[str, Any]]]
    ) -> Dict[str, Dict[str, Any]]:
        """
        Executes transition_fn(current_state, move) -> (next_state_repr, metrics_dict)
        Returns a dictionary mapping move -> { "next_state": ..., "metrics": ... }
        """
        simulated_results: Dict[str, Dict[str, Any]] = {}
        for move in candidate_moves:
            next_state_repr, metrics = transition_fn(current_state, move)
            simulated_results[move] = {
                "projected_state": next_state_repr,
                "projected_metrics": metrics
            }
        return simulated_results

    @staticmethod
    def format_lookahead_observation(
        current_state_summary: Dict[str, Any],
        simulated_moves: Dict[str, Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Combines current state summary with 1-step lookahead projections.
        """
        return {
            "current_state": current_state_summary,
            "simulated_candidate_moves": simulated_moves
        }
