# DecideX Shared Visualization UI Implementation Plan

> **For agentic workers:** Execute inline. Preserve all unrelated and pre-existing worktree changes.

**Goal:** Make DecideX probability, confidence, token, and lint widgets adapt to the Balatro and Hearthstone themes and narrow inspector widths.

**Architecture:** Keep `tools/web_shared/viz.js` and its JSON contract unchanged. Treat `tools/web_shared/viz.css` as the canonical stylesheet and synchronize its standalone WASM consumer copy at `tools/balatro_wasm/web/viz.css`; a focused test will prevent those files from drifting.

**Tech Stack:** Existing vanilla CSS/JavaScript, pytest, and Chromium through `E:/tmp/decidex-ui-playwright/node_modules/playwright`.

## Global Constraints

- Preserve the `DecidexViz` DOM classes and the server visualization report contract.
- Use consumer theme variables (`--good`, `--warn`, `--bad`, `--accent`, `--dim`, `--text`, `--line`) with existing safe fallbacks.
- Keep Balatro, Hearthstone, and WASM page markup and application behavior unchanged.
- Do not modify `viz.js`, provider/strategy code, APIs, or existing user-edited WASM application files.

---

## File Structure

- `tools/web_shared/viz.css`: canonical shared component styles.
- `tools/balatro_wasm/web/viz.css`: byte-identical standalone WASM copy served as a static asset.
- `tests/test_shared_viz_assets.py`: parity and responsive CSS contract tests.
- Existing Balatro/Hearthstone visualization test suites: validate unchanged report/route contracts.

### Task 1: Lock the shared asset boundary

**Files:**
- Create: `tests/test_shared_viz_assets.py`

- [x] Add tests asserting the shared and WASM `viz.css` files are byte-identical and the shared sheet uses theme variables for semantic fills and responsive min-width-safe probability columns.
- [x] Run `.venv\Scripts\python.exe -m pytest tests/test_shared_viz_assets.py -q` and confirm failure because the responsive/theme contract is missing; the mirror parity test passed.

### Task 2: Adapt the shared visualization styling

**Files:**
- Modify: `tools/web_shared/viz.css`
- Copy canonical content to: `tools/balatro_wasm/web/viz.css`
- Test: `tests/test_shared_viz_assets.py`

- [x] Use local theme variables and `color-mix()` for confidence states, selected markers, gradients, and fills while preserving safe variable fallbacks.
- [x] Make confidence badges compact, readable, and wrapping; keep `vz-badge`, `vz-good`, `vz-warn`, and `vz-bad` selectors compatible with existing JS output.
- [x] Make probability rows use `minmax(0, …)` grid tracks, keep percentages readable, and allow labels to wrap or truncate without forcing component overflow.
- [x] Allow long lint messages, codes, notes, and token summaries to wrap within narrow inspector columns; preserve semantic issue colors.
- [x] Keep motion limited to probability fill changes and honor `prefers-reduced-motion`.
- [x] Copy the canonical CSS byte-for-byte to `tools/balatro_wasm/web/viz.css`.
- [x] Run the parity test and existing Balatro/Hearthstone viz suites; browser checks pass at 280px embedded width.

### Task 3: Verify the component in each consumer

**Files:**
- No further source changes unless verification finds a defect.

- [x] Use Chromium with mocked visualization reports in Balatro and Hearthstone viewers at 1440px, 900px, 390px, and a narrow 280px component container.
- [x] Confirm all bars, badges, token gauges, and long lint text remain inside their host; check document and component scroll widths.
- [x] Open the WASM static asset with mocked `DecidexViz` output without launching or actuating the game.
- [x] Run `.venv\Scripts\python.exe -m pytest tests/test_shared_viz_assets.py tests/test_balatro_web_viz.py tests/test_hearthstone_web_viz.py tests/test_balatro_wasm_build.py -q`; 46 passed, and `git diff --check` passed.
- [x] Inspect final changed paths and confirm only the canonical CSS, its WASM copy, the focused test, and project memory/plan files are involved.

## Acceptance

- All three CSS consumers use the same theme-adaptive component stylesheet.
- Narrow containers do not create probability-row or lint-panel overflow.
- Existing provider visualization payloads and shared asset routes remain unchanged.
- Focused tests pass and browser inspection shows consistent, legible widgets in both game dashboards.
