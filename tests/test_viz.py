"""Tests for the platform-agnostic visualization model (decidex.viz).

These cover the pure functions only: token estimation, confidence banding,
probability bars, the three primitive visual specs, and the aggregated lint.
The model is what both web viewers render, so a regression here shows up as a
wrong number on screen rather than an exception.
"""

from __future__ import annotations

import math

import pytest

from decidex.types import PrimitiveType
from decidex import viz


# ---------------------------------------------------------------------------
# token estimation
# ---------------------------------------------------------------------------
def test_estimate_tokens_latin_is_four_chars_per_token():
    assert viz.estimate_tokens("hello world") == 3          # 11 chars -> ceil(11/4)
    assert viz.estimate_tokens("abcd") == 1
    assert viz.estimate_tokens("abcde") == 2                # rounds up


def test_estimate_tokens_counts_cjk_one_per_char():
    assert viz.estimate_tokens("你好世界") == 4
    assert viz.estimate_tokens("筹码倍率") == 4


def test_estimate_tokens_mixed_and_degenerate():
    # 4 CJK + 4 latin -> 4 + 1
    assert viz.estimate_tokens("你好世界abcd") == 5
    assert viz.estimate_tokens(None) == 0
    assert viz.estimate_tokens("") == 0
    # non-strings are stringified, never raise
    assert viz.estimate_tokens({"a": 1}) == viz.estimate_tokens(str({"a": 1}))


def test_fullwidth_forms_count_as_cjk():
    assert viz.estimate_tokens("\uff01\uff02") == 2


# ---------------------------------------------------------------------------
# token report / budgets
# ---------------------------------------------------------------------------
def test_token_report_picks_the_binding_budget():
    """A single oversized question must bind the state+longest budget even when
    the whole request still fits the total one."""
    small_state = {"a": 1}
    huge = "x" * (viz.BUDGET_STATE_PLUS_LONGEST * 4 + 4000)
    report = viz.token_report(small_state, {"q1": huge})
    assert report["binding"]["kind"] == "state_plus_longest"
    assert report["binding"]["level"] == "over"
    # The report measures the JSON-serialised question, not the bare string
    # (providers send the wire form, so that is what the budget must reflect).
    assert report["longest_question"] == viz.estimate_tokens('"' + huge + '"')


def test_token_report_ok_on_a_small_request():
    report = viz.token_report({"blind": {"target": 300}}, {"q": "pick one"})
    assert report["binding"]["level"] == "ok"
    assert report["n_questions"] == 1
    assert report["total"] == report["state"] + report["questions"]


def test_token_report_near_budget():
    # 85% of the total budget: over the 0.9*... no — tune to sit in [0.9, 1.0]
    target = int(viz.BUDGET_TOTAL * 0.95)
    report = viz.token_report("", {"q": "x" * (target * 4)})
    assert report["binding"]["level"] in ("near", "over")
    assert report["binding"]["ratio"] >= 0.9


def test_token_report_accepts_a_sequence_of_questions():
    report = viz.token_report({"a": 1}, ["one", "two", "three"])
    assert report["n_questions"] == 3
    assert report["per_question"][0] == viz.estimate_tokens('"one"')


def test_token_report_empty_questions():
    report = viz.token_report({"a": 1}, {})
    assert report["n_questions"] == 0
    assert report["longest_question"] == 0
    assert report["questions"] == 0


# ---------------------------------------------------------------------------
# confidence banding
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value,expected", [
    (1.0, "high"), (0.95, "high"), (0.9, "high"),        # high threshold inclusive
    (0.89, "medium"), (0.7, "medium"), (0.5, "medium"),  # low threshold inclusive
    (0.49, "low"), (0.0, "low"),
])
def test_confidence_tier_boundaries(value, expected):
    assert viz.confidence_tier(value) == expected


def test_confidence_tier_custom_thresholds():
    assert viz.confidence_tier(0.75, high=0.7, low=0.2) == "high"
    assert viz.confidence_tier(0.1, high=0.7, low=0.2) == "low"


@pytest.mark.parametrize("bad", [None, "junk", float("nan"), {}, []])
def test_confidence_tier_is_total_on_junk(bad):
    assert viz.confidence_tier(bad) == "low"


def test_confidence_badge_shape_and_clamping():
    badge = viz.confidence_badge(1.7)
    assert badge["value"] == 1.0 and badge["ratio"] == 1.0
    assert badge["tier"] == "high"
    assert badge["color"] == "good"
    assert badge["thresholds"] == {"high": viz.CONFIDENCE_HIGH, "low": viz.CONFIDENCE_LOW}
    assert viz.confidence_badge(-3.0)["ratio"] == 0.0


