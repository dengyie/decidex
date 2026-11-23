"""Parity check: the three "transient rejection" classifiers agree.

Single source of truth is the JS isTransient / isStateMismatch pair in
tools/balatro_wasm/web/app.js. The Python harnesses (test_full_flow_soak.py,
test_multi_round_prod.py) must mirror it word for word, including the
deliberate omission of "use_card failed": the vanilla crash behind it
("interval is empty" on an empty hand) is a hard failure the caller must
see, not a readiness gate.

This test extracts all three criteria from source (no copies of the
patterns live here) and replays a shared corpus through one classifier
function, so a divergence in any direction (missing pattern, extra
pattern) fails loudly.
"""

from __future__ import annotations

from pathlib import Path
import re
import sys

import pytest


REPO_ROOT = Path(__file__).parents[1]
E2E_DIR = REPO_ROOT / "tools" / "balatro_wasm" / "e2e"
APP_JS = REPO_ROOT / "tools" / "balatro_wasm" / "web" / "app.js"

sys.path.insert(0, str(E2E_DIR))

from harness_support import classify_action_rejection  # noqa: E402
from test_full_flow_soak import TRANSIENT_INFO_RE as SOAK_RE  # noqa: E402
from test_multi_round_prod import TRANSIENT_INFO_RE as MULTI_ROUND_RE  # noqa: E402

# Corpus: (info string, expected transient classification).
# True  -> transient wait / state mismatch, must NOT count as a hard failure.
# False -> hard failure, MUST count (and "use_card failed" is the keystone
# negative: deleting it from the JS side used to swallow the empty-hand
# vanilla crash entirely).
CORPUS: list[tuple[str, bool]] = [
    # --- keystone negatives (hard failures) ---
    ("use_card failed: interval is empty", False),
    ("use_card failed", False),
    ("use_card failed: attempt to index a nil value", False),
    ("card not found at index 3", False),
    ("blind already selected", False),
    ("already in run", False),
    ("unknown action", False),
    ("pack closed before selection", False),
    # --- readiness / animation family (transient) ---
    ("game not ready", True),
    ("animating", True),
    ("play discarded: debounced", True),
    ("hand in progress", True),
    ("bridge busy", True),
    ("state locked", True),
    ("waiting for settlement", True),
    # --- play-family rejections (transient) ---
    ("card already in play", True),
    ("hand blocks play", True),
    ("joker slots full", True),
    # --- pack family (transient) ---
    ("pack card not found", True),
    ("no pack_cards", True),
    ("not in a pack state", True),
    ("no space to buy", True),
    # --- misc gates (transient) ---
    ("no-op", True),
    ("state not set", True),
    ("acknowledgement timed out", True),
    # --- state mismatches (transient in both JS and Python) ---
    ("not in BLIND_SELECT", True),
    ("not in SHOP", True),
    ("not in ROUND_EVAL", True),
    ("not in SELECTING_HAND", True),
    ("not in HAND_PLAYED", True),
    ("action rejected: not in new_round", True),
    # --- hard-failure controls that must not leak into transient ---
    ("play_card failed: hand is empty", False),
    ("select_pack_card failed: wrong state", False),
]


def _extract_js_regexes(source: str) -> list[re.Pattern]:
    """Pull the two /.../i literals: isStateMismatch then isTransient.

    They are two separate declarations in app.js -- isStateMismatch first
    (its own const), then `const isTransient = isStateMismatch || /.../i`.
    """
    def _literal(anchor: str) -> re.Pattern:
        start = source.index(anchor)
        chunk = source[start : start + 2000]
        literals = re.findall(r"/((?:[^/\\\n]|\\.)+)/(i)\b", chunk)
        assert literals, f"no /.../i regex literal after {anchor!r}"
        body, flags = literals[0]
        return re.compile(body, re.IGNORECASE if flags == "i" else 0)

    mismatch_re = _literal("const isStateMismatch = /")
    transient_re = _literal("const isTransient = isStateMismatch ||")
    return [mismatch_re, transient_re]


def classify_is_transient(patterns: list[re.Pattern], info: str) -> bool:
    """One classification implementation shared by every source's replay."""
    return any(p.search(info) for p in patterns)


