"""
Unit tests for CandidatePruner heuristic ranking and option truncation.
"""

from decidex.pruner import CandidatePruner
from decidex.types import ActionCandidate


def test_pruner_prune_to_top_k():
    candidates = [
        ActionCandidate(candidate_id=f"c_{i}", description=f"Candidate {i}", heuristic_score=float(i))
        for i in range(10)
    ]
    pruned = CandidatePruner.prune_to_top_k(candidates, k=3)
    assert len(pruned) == 3
    assert [c.candidate_id for c in pruned] == ["c_9", "c_8", "c_7"]


def test_pruner_min_threshold():
    candidates = [
        ActionCandidate(candidate_id="bad_1", description="Bad", heuristic_score=10.0),
        ActionCandidate(candidate_id="bad_2", description="Bad", heuristic_score=20.0),
        ActionCandidate(candidate_id="good_1", description="Good", heuristic_score=85.0),
        ActionCandidate(candidate_id="good_2", description="Good", heuristic_score=95.0),
    ]
    pruned = CandidatePruner.prune_to_top_k(candidates, k=5, min_score_threshold=50.0)
    assert len(pruned) == 2
    assert [c.candidate_id for c in pruned] == ["good_2", "good_1"]


def test_pruner_to_choice_bundle():
    candidates = [
        ActionCandidate(candidate_id="play_pair", description="Play Pair of Kings", heuristic_score=50.0),
        ActionCandidate(candidate_id="discard_low", description="Discard low ranks", heuristic_score=30.0),
    ]
    options, lookup, descriptions = CandidatePruner.to_choice_bundle(candidates)
    assert options == ["play_pair", "discard_low"]
    assert lookup["play_pair"].heuristic_score == 50.0
    assert descriptions["discard_low"] == "Discard low ranks"
