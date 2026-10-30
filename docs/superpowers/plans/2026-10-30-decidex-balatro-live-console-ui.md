# DecideX Balatro Live Console UI Implementation Plan

> **For agentic workers:** Execute inline with the `executing-plans` skill. Preserve all unrelated and pre-existing worktree changes.

**Goal:** Make the Balatro live browser console readable and usable across desktop, tablet, and mobile while preserving every existing remote-control and authentication behavior.

**Architecture:** Keep the existing single-page HTML/CSS/JavaScript console. Add small semantic layout wrappers around the control bar, live game/table column, and decision inspector, then use CSS grid areas and responsive breakpoints to rearrange those same regions. Preserve all existing IDs, API calls, event handlers, and image coordinate calculations.

**Tech Stack:** Existing vanilla HTML/CSS/JavaScript and Python `http.server`; pytest for API/auth regression tests; Chromium through the available Playwright Node package for responsive and interaction checks.

## Global Constraints

- Preserve all existing browser control IDs and request payloads.
- Preserve authentication, SSO, input authorization, input toggling, and game coordinate conversion.
- Do not change `tools/balatro_web/app.py`, advisor/provider logic, automation, or `tools/web_shared` assets.
- Browser UI checks must mock live feed, state, session, and all `/api/input/*` endpoints; never send input to a real game.
- Preserve unrelated existing changes in Balatro WASM, bridge, build, and test files.

---

## File Structure

- `tools/balatro_web/index.html`: semantic console regions and the page's inline responsive visual system; keep existing client behavior in place.
- `tools/balatro_web/login.html`: no change planned unless browser inspection finds a clear transition mismatch that blocks the console workflow.
- `tests/test_balatro_web_input.py`, `tests/test_balatro_web_metadata.py`, `tests/test_balatro_web_viz.py`, `tests/test_balatro_wasm_build.py`: existing auth/input/metadata/visualization/static-serving regression coverage; do not change unless a UI contract regression is found.
- Temporary Playwright checks: run inline through `E:/tmp/decidex-ui-playwright/node_modules/playwright`; do not add a runtime dependency to the project.

### Task 1: Capture and lock the console behavior contract

**Files:**
- Read: `tools/balatro_web/index.html`
- Read: `tools/balatro_web/app.py`
- Test: existing Balatro web input/auth and metadata suites

- [x] Run `.venv\Scripts\python.exe -m pytest tests/test_balatro_web_input.py tests/test_balatro_web_metadata.py tests/test_balatro_web_viz.py -q` before page changes and record the baseline (74 passed).
- [x] Start `tools/balatro_web/app.py` on a free loopback port with temporary test credentials and inspect `/stream` in Chromium with mocked `/api/session`, `/api/health`, `/api/game`, `/api/frame.jpg`, `/api/recommend`, and `/api/replay` responses.
- [x] Record the existing functional IDs used by the page: `gameSel`, `provSel`, `recBtn`, `execBtn`, `runAll`, `autoAdvisorCheck`, `authState`, `logoutBtn`, `liveFeed`, `playToggle`, `liveMetrics`, `hand`, `handActions`, `snapList`, `advisorPanel`, `recBody`, `vizBody`, `legal`, and `cmpTable`.
- [x] Confirm the browser harness intercepts `/api/input/click`, `/api/input/key`, and `/api/execute` before interacting with any page controls.

### Task 2: Restructure the live console regions

**Files:**
- Modify: `tools/balatro_web/index.html`

- [x] Group existing header controls under stable semantic wrappers for identity, game/provider/actions, and auth/status. Keep every current ID and handler unchanged.
- [x] Move the existing live feed and full table/action panel into the primary game column. Group the decision timeline, advisor/probability/envelope, and run comparison into the inspector column.
- [x] Use `<main>` and named `<section>`/`<aside>` landmarks with visible heading references; avoid duplicate IDs and do not change text-to-action bindings.
- [x] Preserve the feed's intrinsic ratio and `object-fit:contain`; do not scale by transforms, crop, or add a click overlay that would affect `gameCoords()`.
- [x] Add visible initial frame freshness and synchronized play-input mode text without adding input behavior.

### Task 3: Apply and verify responsive styling

**Files:**
- Modify: `tools/balatro_web/index.html`

- [x] Define a restrained set of local CSS tokens and focus styles consistent with the green felt/gold game cues and charcoal control surfaces; keep semantic green/red meanings.
- [x] At wide desktop, use a primary game column for live feed plus table/actions and a narrower inspector for timeline/advisor; put run comparison below both columns.
- [x] At tablet, retain two columns only while the inspector and action controls remain readable; collapse the inspector beneath the game column at the narrower tablet breakpoint.
- [x] At mobile, order controls, live feed/input status, table/actions, timeline, advisor, and run comparison in one normal scrolling column. Wrap hand and shop controls without page-level horizontal overflow.
- [x] Run existing web tests: `.venv\Scripts\python.exe -m pytest tests/test_balatro_web_input.py tests/test_balatro_web_metadata.py tests/test_balatro_web_viz.py tests/test_balatro_wasm_build.py -q`; 108 passed.
- [x] In Chromium, check `/stream` at 1440x900, 900x900, 390x844, and 360x740 in signed-out and mocked signed-in states. Verify no horizontal overflow, the feed remains wholly visible at its intrinsic ratio, the inspector follows the specified order, and keyboard focus is visible.
- [x] With mocked endpoints, verify selecting a snapshot, changing provider, requesting a recommendation, and viewing the feed do not call `/api/input/*` or `/api/execute`; verify the play-mode toggle changes only the existing mode state.
- [x] Review `git diff --check` and the final path list; only `tools/balatro_web/index.html` changed for implementation.

## Acceptance

- The live feed and table actions read as the primary workflow; recommendation, history, and replay comparison remain easy to locate.
- Desktop, tablet, and phone layouts match the design specification and have no page-level horizontal overflow.
- Authentication and enabled/disabled game-input states remain visible and understandable.
- Existing auth/input test suites pass and mocked browser checks prove passive viewing/advice actions do not emit game input.
- API/backend code, WASM HUD files, and pre-existing user changes remain untouched.