def test_confidence_badge_flags_fallback_so_tier_is_not_misread():
    badge = viz.confidence_badge(0.0, is_fallback=True)
    assert badge["is_fallback"] is True
    assert badge["tier"] == "low"


@pytest.mark.parametrize("tier,color", [("high", "good"), ("medium", "warn"), ("low", "bad")])
def test_confidence_badge_colors(tier, color):
    value = {"high": 0.95, "medium": 0.7, "low": 0.2}[tier]
    assert viz.confidence_badge(value)["color"] == color


# ---------------------------------------------------------------------------
# probability bars
# ---------------------------------------------------------------------------
def test_prob_bars_sorted_desc_with_winner_and_selected():
    bars = viz.prob_bars({"b": 0.3, "a": 0.5, "c": 0.2}, "b")
    assert [b["key"] for b in bars] == ["a", "b", "c"]
    assert bars[0]["winner"] is True
    assert bars[0]["selected"] is False
    assert bars[1]["selected"] is True
    assert bars[0]["percent"] == 50.0


def test_prob_bars_tie_break_is_stable_by_key():
    """Equal probabilities must not swap between polls — that reads as flicker."""
    first = viz.prob_bars({"z": 0.5, "a": 0.5})
    second = viz.prob_bars({"a": 0.5, "z": 0.5})
    assert [b["key"] for b in first] == [b["key"] for b in second] == ["a", "z"]


def test_prob_bars_top_n_renormalises_share():
    bars = viz.prob_bars({"a": 0.5, "b": 0.3, "c": 0.2}, top_n=2)
    assert len(bars) == 2
    assert bars[0]["share"] == round(0.5 / 0.8, 4)
    assert bars[1]["share"] == round(0.3 / 0.8, 4)


def test_prob_bars_labels_override_key():
    bars = viz.prob_bars({"play_0": 1.0}, labels={"play_0": "打顺子"})
    assert bars[0]["label"] == "打顺子"


@pytest.mark.parametrize("junk", [None, {}, "nope", [], {"a": "bad"}, {"a": None}])
def test_prob_bars_tolerates_junk(junk):
    assert viz.prob_bars(junk) == []


def test_prob_bars_zero_total_does_not_divide_by_zero():
    bars = viz.prob_bars({"a": 0.0, "b": 0.0})
    assert all(b["share"] == 0.0 for b in bars)


# ---------------------------------------------------------------------------
# build_visual — choice
# ---------------------------------------------------------------------------
def test_build_visual_choice_shape():
    visual = viz.build_visual(
        PrimitiveType.CHOICE, {"p1": 0.6, "p2": 0.4}, "p1",
        options=["p1", "p2", "p3"], labels={"p1": "打顺子"},
    )
    assert visual["kind"] == "choice"
    assert visual["selected"] == "p1"
    assert visual["empty"] is False
    assert visual["bars"][0]["label"] == "打顺子"
    # option p3 never mentioned by the model still gets a (zero) row, so the
    # bar list matches the legal-action panel
    assert visual["stats"]["n_options"] == 3
    assert visual["stats"]["margin"] == 0.2
    assert visual["stats"]["mass_on_selected"] == 0.6


def test_build_visual_choice_defaults_confidence_to_mass_on_selected():
    """The honest number is the posterior on the pick, not a provider's
    self-reported confidence field."""
    visual = viz.build_visual(PrimitiveType.CHOICE, {"a": 1.0, "b": 0.0}, "a")
    assert visual["confidence"]["value"] == 1.0
    visual2 = viz.build_visual(PrimitiveType.CHOICE, {"a": 0.4, "b": 0.35}, "a")
    assert visual2["confidence"]["value"] == 0.4
    assert visual2["confidence"]["tier"] == "low"


def test_build_visual_choice_accepts_string_primitive():
    assert viz.build_visual("choice", {"a": 1.0}, "a")["kind"] == "choice"
    assert viz.build_visual("CHOICE", {"a": 1.0}, "a")["kind"] == "choice"


def test_build_visual_empty_distribution_is_flagged_not_crashed():
    visual = viz.build_visual(PrimitiveType.CHOICE, {}, None)
    assert visual["empty"] is True
    assert visual["bars"] == []
    assert visual["stats"]["entropy"] == 0.0


