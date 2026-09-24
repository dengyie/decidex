"""
Combinatorial action candidate generator and heuristic pruner.
Prevents probability dilution in card and board games (Balatro, Hearthstone)
by pre-ranking and truncating the action space to a model-friendly Top-K.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from decidex.types import ActionCandidate


class CandidatePruner:
    """
    Filters and truncates combinatorial action spaces into a compact candidate set.
    """

    @staticmethod
    def prune_to_top_k(
        candidates: List[ActionCandidate],
        k: int = 5,
        min_score_threshold: Optional[float] = None
    ) -> List[ActionCandidate]:
        """
        Sorts candidates by their heuristic score and retains the top K candidates.
        Optionally filters out candidates below a score threshold.
        """
        if not candidates:
            return []

        filtered = candidates
        if min_score_threshold is not None:
            filtered = [c for c in candidates if c.heuristic_score >= min_score_threshold]
            if not filtered:
                # If all are filtered out, keep at least the single best candidate
                filtered = [max(candidates, key=lambda c: c.heuristic_score)]

        # Sort descending by heuristic score
        sorted_candidates = sorted(filtered, key=lambda c: c.heuristic_score, reverse=True)
        return sorted_candidates[:k]

    @staticmethod
    def to_choice_bundle(
        candidates: List[ActionCandidate]
    ) -> Tuple[List[str], Dict[str, ActionCandidate], Dict[str, str]]:
        """
        Converts a list of ActionCandidate objects into:
        1. A list of candidate IDs suitable for QuestionSpec.options
        2. A lookup dict mapping candidate_id -> ActionCandidate
        3. A dictionary mapping candidate_id -> description for prompt injection
        """
        options: List[str] = []
        lookup: Dict[str, ActionCandidate] = {}
        descriptions: Dict[str, str] = {}

        for c in candidates:
            options.append(c.candidate_id)
            lookup[c.candidate_id] = c
            descriptions[c.candidate_id] = c.description

        return options, lookup, descriptions
