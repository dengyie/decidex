from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SHARED_CSS = ROOT / "tools" / "web_shared" / "viz.css"
WASM_CSS = ROOT / "tools" / "balatro_wasm" / "web" / "viz.css"


def test_wasm_visualization_styles_match_shared_source():
    assert WASM_CSS.read_bytes() == SHARED_CSS.read_bytes()


def test_probability_rows_allow_narrow_theme_adaptive_columns():
    css = SHARED_CSS.read_text(encoding="utf-8")
    bar_rule = re.search(r"\.vz-bar\s*\{([^}]*)\}", css)

    assert bar_rule is not None
    assert re.search(
        r"grid-template-columns:\s*minmax\(0,\s*88px\)\s+minmax\(0,\s*1fr\)\s+46px",
        bar_rule.group(1),
    )
    assert "--vz-good:var(--good,var(--green," in css
    assert "--vz-warn:var(--warn,var(--gold," in css
    assert "--vz-bad:var(--bad,var(--red," in css
    assert re.search(r"color-mix\(in srgb,\s*var\(--vz-good", css)
    assert re.search(r"color-mix\(in srgb,\s*var\(--vz-warn", css)
    assert re.search(r"color-mix\(in srgb,\s*var\(--vz-bad", css)


def test_long_visualization_copy_can_wrap_inside_inspectors():
    css = SHARED_CSS.read_text(encoding="utf-8")

    assert ".vz-note {" in css
    assert "overflow-wrap:anywhere" in css
    assert "min-width:0" in css
