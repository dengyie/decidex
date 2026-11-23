"""Deterministic checks for the browser E2E harness configuration and latch."""

from pathlib import Path
import subprocess
import sys

import pytest


E2E_DIR = Path(__file__).parents[1] / "tools" / "balatro_wasm" / "e2e"
sys.path.insert(0, str(E2E_DIR))

from harness_support import (  # noqa: E402
    TERMINAL_LATCH_READ,
    TERMINAL_LATCH_SCRIPT,
    effective_stall_budget,
    positive_finite_float,
    positive_viewport,
    scale_dwell_budget,
)


@pytest.mark.parametrize("name", ["PROBE_POLL", "PROBE_DIAG_EVERY", "BALATRO_SOAK_POLL"])
def test_positive_finite_float_accepts_harness_overrides(monkeypatch, name):
    monkeypatch.setenv(name, "0.25")
    assert positive_finite_float(name, 2) == 0.25


@pytest.mark.parametrize("name", ["PROBE_POLL", "PROBE_DIAG_EVERY", "BALATRO_SOAK_POLL"])
@pytest.mark.parametrize("raw", ["0", "-1", "nan", "inf", "-inf", "not-a-number"])
def test_positive_finite_float_rejects_busy_loop_values(monkeypatch, name, raw):
    monkeypatch.setenv(name, raw)
    with pytest.raises(ValueError, match=rf"{name}.*finite number > 0"):
        positive_finite_float(name, 2)


def test_positive_viewport_accepts_dimensions(monkeypatch):
    monkeypatch.setenv("TEST_VIEWPORT", "1024x576")
    assert positive_viewport("TEST_VIEWPORT", "800x600") == {"width": 1024, "height": 576}


@pytest.mark.parametrize("raw", ["0x576", "1024x0", "-1x576", "1024", "widexhigh"])
def test_positive_viewport_rejects_invalid_dimensions(monkeypatch, raw):
    monkeypatch.setenv("TEST_VIEWPORT", raw)
    with pytest.raises(ValueError, match=r"TEST_VIEWPORT.*WIDTHxHEIGHT"):
        positive_viewport("TEST_VIEWPORT", "800x600")


def test_terminal_latch_counts_edges_and_retains_events():
    """The browser latch survives missed polls and ignores repeated snapshots."""
    script = f"""
      global.window = {{}};
      const updates = [];
      window.onBalatroStateUpdate = (state) => updates.push(state.state_name);
      const install = {TERMINAL_LATCH_SCRIPT};
      install();
      const emit = (state_name, extra = {{}}) => window.onBalatroStateUpdate({{ state_name, ...extra }});
      emit("MENU");
      emit("GAME_OVER");
      emit("GAME_OVER");
      emit("MENU");
      emit("GAME_WON");
      emit("GAME_WON");
      emit("MENU");
      emit("UNKNOWN", {{ won: true }});
      const readLatch = {TERMINAL_LATCH_READ};
      console.log(JSON.stringify({{ latch: readLatch(), updates }}));
    """
    result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    import json

    payload = json.loads(result.stdout)
    latch = payload["latch"]
    assert latch["total"] == 3
    assert [event["seq"] for event in latch["events"]] == [1, 2, 3]
    assert [event["state"] for event in latch["events"]] == ["GAME_OVER", "GAME_WON", "UNKNOWN"]
    assert payload["updates"] == ["MENU", "GAME_OVER", "GAME_OVER", "MENU", "GAME_WON", "GAME_WON", "MENU", "UNKNOWN"]


@pytest.mark.parametrize(
    "hand_size,expected_factor",
    [
        (0, 1.0),
        (8, 1.0),
        (12, 1.0),
        (20, 1.32),
        (49, 2.0),
    ],
)
def test_scale_dwell_budget_stretches_for_oversized_hands(hand_size, expected_factor):
    """A big hand is slow, not wedged: budgets grow (capped at 2x)."""
    assert scale_dwell_budget(90, hand_size) == pytest.approx(90 * expected_factor)


def test_scale_dwell_budget_never_shrinks():
    assert scale_dwell_budget(75, -3) == 75


def test_effective_stall_budget_is_flat_30_percent_grace():
    assert effective_stall_budget(75) == round(75 * 1.3) == 98
    assert effective_stall_budget(60) == round(60 * 1.3) == 78
    assert effective_stall_budget(30) == 39
    assert effective_stall_budget(80) == 104


def test_effective_stall_budget_zero_stays_zero():
    """A zero budget means 'wedge immediately'; grace cannot revive it."""
    assert effective_stall_budget(0) == 0


def test_effective_stall_budget_ignores_hand_size():
    """The old hand-scale factor is gone: identical budgets regardless of hand."""
    assert effective_stall_budget(75) == 98
    # A 49-card hand does not stretch the stall budget (contrast with
    # scale_dwell_budget, which doubles it for observability only).
    assert effective_stall_budget(90) == round(90 * 1.3) == 117


def test_effective_stall_budget_large_value_and_negative_clamped():
    assert effective_stall_budget(1000) == 1300
    # Defensive: negative inputs clamp to the zero-budget behaviour.
    assert effective_stall_budget(-5) == 0
