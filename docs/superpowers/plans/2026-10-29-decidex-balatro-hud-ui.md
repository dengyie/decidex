# DecideX Balatro HUD UI Implementation Plan

> **For agentic workers:** Implement this plan inline, one task at a time, preserving the existing uncommitted bridge and recommendation changes.

**Goal:** Make the Balatro WebAssembly advisor HUD easy to scan and usable on desktop and narrow mobile screens.

**Architecture:** Keep the existing game canvas, HTML IDs, dynamic recommendation markup, action handlers, and shared visualization renderer. Improve the existing HUD stylesheet with a compact desktop dock, clearer recommendation hierarchy, and a mobile bottom-sheet layout; change markup only when required for labeling or responsive access.

**Tech Stack:** Existing vanilla HTML/CSS/JavaScript, Python `http.server` based project preview, browser visual inspection.

## Global Constraints

- Preserve current element IDs, dynamic recommendation templates, action handlers, Auto-Play behavior, game bridge behavior, and shared probability visualization semantics.
- Do not modify Balatro gameplay assets, decision algorithms, provider logic, or the separate Hearthstone/replay interfaces in this milestone.
- Existing changes in `app.js`, `index.html`, bridge files, build files, tests, and test profiles are out of scope and must remain intact.
- Respect reduced-motion preferences and avoid viewport-dependent font scaling.

---

## File Structure

- `tools/balatro_wasm/web/style.css`: owns game/HUD layout, hierarchy, desktop styling, responsive bottom sheet, focus states, and reduced-motion handling.
- `tools/balatro_wasm/web/index.html`: change only if the current semantic structure cannot provide a labeled, keyboard-operable mobile sheet control.
- `tools/balatro_wasm/web/app.js`: avoid changes; existing `.hud-header` click handling must keep working.
- `tools/balatro_wasm/web/viz.css`: avoid changing the shared component contract; tune its variables locally from the page stylesheet if needed.

### Task 1: Refine the Desktop HUD

**Files:**
- Modify: `tools/balatro_wasm/web/style.css`

- [x] Define restrained surface, line, semantic, spacing, and focus tokens in `:root`; preserve the dark table field and existing Balatro color meanings.
- [x] Refine desktop HUD header, autopilot control, status strip, context strip, recommendation, alternative recommendation, candidate rows, and footer spacing without changing selectors consumed by JavaScript.
- [x] Add clear focus-visible treatment, readable long-text wrapping, and a reduced-motion rule.
- [x] Run `git diff --check` and inspect only the stylesheet diff before beginning responsive changes.

### Task 2: Add Responsive HUD Access

**Files:**
- Modify: `tools/balatro_wasm/web/style.css`
- Modify only if necessary: `tools/balatro_wasm/web/index.html`

- [x] Add a narrow-screen breakpoint that moves the HUD to a fixed bottom sheet with a stable collapsed handle, bounded expanded height, and independent scrolling.
- [x] Ensure the game viewport remains visible behind/above the sheet and the canvas can shrink without cropping; preserve the existing `.minimized` state transition and toggle button.
- [x] At mobile browser viewports, test both collapsed and expanded HUD states, scrolling, game canvas visibility, and text overflow. Retain all existing IDs and handlers.
- [x] At a desktop viewport, verify the dock width, canvas visibility, recommendation card, candidate ranking, and footer controls.
- [x] Run focused tests: `.venv\Scripts\python.exe -m pytest tests/test_balatro_wasm_build.py tests/test_balatro_web_viz.py -q`; 36 tests passed.
- [x] Review the final stylesheet diff and confirm pre-existing `index.html`/`app.js` changes remain intact and untouched.

## Acceptance

- The first scan identifies Auto-Play status, current game context, and the preferred action.
- Alternative action remains available with a distinct semantic treatment and text label.
- Candidate scores remain comparable and utilities remain reachable.
- Desktop retains a side-by-side game and HUD composition.
- Narrow mobile retains a visible game area and an expandable, scrollable HUD without horizontal overflow.
- Existing focused tests pass and browser visual inspection finds no overlap or unusable controls.