# ---------------------------------------------------------------------------
# build_visual — noul
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("p_yes,zone", [
    # The "uncertain" band is |p - 0.5| < 0.15, exclusive: 0.35/0.65 are already
    # decisive, 0.349/0.651 are the nearest values inside the band.
    (0.99, "strong_yes"), (0.65, "strong_yes"),
    (0.649, "uncertain"), (0.5, "uncertain"), (0.351, "uncertain"),
    (0.35, "strong_no"), (0.01, "strong_no"),
])
def test_build_visual_noul_zones(p_yes, zone):
    visual = viz.build_visual(PrimitiveType.NOUL, {"true": p_yes, "false": 1 - p_yes}, p_yes >= 0.5)
    assert visual["kind"] == "noul"
    assert visual["noul"]["zone"] == zone
    assert visual["noul"]["p_no"] == round(1 - p_yes, 4)


def test_build_visual_noul_has_two_bars_yes_no():
    visual = viz.build_visual(PrimitiveType.NOUL, {"true": 0.8, "false": 0.2}, True)
    labels = {b["key"]: b["label"] for b in visual["bars"]}
    assert labels == {"true": "是", "false": "否"}


def test_build_visual_noul_infers_p_yes_from_boolean_selection():
    visual = viz.build_visual(PrimitiveType.NOUL, {}, True)
    assert visual["noul"]["p_yes"] == 1.0
    assert visual["noul"]["zone"] == "strong_yes"


# ---------------------------------------------------------------------------
# build_visual — score
# ---------------------------------------------------------------------------
def test_build_visual_score_marker_on_tick_scale():
    dist = {str(i): 1 / 5 for i in range(5)}
    visual = viz.build_visual(PrimitiveType.SCORE, dist, 3, scale=[0, 100])
    assert visual["kind"] == "score"
    assert visual["score"]["n_levels"] == 5
    assert visual["score"]["marker"] == round(3 / 4, 4)   # index 3 of 0..4
    assert visual["score"]["scale"] == [0, 100]


def test_build_visual_score_marker_clamped_to_scale():
    dist = {str(i): 1 / 5 for i in range(5)}
    assert viz.build_visual(PrimitiveType.SCORE, dist, 99)["score"]["marker"] == 1.0
    assert viz.build_visual(PrimitiveType.SCORE, dist, -5)["score"]["marker"] == 0.0


def test_build_visual_score_single_level_has_no_marker():
    visual = viz.build_visual(PrimitiveType.SCORE, {"0": 1.0}, 0)
    assert visual["score"]["n_levels"] == 1
    assert visual["score"]["marker"] is None


# ---------------------------------------------------------------------------
# stats helpers
# ---------------------------------------------------------------------------
def test_entropy_is_zero_for_certain_and_one_for_flat():
    assert viz.build_visual("choice", {"a": 1.0, "b": 0.0}, "a")["stats"]["entropy"] == 0.0
    flat = viz.build_visual("choice", {"a": 0.5, "b": 0.5}, "a")["stats"]["entropy"]
    assert flat == 1.0


def test_marginal_matches_hand_computation():
    assert viz.marginal({"a": 0.7, "b": 0.2, "c": 0.1}) == 0.5
    assert viz.marginal({"a": 1.0}) == 0.0
    assert viz.marginal({}) == 0.0


# ---------------------------------------------------------------------------
# lint
# ---------------------------------------------------------------------------
def test_lint_clean_request_mostly_quiet():
    issues = viz.lint_decision({"blind": {"target": 300}}, {"q": "criteria"},
                               distribution={"a": 0.95, "b": 0.05}, selected="a",
                               confidence=0.95)
    assert [i.code for i in issues] == []


def test_lint_flags_empty_questions_and_state():
    issues = viz.lint_decision({}, {}, distribution={"a": 1.0}, selected="a")
    codes = {i.code for i in issues}
    assert "no_questions" in codes
    assert "state_empty" in codes
    assert all(i.level == "error" for i in issues if i.code in ("no_questions", "state_empty"))


def test_lint_flags_over_budget_as_error():
    huge = "x" * (viz.BUDGET_TOTAL * 4 + 8000)
    issues = viz.lint_decision(huge, {"q": "y"}, distribution={"a": 1.0}, selected="a")
    codes = {i.code for i in issues}
    assert any(c.startswith("token_over") for c in codes)
    assert any(i.level == "error" for i in issues if i.code.startswith("token_over"))


def test_lint_flags_fallback_and_empty_distribution():
    issues = viz.lint_decision({"a": 1}, {"q": "y"}, distribution={},
                               selected="a", is_fallback=True)
    codes = {i.code for i in issues}
    assert "verdict_is_fallback" in codes
    assert "distribution_empty" in codes


