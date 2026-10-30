# DecideX Hearthstone Replay UI Implementation Plan

> **For agentic workers:** Execute inline on the current feature branch. Preserve all unrelated and pre-existing worktree changes.

**Goal:** Build a clear responsive Hearthstone replay analysis workspace that connects a selected turn, its reconstructed board, and the advisor review.

**Architecture:** Keep the current single-file page and its client-side API flow. Rework page structure and inline CSS within `tools/hearthstone_web/index.html`, retaining existing IDs, JavaScript functions, API routes, and shared visualization assets. Use responsive CSS grid areas for wide, tablet, and mobile layouts.

**Tech Stack:** Existing vanilla HTML/CSS/JavaScript, Python `http.server` web handler, pytest, Playwright with Chromium.

## Global Constraints

- Preserve API routes and payloads: `/api/health`, `/api/game`, `/api/recommend`, and `/api/replay`.
- Preserve the read-only boundary: the viewer may enumerate legal actions and display advice but must never drive the Hearthstone client.
- Reuse `tools/web_shared/viz.css` and `tools/web_shared/viz.js` as-is.
- Do not alter the Balatro WASM HUD or other existing user changes in this milestone.

---

## File Structure

- `tools/hearthstone_web/index.html`: page structure, responsive visual system, and existing browser interaction code. Retain current functional IDs and API calls.
- `tests/test_hearthstone_web_viz.py`: existing shared visualization/API contract tests; do not change unless UI integration reveals a contract regression.
- `tools/hearthstone_web/app.py`: serve replay API and shared assets. No changes planned.
- `data/replays/*.log`: existing real Power.log fixtures used by the local server and browser smoke test; do not modify.

### Task 1: Restructure the Replay Workspace

**Files:**
- Modify: `tools/hearthstone_web/index.html`

- [x] Preserve header control IDs `gameSel`, `provSel`, `runAll`, `provBadge`, and `hsBadge`; group them in a compact toolbar with a responsive wrap.
- [x] Preserve `stats`, `turns`, `board`, `recBody`, and `note` IDs. Add stable grid-area wrappers around timeline, board, and advisor regions so CSS can move the same content without JavaScript changes.
- [x] Keep the current decision summary and turn rows; refine selected, agreement, disagreement, and unscored state labels so meaning is not color-only.
- [x] Keep board rendering and advisor rendering functions intact, but ensure their containers have `min-width: 0` and wrap long labels/card names.
- [x] Add explicit responsive empty/error presentation for loading, empty inventory, and inline errors; preserve the API surface and read-only behavior.
- [x] Run `git diff --check` and inspect the HTML diff; API routes and action code remain unchanged.

### Task 2: Implement Responsive Layout and Verify Flows

**Files:**
- Modify: `tools/hearthstone_web/index.html`

- [x] At wide desktop (`min-width: 1240px`), render timeline, board, and advisor as three columns; give the board flexible width and keep timeline/advisor readable.
- [x] At tablet (`760px` through `1239px`), render timeline in the first column and board/advisor as a second-column stack.
- [x] At narrow mobile (`max-width: 759px`), render controls and summary first, then timeline, board, and advisor; bound timeline height and keep normal page scrolling.
- [x] Ensure hero rows, minions, hand cards, legal options, and probability bars wrap or scroll internally without document horizontal overflow.
- [x] Run `.venv\Scripts\python.exe -m pytest tests/test_hearthstone_web_viz.py -q`; 3 tests passed.
- [x] Run the web tool on port 8097 and use Chromium at desktop (1440px), tablet (900px), and mobile (390px) to select turns by keyboard, switch provider, run replay, and verify empty/error states.
- [x] Confirm all tested viewports have no horizontal overflow and selected-turn context stays synchronized; confirm the UI remains explicitly read-only.
- [x] Review the final diff and confirm no Balatro files or unrelated user modifications were included in this change.

## Acceptance

- Desktop shows timeline, reconstructed board, and advisor review together.
- Tablet shows a usable timeline beside the board and advisor stack.
- Mobile follows the documented single-column order and keeps normal scrolling usable.
- Selecting a turn updates the board and recommendation for that same turn; provider switching and run-all still work.
- Empty inventory and failed requests are understandable and retryable.
- No horizontal page overflow at verified viewports.
- The page remains a read-only advisor and the shared visualization contract is unchanged.
