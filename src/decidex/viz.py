"""
DecideX visualization model: turn a decision into something a human can see.

Design reference: PlayJev's `packages/core` split — the *model* lives here as
pure, platform-agnostic functions (no web framework, no DOM, no pydantic), and
each frontend only has to draw the returned dict. Three pieces, mirroring
PlayJev's `tokens/estimate.ts`, `lint/rules.ts` and `shared.tsx`:

  * :func:`estimate_tokens` / :func:`token_report` — budget accounting, so an
    oversized state or question set is visible *before* it is sent.
  * :func:`lint_decision` — aggregated pre-send findings (PlayJev `Issue`).
  * :func:`build_visual` — render-ready spec for the three System One
    primitives (choice / score / noul): probability bars, three-range
    confidence band, and per-primitive extras.

Read-only red line: nothing here influences a decision. Every function is a
pure transform of data the engine already produced; the frontend may draw it or
ignore it, and the advisor's output is byte-identical either way.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Confidence banding (PlayJev docs/jev/confidence.md)
# ---------------------------------------------------------------------------
# Three ranges, three behaviors: high -> act, medium -> proceed with caution /
# flag for review, low -> do not act, route to a human. The thresholds are
# *policy*, not physics, so every entry point takes them as arguments.
CONFIDENCE_HIGH = 0.9
CONFIDENCE_LOW = 0.5


def confidence_tier(
    confidence: float,
    high: float = CONFIDENCE_HIGH,
    low: float = CONFIDENCE_LOW,
) -> str:
    """Band a [0,1] confidence into ``"high"`` / ``"medium"`` / ``"low"``."""
    try:
        value = float(confidence)
    except (TypeError, ValueError):
        return "low"
    if math.isnan(value):
        return "low"
    if value >= high:
        return "high"
    if value >= low:
        return "medium"
    return "low"


_TIER_LABEL = {"high": "高置信", "medium": "需复核", "low": "低置信"}
_TIER_ADVICE = {
    "high": "可直接采纳",
    "medium": "建议人工复核后再采纳",
    "low": "证据不足,请人工决策",
}
_TIER_COLOR = {"high": "good", "medium": "warn", "low": "bad"}


def confidence_badge(
    confidence: float,
    high: float = CONFIDENCE_HIGH,
    low: float = CONFIDENCE_LOW,
    *,
    is_fallback: bool = False,
) -> Dict[str, Any]:
    """Badge spec: tier, human label, advice, bar ratio, colour key."""
    tier = confidence_tier(confidence, high, low)
    try:
        ratio = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        ratio = 0.0
    return {
        "value": round(ratio, 4),
        "ratio": round(ratio, 4),
        "tier": tier,
        "label": _TIER_LABEL[tier],
        "advice": _TIER_ADVICE[tier],
        "color": _TIER_COLOR[tier],
        "thresholds": {"high": float(high), "low": float(low)},
        # A fallback is not a judgement at all — the model did not speak. The
        # frontend shows it in its own colour so the tier is not misread as a
        # (very low) confidence in a model recommendation.
        "is_fallback": bool(is_fallback),
    }


# ---------------------------------------------------------------------------
# Token budgeting (PlayJev packages/core/src/tokens/estimate.ts)
# ---------------------------------------------------------------------------
BUDGET_TOTAL = 64_000
BUDGET_STATE_PLUS_LONGEST = 32_000

# A budget utilisation above this ratio is reported as "near" — worth a warning
# but not a hard stop (PlayJev `token_near_budget`).
NEAR_BUDGET_RATIO = 0.9


def estimate_tokens(text: Any) -> int:
    """
    Cheap, provider-agnostic token estimate.

    CJK codepoints are counted 1:1 (one glyph is one token in practice) and
    everything else at 4 characters per token. Deliberately approximate: it
    exists to catch order-of-magnitude blowups before a request is sent, not to
    predict a bill.
    """
    if text is None:
        return 0
    if not isinstance(text, str):
        text = str(text)
    cjk = 0
    other = 0
    for ch in text:
        if "\u3000" <= ch <= "\u9fff" or "\uff00" <= ch <= "\uffef":
            cjk += 1
        else:
            other += 1
    return cjk + math.ceil(other / 4)


def _payload_text(value: Any) -> str:
    """JSON-ish rendering for token counting (never raises on odd types)."""
    import json

    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


def token_report(
    state: Any,
    questions: Any,
    *,
    budget_total: int = BUDGET_TOTAL,
    budget_state_plus_longest: int = BUDGET_STATE_PLUS_LONGEST,
    near_ratio: float = NEAR_BUDGET_RATIO,
) -> Dict[str, Any]:
    """
    Token accounting for one System One request.

    ``questions`` is either a mapping ``{id: spec}`` (the wire shape built by
    providers/typesafe.py) or an iterable of specs. Which budget binds is
    reported explicitly: PlayJev caps the whole request *and* the state plus the
    longest single question, because one oversized head can sink a request even
    when the total looks fine.
    """
    state_tokens = estimate_tokens(_payload_text(state))

    if isinstance(questions, dict):
        items: List[Any] = list(questions.values())
    elif questions is None:
        items = []
    else:
        items = list(questions)

    per_question = [estimate_tokens(_payload_text(q)) for q in items]
    longest = max(per_question) if per_question else 0
    total = state_tokens + sum(per_question)

    candidates: List[Tuple[str, int, int]] = [
        ("total", total, int(budget_total)),
        ("state_plus_longest", state_tokens + longest, int(budget_state_plus_longest)),
    ]
    # Report the tightest constraint first; that is the one that will bite.
    binding_kind, binding_used, binding_budget = max(
        candidates, key=lambda c: (c[1] / c[2]) if c[2] > 0 else 0.0
    )
    ratio = (binding_used / binding_budget) if binding_budget > 0 else 0.0
    if ratio > 1.0:
        level = "over"
    elif ratio >= near_ratio:
        level = "near"
    else:
        level = "ok"

    return {
        "state": state_tokens,
        "questions": sum(per_question),
        "longest_question": longest,
        "total": total,
        "n_questions": len(items),
        "per_question": per_question,
        "budgets": {
            "total": int(budget_total),
            "state_plus_longest": int(budget_state_plus_longest),
        },
        "binding": {
            "kind": binding_kind,
            "used": int(binding_used),
            "budget": int(binding_budget),
            "ratio": round(ratio, 4),
            "level": level,
        },
    }


# ---------------------------------------------------------------------------
# Lint (PlayJev packages/core/src/lint/rules.ts)
# ---------------------------------------------------------------------------
@dataclass
class Issue:
    """One lint finding. ``code`` is stable; ``message`` is for humans."""

    level: str  # "error" | "warning" | "info"
    code: str
    message: str
    question_id: Optional[str] = None
    params: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"level": self.level, "code": self.code, "message": self.message}
        if self.question_id is not None:
            out["question_id"] = self.question_id
        if self.params:
            out["params"] = self.params
        return out


_LEVEL_RANK = {"error": 0, "warning": 1, "info": 2}


def _normalise_distribution(distribution: Any) -> Dict[str, float]:
    """Coerce a raw distribution into ``{key: float}``, dropping junk."""
    if not isinstance(distribution, dict):
        return {}
    out: Dict[str, float] = {}
    for key, value in distribution.items():
        try:
            out[str(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return out


def prob_bars(
    distribution: Any,
    selected: Any = None,
    *,
    top_n: Optional[int] = None,
    labels: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """
    Sort a distribution descending into render-ready bars.

    ``ratio`` is the raw probability (the frontend scales it to a bar width);
    ``share`` is the same number renormalised against the shown mass, so a
    truncated top-N view still fills the track. Ordering is stable by key so two
    equal probabilities do not swap between polls (which reads as flicker).
    """
    dist = _normalise_distribution(distribution)
    if not dist:
        return []

    selected_key = None if selected is None else str(selected)
    rows = sorted(dist.items(), key=lambda kv: (-kv[1], kv[0]))
    if top_n is not None and top_n >= 0:
        rows = rows[:top_n]

    shown_total = sum(v for _, v in rows)
    bars: List[Dict[str, Any]] = []
    for key, value in rows:
        ratio = max(0.0, min(1.0, value))
        bars.append({
            "key": key,
            "label": (labels or {}).get(key, key),
            "ratio": round(ratio, 4),
            "percent": round(ratio * 100, 1),
            "share": round(ratio / shown_total, 4) if shown_total > 0 else 0.0,
            "selected": selected_key is not None and key == selected_key,
        })
    if bars:
        bars[0]["winner"] = True
    return bars


def _entropy(dist: Dict[str, float]) -> float:
    """Normalised Shannon entropy in [0,1]; 1.0 == flat, 0.0 == certain."""
    probs = [p for p in dist.values() if p > 0]
    if len(probs) <= 1:
        return 0.0
    total = sum(probs)
    if total <= 0:
        return 0.0
    probs = [p / total for p in probs]
    h = -sum(p * math.log(p) for p in probs)
    return round(h / math.log(len(probs)), 4)


def marginal(dist: Dict[str, float]) -> float:
    """Top-1 minus top-2 probability: how far clear the leader is."""
    if len(dist) < 2:
        return 0.0
    ordered = sorted(dist.values(), reverse=True)
    return round(ordered[0] - ordered[1], 4)


def build_visual(
    primitive: Any,
    distribution: Any = None,
    selected: Any = None,
    *,
    confidence: Optional[float] = None,
    options: Optional[Iterable[Any]] = None,
    labels: Optional[Dict[str, str]] = None,
    scale: Optional[Sequence[float]] = None,
    is_fallback: bool = False,
    high: float = CONFIDENCE_HIGH,
    low: float = CONFIDENCE_LOW,
    top_n: int = 12,
) -> Dict[str, Any]:
    """
    Render-ready visualization of one verdict.

    ``primitive`` accepts a :class:`~decidex.types.PrimitiveType` or its string
    value. The returned dict always carries ``kind``, ``bars``, ``confidence``
    (badge spec) and ``stats``; the per-primitive block differs:

      choice -> ``bars`` is the option distribution (winner first)
      score  -> ``marker`` positions the selected level on a 0..n-1 tick scale
      noul   -> ``p_yes`` plus a ``zone`` (strong_no / uncertain / strong_yes)

    ``confidence`` defaults to the mass on ``selected`` (the posterior the model
    actually assigned), not to whatever flowery number a provider returned: if
    the model put 0.4 on its own pick, that is the honest number to show.
    """
    kind = getattr(primitive, "value", primitive)
    kind = str(kind).lower()

    dist = _normalise_distribution(distribution)
    option_keys = [str(o) for o in options] if options is not None else []

    # Options can carry probability mass that the model never mentioned (or the
    # model can invent a key). Union both, so the bar list matches the option
    # label the player sees in the legal-action panel.
    if option_keys:
        for key in option_keys:
            dist.setdefault(key, 0.0)

    conf_value = confidence
    if conf_value is None:
        if selected is not None and str(selected) in dist:
            conf_value = dist[str(selected)]
        elif dist:
            conf_value = max(dist.values())
        else:
            conf_value = 0.0

    visual: Dict[str, Any] = {
        "kind": kind,
        "selected": None if selected is None else str(selected),
        "confidence": confidence_badge(conf_value, high, low, is_fallback=is_fallback),
        "stats": {
            "n_options": len(dist) if dist else len(option_keys),
            "entropy": _entropy(dist),
            "margin": marginal(dist),
            "mass_on_selected": round(dist.get(str(selected), 0.0), 4) if selected is not None else None,
        },
        "empty": not dist,
    }

    if kind == "noul":
        p_yes = dist.get("true", dist.get("yes", 0.0))
        if selected is not None and "true" not in dist and "yes" not in dist:
            p_yes = 1.0 if str(selected).lower() in ("true", "1", "yes") else 0.0
        p_yes = max(0.0, min(1.0, p_yes))
        # PlayJev NoulAnswerCard: "uncertain" is a band around 0.5, not a
        # threshold on either side.
        if abs(p_yes - 0.5) < 0.15:
            zone = "uncertain"
        elif p_yes >= 0.5:
            zone = "strong_yes"
        else:
            zone = "strong_no"
        visual["noul"] = {
            "p_yes": round(p_yes, 4),
            "p_no": round(1.0 - p_yes, 4),
            "zone": zone,
        }
        visual["bars"] = prob_bars({"true": p_yes, "false": 1.0 - p_yes}, selected,
                                   labels={"true": "是", "false": "否"})
        return visual

    if kind == "score":
        bars = prob_bars(dist, selected, labels=labels, top_n=None)
        n = len(bars)
        # System One returns a weighted level *index*; the frontend marks it on
        # a discrete tick scale rather than a continuous gauge.
        marker = None
        if selected is not None and n > 1:
            try:
                idx = float(selected)
                marker = round(max(0.0, min(1.0, idx / (n - 1))), 4)
            except (TypeError, ValueError):
                for i, bar in enumerate(bars):
                    if bar["key"] == str(selected):
                        marker = round(i / (n - 1), 4) if n > 1 else 0.0
                        break
        visual["score"] = {
            "marker": marker,
            "n_levels": n,
            "scale": list(scale) if scale else None,
        }
        visual["bars"] = bars
        return visual

    # choice (default)
    visual["bars"] = prob_bars(dist, selected, labels=labels, top_n=top_n)
    return visual


# ---------------------------------------------------------------------------
# Aggregated lint
# ---------------------------------------------------------------------------
def lint_decision(
    state: Any,
    questions: Any,
    *,
    distribution: Any = None,
    selected: Any = None,
    confidence: Optional[float] = None,
    scale: Optional[Sequence[float]] = None,
    is_fallback: bool = False,
    top_n: int = 12,
    budget_total: int = BUDGET_TOTAL,
    budget_state_plus_longest: int = BUDGET_STATE_PLUS_LONGEST,
    near_ratio: float = NEAR_BUDGET_RATIO,
) -> List[Issue]:
    """
    Pre-send health check for one request plus its verdict.

    Sorted most-severe first so a UI can show the top findings without
    reshuffling. Never raises: this runs on the request path.
    """
    issues: List[Issue] = []
    report = token_report(state, questions, budget_total=budget_total,
                          budget_state_plus_longest=budget_state_plus_longest,
                          near_ratio=near_ratio)

    kinds = questions.keys() if isinstance(questions, dict) else []
    if not kinds:
        issues.append(Issue("error", "no_questions", "本次请求没有任何问题(questions 为空)"))
    # Test emptiness of the value itself, not its token count: an empty dict
    # still serialises to "{}" and would otherwise slip through as "non-zero".
    if not state:
        issues.append(Issue("error", "state_empty", "证据(state)为空,模型将无从判断"))

    binding = report["binding"]
    if binding["level"] == "over":
        issues.append(Issue(
            "error",
            "token_over_{}".format("total" if binding["kind"] == "total" else "state"),
            "Token 超预算:{} 用了 {}/{}".format(
                "总请求" if binding["kind"] == "total" else "证据+最长问题",
                binding["used"], binding["budget"]),
            params=binding,
        ))
    elif binding["level"] == "near":
        issues.append(Issue(
            "warning",
            "token_near_budget",
            "Token 接近预算上限({}/{},{:.0%})".format(
                binding["used"], binding["budget"], binding["ratio"]),
            params=binding,
        ))

    if is_fallback:
        issues.append(Issue("warning", "verdict_is_fallback",
                            "本结论是回落结果,不是模型判断,置信度不具可比性"))

    dist = _normalise_distribution(distribution)
    if not dist:
        issues.append(Issue(
            "warning", "distribution_empty",
            "模型未返回概率分布,无法展示置信依据" if not is_fallback else "回落结论没有分布(属正常)"))
    else:
        total = sum(dist.values())
        # Probabilities are a distribution: if they do not sum to ~1 the
        # confidence number on screen is not a posterior and should not be
        # presented as one.
        if total > 0 and abs(total - 1.0) > 0.02:
            issues.append(Issue(
                "warning", "distribution_unnormalised",
                "概率分布和不为 1(合计 {:.4f}),置信度仅供参考".format(total),
                params={"total": round(total, 4)}))
        if selected is not None and str(selected) not in dist:
            issues.append(Issue(
                "warning", "selected_not_in_distribution",
                "被选中的动作不在概率分布里:{}".format(selected)))
        if len(dist) > top_n:
            issues.append(Issue(
                "info", "distribution_truncated",
                "概率分布共 {} 项,界面只展示前 {} 项".format(len(dist), top_n),
                params={"n": len(dist), "top_n": top_n}))

    if confidence is not None:
        tier = confidence_tier(confidence)
        if tier == "low" and not is_fallback:
            issues.append(Issue(
                "warning", "confidence_low",
                "置信度 {:.0%} 低于低置信阈值,建议人工决策".format(float(confidence))))

    if scale is not None:
        try:
            lo, hi = float(scale[0]), float(scale[1])
            if hi <= lo:
                issues.append(Issue("error", "scale_inverted",
                                    "评分区间无效:[{}, {}]".format(lo, hi)))
        except (TypeError, ValueError, IndexError):
            issues.append(Issue("error", "scale_invalid", "评分区间无法解析:{}".format(scale)))

    issues.sort(key=lambda i: (_LEVEL_RANK.get(i.level, 9), i.code))
    return issues


def lint_summary(issues: Sequence[Issue]) -> Dict[str, int]:
    """Counts by level, for a compact HUD badge."""
    counts = {"error": 0, "warning": 0, "info": 0}
    for issue in issues:
        counts[issue.level] = counts.get(issue.level, 0) + 1
    counts["total"] = len(issues)
    return counts


def build_report(
    state: Any,
    questions: Any,
    *,
    primitive: Any = None,
    distribution: Any = None,
    selected: Any = None,
    confidence: Optional[float] = None,
    options: Optional[Iterable[Any]] = None,
    labels: Optional[Dict[str, str]] = None,
    scale: Optional[Sequence[float]] = None,
    is_fallback: bool = False,
    top_n: int = 12,
) -> Dict[str, Any]:
    """
    The single entry point a frontend needs: ``{"visual", "tokens", "lint"}``.

    Everything the viewer draws comes from here, so the Python model stays the
    one source of truth and the two HTML pages share a contract instead of
    duplicating presentation logic.
    """
    issues = lint_decision(
        state, questions,
        distribution=distribution, selected=selected, confidence=confidence,
        scale=scale, is_fallback=is_fallback, top_n=top_n,
    )
    return {
        "visual": build_visual(
            primitive, distribution, selected,
            confidence=confidence, options=options, labels=labels, scale=scale,
            is_fallback=is_fallback, top_n=top_n,
        ) if primitive is not None else None,
        "tokens": token_report(state, questions),
        "lint": {
            "issues": [i.to_dict() for i in issues],
            "summary": lint_summary(issues),
        },
        "thresholds": {"confidence_high": CONFIDENCE_HIGH, "confidence_low": CONFIDENCE_LOW,
                       "budget_total": BUDGET_TOTAL,
                       "budget_state_plus_longest": BUDGET_STATE_PLUS_LONGEST},
    }