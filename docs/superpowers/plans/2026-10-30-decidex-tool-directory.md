# DecideX Tool Directory Implementation Plan

> **For agentic workers:** Execute inline. Preserve unrelated and pre-existing worktree changes.

**Goal:** Give users one stable DecideX entry point for Balatro WASM, the remote Balatro console, and a separately hosted Hearthstone replay viewer.

**Architecture:** Serve a new static tool directory at `/` and `/index.html`; serve the existing WASM page at `/play`; keep `/stream`, static WASM asset paths, and all APIs intact. The Hearthstone card accepts a user-supplied HTTP(S) URL stored in browser local storage, because that viewer can run on a different host and port.

**Tech Stack:** Existing Python `http.server`, vanilla HTML/CSS/JavaScript, pytest, Chromium through the existing Playwright Node package.

## Global Constraints

- `/` and `/index.html` must remain the tool directory regardless of whether the WASM index asset exists.
- `/play` serves the existing `tools/balatro_wasm/web/index.html` or returns an explicit unavailable response when that asset is absent.
- `/stream`, login redirects, `/viz.css`, `/viz.js`, WASM files, and all API routes keep their current behavior.
- The portal performs no API calls, login, or game input. It only stores the user-entered Hearthstone HTTP(S) URL in local storage.
- Preserve existing user changes in WASM, bridge, Balatro page, Hearthstone page, and shared visualization files.

---

## File Structure

- `tools/balatro_web/portal.html`: new product entry directory and local Hearthstone URL control.
- `tools/balatro_web/app.py`: explicit `/`/`/index.html` portal and `/play` game routes for both GET and HEAD.
- `tests/test_balatro_web_input.py`: route contract tests using the existing HTTP handler fixture.
- `README.md`: brief link to the new local tool directory and direct routes.
- `.codex-memory/*` and this plan: project continuity and execution state.

### Task 1: Lock route behavior with tests

**Files:**
- Modify: `tests/test_balatro_web_input.py`

- [x] Add an HTTP test asserting `/` and `/?source=test` serve the tool directory markers and do not serve the WASM canvas.
- [x] Add an HTTP test asserting `/play` serves the existing WASM index and `/stream` still serves the live console.
- [x] Run `.venv\Scripts\python.exe -m pytest tests/test_balatro_web_input.py -q`; confirmed the new `/play` contract failed before implementation.

### Task 2: Add the stable portal and explicit game route

**Files:**
- Create: `tools/balatro_web/portal.html`
- Modify: `tools/balatro_web/app.py`
- Modify: `README.md`

- [x] Build a responsive directory with three clear entries: local Balatro WASM at `/play`, authenticated remote console at `/stream`, and external Hearthstone replay viewer.
- [x] Let users edit and save a Hearthstone URL in local storage; accept only `http:` and `https:` URLs, show a clear invalid/empty state, and open configured destinations in a new tab with `noopener`.
- [x] Change GET `/` and `/index.html` to serve `portal.html` regardless of WASM asset presence.
- [x] Add GET and HEAD `/play` branches to serve the existing WASM index with no cache while leaving the current static asset resolver intact.
- [x] Update README entry points with `/`, `/play`, and `/stream` meanings.
- [x] Run the route tests, including root query, missing WASM build, and HEAD response behavior.

### Task 3: Verify navigation and responsive behavior

**Files:**
- No source changes unless verification exposes a defect.

- [x] Run `.venv\Scripts\python.exe -m pytest tests/test_balatro_web_input.py tests/test_balatro_web_metadata.py tests/test_balatro_web_viz.py tests/test_balatro_wasm_build.py -q`; 111 passed.
- [x] Use Chromium at 1440x900, 900x900, and 390x844 (also 360x740); confirm the tool directory has no horizontal overflow, each local route is correct, and saving/reloading an external Hearthstone URL persists locally.
- [x] Verify invalid schemes such as `javascript:` are rejected and that clicking an unconfigured Hearthstone action does not navigate.
- [x] Verify portal navigation emits no `/api/*` requests.
- [ ] Run `git diff --check` and confirm source changes are limited to the portal, Balatro routing/tests, README, and project memory/plan.

## Acceptance

- Opening the Balatro service root always shows a stable DecideX tool directory.
- The existing game is explicitly reachable at `/play`; the existing remote controller remains at `/stream`.
- Hearthstone can be configured at a different origin without assuming a host or port.
- No authentication, API, replay, or game-control contract changes.
