"""
Unit tests for DecideX core types and Pydantic v2 contracts.
"""

import pytest
from decidex.types import (
    ActionCandidate,
    DecisionVerdict,
    ObservationPayload,
    PrimitiveType,
    QuestionSpec,
    TurnPhase,
)


def test_question_spec_choice_valid():
    q = QuestionSpec(
        id="act",
        primitive=PrimitiveType.CHOICE,
        description="Select action",
        options=["jump", "run"]
    )
    q.validate_spec()
    assert q.id == "act"
    assert q.primitive == PrimitiveType.CHOICE
    assert q.options == ["jump", "run"]


def test_question_spec_choice_invalid_too_few_options():
    q = QuestionSpec(
        id="act",
        primitive=PrimitiveType.CHOICE,
        description="Select action",
        options=["jump"]
    )
    with pytest.raises(ValueError, match="requires at least 2 options"):
        q.validate_spec()


def test_question_spec_score_invalid_scale():
    q = QuestionSpec(
        id="rating",
        primitive=PrimitiveType.SCORE,
        description="Evaluate value",
        scale=(10.0, 5.0)
    )
    with pytest.raises(ValueError, match="scale min must be < max"):
        q.validate_spec()


def test_decision_verdict_serialization():
    verdict = DecisionVerdict(
        id="act",
        primitive=PrimitiveType.CHOICE,
        selected="jump",
        raw_confidence=0.92,
        calibrated_confidence=0.88,
        distribution={"jump": 0.88, "run": 0.12},
        latency_ms=15.2,
        is_fallback=False
    )
    dumped = verdict.model_dump()
    assert dumped["id"] == "act"
    assert dumped["selected"] == "jump"
    assert dumped["calibrated_confidence"] == 0.88
    assert not dumped["is_fallback"]


def test_observation_payload():
    payload = ObservationPayload(
        observation={"player_x": 100, "player_y": 200},
        memory={"last_action": "jump"},
        phase=TurnPhase.MAIN
    )
    assert payload.observation["player_x"] == 100
    assert payload.phase == TurnPhase.MAIN


def test_observation_payload_matrix_and_primitives():
    # 2D matrix (2048 grid)
    matrix_payload = ObservationPayload(
        observation=[[2, 0, 0, 0], [4, 2, 0, 0], [8, 4, 2, 0], [16, 8, 4, 2]]
    )
    assert isinstance(matrix_payload.observation, list)
    assert matrix_payload.observation[0][0] == 2

    # String / Text prompt observation
    str_payload = ObservationPayload(observation="Current prompt state")
    assert str_payload.observation == "Current prompt state"


def test_action_candidate():
    candidate = ActionCandidate(
        candidate_id="play_ace_king",
        description="Play Ace and King of Spades",
        heuristic_score=145.0,
        metadata={"chips": 100, "mult": 12}
    )
    assert candidate.heuristic_score == 145.0
    assert candidate.metadata["chips"] == 100
