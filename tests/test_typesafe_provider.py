"""
Request/response mapping tests for TypeSafeJevProvider against the System One
wire format shared by TypeSafe Jev and Nace Drex.

These assert observable contract behaviour (what goes on the wire and what
verdicts come back), not internal implementation details.
"""

from decidex.providers.typesafe import TypeSafeJevProvider, _score_levels
from decidex.types import ObservationPayload, PrimitiveType, QuestionSpec


def _provider() -> TypeSafeJevProvider:
    return TypeSafeJevProvider(api_key="dummy_key")


def _payload():
    return ObservationPayload(observation={"ticket": "I was charged twice."})


# ---------------------------------------------------------------- request side

def test_score_request_discretizes_continuous_scale_into_ordered_levels():
    """A SCORE question with only a [min, max] scale must emit an ordered level
    list spanning the range (the model needs discrete levels, not bounds)."""
    q = QuestionSpec(id="s", primitive=PrimitiveType.SCORE, description="rate", scale=(0.0, 4.0))
    body = _provider()._format_request_body(_payload(), [q])
    assert body["questions"]["s"]["criteria"] == ["0", "1", "2", "3", "4"]


def test_score_request_preserves_explicit_dict_criteria():
    q = QuestionSpec(
        id="s", primitive=PrimitiveType.SCORE, description="rate",
        scale=(1.0, 5.0), criteria={"1": "Critical", "5": "Excellent"},
    )
    body = _provider()._format_request_body(_payload(), [q])
    assert body["questions"]["s"]["criteria"] == {"1": "Critical", "5": "Excellent"}


def test_choice_and_noul_request_shape_matches_system_one():
    qs = [
        QuestionSpec(id="c", primitive=PrimitiveType.CHOICE, description="team", options=["billing", "tech"]),
        QuestionSpec(id="n", primitive=PrimitiveType.NOUL, description="refund?"),
    ]
    body = _provider()._format_request_body(_payload(), qs)
    assert body["model"] == "jev-latest"
    assert body["state"] == {"observation": {"ticket": "I was charged twice."}}
    assert body["questions"]["c"]["type"] == "choice"
    assert set(body["questions"]["c"]["criteria"]) == {"billing", "tech"}
    assert body["questions"]["n"] == {"type": "noul", "instructions": "refund?"}


# --------------------------------------------------------------- response side

def test_nace_choice_and_noul_answers_parse():
    data = {
        "model": "drex-v1.5",
        "answers": {
            "c": {"type": "choice", "choice": "billing", "confidence": 0.99,
                  "probabilities": {"billing": 0.9945, "tech": 0.0055}},
            "n": {"type": "noul", "noul": 0.9936},
        },
    }
    qs = [
        QuestionSpec(id="c", primitive=PrimitiveType.CHOICE, description="team", options=["billing", "tech"]),
        QuestionSpec(id="n", primitive=PrimitiveType.NOUL, description="refund?"),
    ]
    verdicts = {v.id: v for v in _provider()._parse_response(data, qs, 12.0)}

    assert verdicts["c"].selected == "billing"
    assert verdicts["c"].raw_confidence == 0.9945  # class posterior, not the margin "confidence"
    assert verdicts["n"].selected is True
    assert verdicts["n"].raw_confidence == 0.9936


def test_nace_score_index_is_mapped_back_into_scale_domain():
    """System One `score` is a probability-weighted level INDEX; `selected` must be
    a real score in the question's scale, and confidence must come from the
    distribution rather than the API's low concentration `confidence` field."""
    data = {
        "answers": {
            "s": {
                "type": "score",
                "score": 2.5,  # expected level index over 5 levels [0..4]
                "legend": ["0", "1", "2", "3", "4"],
                "probabilities": {"0": 0.05, "1": 0.1, "2": 0.4, "3": 0.35, "4": 0.1},
                "confidence": 0.0216,  # nace's concentration metric: deliberately tiny
            }
        }
    }
    q = QuestionSpec(id="s", primitive=PrimitiveType.SCORE, description="rate", scale=(0.0, 4.0))
    v = _provider()._parse_response(data, [q], 9.0)[0]

    assert v.selected == 2.5
    assert v.raw_confidence == 0.4  # probability at modal level 2, not 0.0216
    assert v.distribution["2"] == 0.4


def test_score_levels_helper_falls_back_to_scale_for_non_numeric_labels():
    """Non-numeric criteria labels must still yield the same level COUNT that was
    sent (else the returned index would be mis-scaled)."""
    levels = _score_levels((0.0, 10.0), {"low": "x", "mid": "y", "high": "z"})
    assert levels == [0.0, 5.0, 10.0]