def _js_patterns() -> list[re.Pattern]:
    source = APP_JS.read_text(encoding="utf-8")
    return _extract_js_regexes(source)


def test_js_source_contains_expected_patterns():
    """Guard the extraction itself: the keystone patterns exist in app.js."""
    source = APP_JS.read_text(encoding="utf-8")
    assert "const isTransient = isStateMismatch ||" in source
    assert "acknowledgement timed out" in source
    assert "not in (BLIND_SELECT|SHOP|ROUND_EVAL|SELECTING_HAND|HAND_PLAYED|new_round)" in source
    # "use_card failed" must not appear inside either extracted regex
    # literal itself (comments mentioning it are fine and expected).
    mismatch_re, transient_re = _js_patterns()
    assert "use_card failed" not in transient_re.pattern
    assert "use_card failed" not in mismatch_re.pattern


def test_soak_regex_matches_js_wordlist():
    """The soak regex must contain every JS pattern word."""
    js_words = {w for p in _js_patterns() for w in p.pattern.split("|")}
    soak_words = {w for w in SOAK_RE.pattern.split("|")}
    missing = js_words - soak_words
    assert not missing, f"soak TRANSIENT_INFO_RE is missing JS patterns: {sorted(missing)}"


def test_multi_round_regex_matches_js_wordlist():
    js_words = {w for p in _js_patterns() for w in p.pattern.split("|")}
    mr_words = {w for w in MULTI_ROUND_RE.pattern.split("|")}
    missing = js_words - mr_words
    assert not missing, f"multi_round TRANSIENT_INFO_RE is missing JS patterns: {sorted(missing)}"


@pytest.mark.parametrize("info,expected", CORPUS, ids=[c[0][:40] for c in CORPUS])
def test_all_three_classifiers_agree_on_corpus(info, expected):
    js = classify_is_transient(_js_patterns(), info)
    soak = classify_is_transient([SOAK_RE], info)
    multi = classify_is_transient([MULTI_ROUND_RE], info)
    assert js == expected, f"JS isTransient misclassifies {info!r}: got {js}, want {expected}"
    assert soak == expected, f"soak regex misclassifies {info!r}: got {soak}, want {expected}"
    assert multi == expected, f"multi_round regex misclassifies {info!r}: got {multi}, want {expected}"


def test_corpus_size_and_keystones():
    assert len(CORPUS) >= 30
    keystones = {info for info, _ in CORPUS}
    assert "use_card failed: interval is empty" in keystones
    assert any("debounced" in info for info in keystones)
    assert "acknowledgement timed out" in keystones
    assert "not in BLIND_SELECT" in keystones


# Harness-verdict layer: the soak/probes additionally route bridge-emitted
# benign rejections (client self-recovers; JS handles them in the
# benign-notice branch / log-only path) into their own bucket instead of
# failing the verdict. "blind already selected (animating)" is transient by
# wordlist; the other two are benign-only.
BENIGN_CASES = [
    "card not found at index 3",
    "already in run",
    "blind already selected (animating)",
]


@pytest.mark.parametrize("info", BENIGN_CASES, ids=lambda s: s[:40])
def test_bridge_self_recovering_rejections_never_fail_verdict(info):
    """Every one of these must classify as transient-or-benign for the verdict.

    Note the JS isTransient wordlist itself deliberately does NOT match
    "card not found at index N" / "already in run" — that is the point:
    the JS client recovers from them in its adjacent benign-notice branch,
    and the harness must mirror that tolerance at the verdict layer, not
    pretend they are wordlist-transient.
    """
    verdict = classify_action_rejection(f"[DECIDEX_ACTION_RESULT] ok=false info={info}", SOAK_RE)
    assert verdict in ("transient", "benign")
    assert classify_action_rejection(f"[DECIDEX_ACTION_RESULT] ok=false info={info}", MULTI_ROUND_RE) in ("transient", "benign")


def test_hard_verdict_keystones_still_hard():
    """The classifier must stay strict on genuine failures."""
    for info in ("use_card failed: interval is empty", "some novel failure"):
        assert classify_action_rejection(f"[DECIDEX_ACTION_RESULT] ok=false info={info}", SOAK_RE) == "hard"
