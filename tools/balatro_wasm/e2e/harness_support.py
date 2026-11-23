"""Shared validation and browser-side event helpers for Balatro E2E harnesses."""

from __future__ import annotations

import math
import os
import re


_VIEWPORT_RE = re.compile(r"^(\d+)x(\d+)$")


def positive_finite_float(name: str, default: float) -> float:
    """Read a strictly positive, finite float from an environment variable."""
    raw = os.environ.get(name, str(default))
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite number > 0, got {raw!r}") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite number > 0, got {raw!r}")
    return value


def positive_viewport(name: str, default: str) -> dict[str, int]:
    """Read a WIDTHxHEIGHT viewport with positive integer dimensions."""
    raw = os.environ.get(name, default)
    match = _VIEWPORT_RE.fullmatch(raw)
    if not match:
        raise ValueError(f"{name} must match WIDTHxHEIGHT with positive integers, got {raw!r}")
    width, height = (int(part) for part in match.groups())
    if width <= 0 or height <= 0:
        raise ValueError(f"{name} must match WIDTHxHEIGHT with positive integers, got {raw!r}")
    return {"width": width, "height": height}


def scale_dwell_budget(budget: float, hand_size: int) -> float:
    """Stretch a dwell budget for oversized hands, capped at 2x.

    Scoring animations and decision work grow with the number of cards held;
    past ~12 cards a healthy state can legitimately outlive the normal budget.
    Without this, an endless run with a 49-card hand reports "slow" as
    "wedged" (observed: 3 false stalls in one 2-game soak).

    Observability-only since the stall-budget dedup: hand-scale stretching
    multiplied the false-negative risk (a 49-card hand doubled every budget,
    so a real stall had to sit for 60s before being reported). Use
    effective_stall_budget for stall *decisions*; keep this only for
    "slow but healthy" reporting.
    """
    extra = max(0, int(hand_size or 0) - 12)
    return min(budget * 2.0, budget * (1.0 + extra * 0.04))


# Fixed 30% grace on top of the base dwell budget for stall decisions.
# The old path multiplied the base budget by a hand-size factor AND a grace
# factor, so deep-ante 49-card hands got a 2.2x inflated budget and a true
# wedge sat undetected for minutes. The base budgets already encode the
# worst observed *healthy* dwell per state (46-80s); a flat 30% grace covers
# timer jitter without scaling with hand count.
STALL_GRACE_FACTOR = 1.3


def effective_stall_budget(budget: float) -> float:
    """Stall-decision budget: base budget * fixed 1.3 grace, no hand scaling.

    `budget` is the per-state DWELL_BUDGETS entry (0 allowed -> 0: a zero
    budget wedges immediately, grace cannot revive it).
    """
    return round(max(0.0, float(budget)) * STALL_GRACE_FACTOR)


def hard_action_failure_info(text: str, transient_re) -> str | None:
    """Return the info payload if a DECIDEX_ACTION_RESULT line is a hard failure.

    `transient_re` is the caller's transient classifier (the soak's
    TRANSIENT_INFO_RE, mirrored from app.js). Rejections it matches are
    ignored exactly as the soak classifier ignores them; bridge-emitted
    benign rejections (BENIGN_REJECTION_RE) are ignored the way the JS
    client ignores them; everything else with ok=false is a real action
    failure that must fail the probe.
    """
    if classify_action_rejection(text, transient_re) == "hard":
        return text.split("info=", 1)[1].strip() if "info=" in text else ""
    return None


# Bridge-emitted rejections the JS client recovers from on its own but that
# its isTransient regex deliberately does NOT cover (app.js handles them in
# the adjacent benign-notice branch of onActionDispatchFailure / the
# log-only onActionResult path). The soak used to classify them as hard
# failures, contradicting the self-recovery evidence in its own comments
# (soak3: "card not found at index ... game kept advancing 30s afterwards").
# These are recorded separately as benign rejections, never as verdict
# failures.
BENIGN_REJECTION_RE = re.compile(
    r"card not found at index \d+"          # buy/use retry racing area rebuild
    r"|blind already selected \(animating\)"  # skip_blind racing select animation
    r"|already in run",                       # start_run on an already-running run
    re.IGNORECASE,
)


def classify_action_rejection(text: str, transient_re) -> str:
    """Classify a console line that may be a DECIDEX_ACTION_RESULT rejection.

    Returns one of:
      "none"     — not an ok=false action result
      "transient" — matches the JS-mirrored transient classifier
      "benign"   — bridge-emitted, client self-recovers (BENIGN_REJECTION_RE)
      "hard"     — everything else: a real failure for the verdict
    """
    if "[DECIDEX_ACTION_RESULT]" not in text or "ok=false" not in text:
        return "none"
    info = text.split("info=", 1)[1] if "info=" in text else ""
    if transient_re.search(info):
        return "transient"
    if BENIGN_REJECTION_RE.search(info):
        return "benign"
    return "hard"


def emit_probe_verdict(name: str, failures: list[str], pass_detail: str) -> None:
    """Print one PASS/FAIL line and exit 1 on any accumulated failure.

    Mirrors test_full_flow_soak.py's verdict rollup. Print-only probes used
    to exit 0 even when a pageerror or a hard action failure was printed;
    routing their verdict through here makes the exit code honest. Callers
    accumulate human-readable reasons (page errors, hard action failures,
    unmet progress milestones) and hand them here.
    """
    if failures:
        print(f"\n[FAIL] {name}: {', '.join(failures)}", flush=True)
        raise SystemExit(1)
    print(f"\n[PASS] {name}: {pass_detail}", flush=True)


# Install after the page's app.js has defined onBalatroStateUpdate and before
# AutoPilot is enabled. It counts terminal *edges*, not snapshots, so a busy
# browser cannot make Python miss a GAME_OVER/GAME_WON transition.
TERMINAL_LATCH_SCRIPT = r"""
() => {
  if (window.__dxTerminalLatchInstalled) return;
  const original = window.onBalatroStateUpdate;
  if (typeof original !== "function") {
    throw new Error("onBalatroStateUpdate is not available for terminal latch");
  }
  const latch = {
    total: 0,
    active: false,
    nextSeq: 0,
    events: [],
  };
  const wrapped = function (state) {
    const terminal = !!state && (
      state.state_name === "GAME_OVER" ||
      state.state_name === "GAME_WON" ||
      state.won === true
    );
    if (terminal && !latch.active) {
      latch.active = true;
      latch.total += 1;
      latch.events.push({
        seq: ++latch.nextSeq,
        state: state.state_name || (state.won ? "GAME_WON" : "UNKNOWN"),
        ante: state.ante,
        round: state.round,
        money: state.money,
      });
      if (latch.events.length > 32) latch.events.shift();
    } else if (!terminal) {
      latch.active = false;
    }
    return original.apply(this, arguments);
  };
  window.onBalatroStateUpdate = wrapped;
  window.__dxTerminalLatch = latch;
  window.__dxTerminalLatchInstalled = true;
};
"""

TERMINAL_LATCH_READ = r"""
() => {
  const latch = window.__dxTerminalLatch;
  if (!latch) throw new Error("terminal latch is not installed");
  return { total: latch.total, events: latch.events.slice(-8) };
}
"""
