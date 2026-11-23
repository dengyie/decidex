"""Boundary tests for tests/js_scoring_harness.mjs coverage.

The harness mirrors the *production* scoring engine (tools/balatro_wasm/web/
app.js) for replay/analysis only. These tests parse the harness source text
and assert two things:

1. The names the harness extracts/exposes (its NEEDS list and its factory
   API) still contain the core scoring set — most importantly
   ``evaluateHandScore``, the numerical golden tests hang on it.
2. The covered-name list does not shrink silently: it is pinned by a fixed
   subset and a fixed minimum count, so trimming a definition out of the
   harness (and thus out of numerical verification) fails here first.

No Node execution happens here; this is pure source-text extraction, so it
runs even where Node is unavailable (unlike test_balatro_scoring_js.py).
"""

from pathlib import Path

import pytest

HARNESS = Path(__file__).resolve().parent / "js_scoring_harness.mjs"
HARNESS_SRC = HARNESS.read_text(encoding="utf-8")

# The names a scoring harness must cover to be a faithful replay mirror of
# the production engine. evaluateHandScore is the load-bearing one.
CORE_REQUIRED = {
    "evaluateHandScore",
    "findBestPlays",
    "findBestDiscards",
    "getScoringCards",
    "isStraight",
    "detectConstraints",
    "arbitrateAction",
    # module-level constants the math depends on
    "RANK_VALUES",
    "POKER_HANDS",
}
MIN_COVERED_NAMES = 16  # harness currently carries 18 NEEDS entries; a drop
                        # below this means scoring coverage was silently cut.


def _extract_needs_list(src: str) -> list[str]:
    """Pull the identifiers out of the harness's NEEDS array literal."""
    start = src.index("const NEEDS = [") + len("const NEEDS = [")
    end = src.index("]", start)
    body = src[start:end]
    return [tok.strip().strip('"').strip("'") for tok in body.split(",") if tok.strip()]


def _extract_factory_api(src: str) -> list[str]:
    """Pull the exported API names from the harness's `return { ... }` factory."""
    marker = 'return { RANK_NUM,'
    start = src.index(marker) + len("return {")
    end = src.index("}", start)
    body = src[start:end]
    return [tok.strip().strip('"').strip("'") for tok in body.split(",") if tok.strip()]


def test_harness_file_exists():
    assert HARNESS.is_file()


def test_harness_carries_replay_only_declaration():
    """S-1 boundary: the harness must state it is not the production engine."""
    assert "线上运行的策略实现位于 tools/balatro_wasm/web/app.js" in HARNESS_SRC


def test_needs_list_contains_core_scoring_set():
    needs = _extract_needs_list(HARNESS_SRC)
    missing = CORE_REQUIRED - set(needs)
    assert not missing, f"harness NEEDS list lost core names: {sorted(missing)}"


def test_factory_api_exposes_core_functions():
    api = _extract_factory_api(HARNESS_SRC)
    for name in ("evaluateHandScore", "findBestPlays", "findBestDiscards",
                 "getScoringCards", "isStraight"):
        assert name in api, f"harness factory API no longer exposes {name}"


def test_needs_list_not_silently_shrunk():
    needs = _extract_needs_list(HARNESS_SRC)
    assert len(needs) >= MIN_COVERED_NAMES, (
        f"harness covers only {len(needs)} definitions (< {MIN_COVERED_NAMES}); "
        "scoring coverage was reduced without updating the pinned minimum"
    )
    # and no empty/garbage entries may sneak in
    assert all(name.replace("_", "").isalnum() for name in needs)


def test_harness_still_references_production_app_js():
    """The harness extracts from the production file — that linkage must stay."""
    assert "tools/balatro_wasm/web/app.js" in HARNESS_SRC


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(pytest.main([__file__, "-q"]))