def test_lint_flags_unnormalised_distribution():
    issues = viz.lint_decision({"a": 1}, {"q": "y"},
                               distribution={"a": 0.5, "b": 0.2}, selected="a")
    assert "distribution_unnormalised" in {i.code for i in issues}


def test_lint_flags_selected_missing_from_distribution():
    issues = viz.lint_decision({"a": 1}, {"q": "y"},
                               distribution={"a": 0.6, "b": 0.4}, selected="zzz")
    assert "selected_not_in_distribution" in {i.code for i in issues}


def test_lint_reports_truncation_as_info_only():
    dist = {f"k{i}": 1 / 20 for i in range(20)}
    issues = viz.lint_decision({"a": 1}, {"q": "y"}, distribution=dist,
                               selected="k0", top_n=12)
    trunc = [i for i in issues if i.code == "distribution_truncated"]
    assert len(trunc) == 1 and trunc[0].level == "info"


def test_lint_flags_low_confidence_but_not_for_fallback():
    low = viz.lint_decision({"a": 1}, {"q": "y"}, distribution={"a": 1.0},
                            selected="a", confidence=0.2)
    assert "confidence_low" in {i.code for i in low}
    fb = viz.lint_decision({"a": 1}, {"q": "y"}, distribution={},
                           selected="a", confidence=0.2, is_fallback=True)
    assert "confidence_low" not in {i.code for i in fb}


@pytest.mark.parametrize("scale", [[5, 1], ["a", "b"], [1], [], None])
def test_lint_scale_validation_never_raises(scale):
    issues = viz.lint_decision({"a": 1}, {"q": "y"}, distribution={"a": 1.0},
                               selected="a", scale=scale if scale is not None else [0, 1])
    assert isinstance(issues, list)


def test_lint_scale_inverted_is_an_error():
    issues = viz.lint_decision({"a": 1}, {"q": "y"}, distribution={"a": 1.0},
                               selected="a", scale=[5, 1])
    assert "scale_inverted" in {i.code for i in issues}


def test_lint_sorted_most_severe_first():
    huge = "x" * (viz.BUDGET_TOTAL * 4 + 8000)
    issues = viz.lint_decision(huge, {"q": "y"}, distribution={}, selected="a",
                               is_fallback=True)
    ranks = [{"error": 0, "warning": 1, "info": 2}[i.level] for i in issues]
    assert ranks == sorted(ranks)
    assert issues[0].level == "error"


def test_lint_summary_counts():
    issues = [viz.Issue("error", "a", "x"), viz.Issue("warning", "b", "y"),
              viz.Issue("warning", "c", "z"), viz.Issue("info", "d", "w")]
    assert viz.lint_summary(issues) == {"error": 1, "warning": 2, "info": 1, "total": 4}


def test_issue_to_dict_omits_empty_optionals():
    d = viz.Issue("info", "code", "msg").to_dict()
    assert d == {"level": "info", "code": "code", "message": "msg"}
    d2 = viz.Issue("info", "code", "msg", question_id="q1", params={"n": 1}).to_dict()
    assert d2["question_id"] == "q1" and d2["params"] == {"n": 1}


# ---------------------------------------------------------------------------
# build_report — the frontend contract
# ---------------------------------------------------------------------------
def test_build_report_returns_the_three_frontend_sections():
    report = viz.build_report(
        {"blind": {"target": 300}},
        {"balatro_action": {"play_0": "打顺子"}},
        primitive="choice",
        distribution={"play_0": 0.8, "discard_0": 0.2},
        selected="play_0",
        confidence=0.8,
        options=["play_0", "discard_0"],
    )
    assert set(report) == {"visual", "tokens", "lint", "thresholds"}
    assert report["visual"]["kind"] == "choice"
    assert report["tokens"]["n_questions"] == 1
    assert "summary" in report["lint"] and "issues" in report["lint"]
    assert report["thresholds"]["confidence_high"] == viz.CONFIDENCE_HIGH


def test_build_report_without_primitive_has_no_visual_but_still_tokens_and_lint():
    report = viz.build_report({"a": 1}, {"q": "y"})
    assert report["visual"] is None
    assert report["tokens"]["total"] > 0
    assert isinstance(report["lint"]["issues"], list)


def test_build_report_is_json_serialisable():
    import json
    report = viz.build_report({"盲注": {"目标": 300}}, {"q": "选一个"},
                              primitive="choice", distribution={"a": 1.0}, selected="a")
    json.dumps(report, ensure_ascii=False)   # must not raise


def test_build_report_never_raises_on_hostile_input():
    report = viz.build_report(None, None, primitive=object(),
                              distribution={"a": object()}, selected=object())
    assert isinstance(report, dict)