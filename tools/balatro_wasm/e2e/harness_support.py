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
    """
    extra = max(0, int(hand_size or 0) - 12)
    return min(budget * 2.0, budget * (1.0 + extra * 0.04))


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
