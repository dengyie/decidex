# DecideX Hearthstone Replay Dashboard Design

## Goal

Make the Hearthstone replay viewer a clear, responsive analysis workspace where users can move from a decision point to its reconstructed board and compare the advisor's recommendation with the human action.

## Audience and visual direction

The audience reviews completed Hearthstone turns and wants to understand why an action was recommended. Use a restrained tactical-workspace visual style: charcoal surfaces, clear text hierarchy, compact game-state details, and semantic green/red/gold accents for agreement, disagreement, and attention. Keep the existing card battleground readable without turning every section into a floating card.

## Information architecture

The page has three connected regions:

1. **Decision timeline:** decision count, agreement rate, fallback count, and selectable turns with turn number, mana, option count, and result state.
2. **Reconstructed board:** opponent and player heroes, minions, mana, deck count, and hand for the selected decision.
3. **Advisor review:** recommendation, provider and fallback provenance, confidence, agreement with the human action, legal action envelope, and the existing shared visualization report.

Keep the game selector, provider selector, health badges, and run-all action in a compact persistent header. Selecting a turn continues to update both the board and recommendation. Running the full replay continues to populate summary statistics and per-turn results.

## Responsive behavior

- Wide desktop: use a three-column workspace with a narrow timeline, flexible board, and recommendation pane sized for readable evidence.
- Tablet: use a two-column layout with the timeline alongside the board/recommendation stack.
- Narrow mobile: use a single column ordered as summary and controls, decision timeline, board, then advisor review. Bound the timeline height so the selected board remains easy to reach. Allow the hand and minion rows to wrap without horizontal page overflow.
- Preserve normal page scrolling and visible focus. Do not add a separate navigation mode or change the page's data flow.

## State and interaction behavior

- Loading: show an inline progress state while replay inventory or a selected log is being loaded.
- Empty inventory: keep the dashboard structure and show an explicit empty state for missing replay logs.
- Selected turn: visibly identify the active timeline entry while the board and advisor panes show the same turn.
- Recommendation pending: show the existing calculation state in the advisor pane without clearing the selected board context.
- Failure: display readable inline errors for failed replay loading, recommendation, or run-all operations; keep selectors and navigation usable for retry.
- Full replay completion: show decision count, agreement rate, fallback count, and turn result markers.

## Implementation boundaries

- Primary implementation file: `tools/hearthstone_web/index.html`, which currently owns the page markup, inline CSS, and client interactions.
- Reuse `tools/web_shared/viz.css` and `tools/web_shared/viz.js` as-is; the server already exposes those shared components.
- Leave `tools/hearthstone_web/app.py` and decision/provider logic unchanged unless a true P0/P1 UI blocker is found.
- Preserve API routes and payloads: `/api/health`, `/api/game`, `/api/recommend`, and `/api/replay`.
- Preserve the read-only boundary: the viewer may enumerate legal actions and display advice but must never drive the Hearthstone client.
- Do not alter the Balatro WASM HUD or other existing user changes in this milestone.

## Verification and acceptance

- At desktop, verify the decision timeline, reconstructed board, advisor review, controls, and summary fit without horizontal overflow.
- At tablet and narrow mobile widths, verify the regions follow the stated order and remain readable; selecting a turn updates the board and advisor for that same turn.
- Verify empty replay inventory and recommendation/run-all failure states remain understandable and recoverable.
- Verify provider switching and full-replay analysis remain functional without changing API behavior.
- Run focused Hearthstone web/viz tests and browser checks with the available Power.log samples.
- Confirm the UI continues to identify itself as a read-only advisor and introduces no game-driving controls.

## Alternatives considered

- Adapt the Balatro live web console next: deferred because it has more game-driving controls and a substantially larger dynamic page surface.
- Apply one shared visual redesign to every DecideX tool: deferred because it would combine separate workflows and increase regression risk.
- Focused Hearthstone replay workspace: selected because the tool has real replay samples, a clear three-region workflow, and a separable front-end boundary.

## Decision

Proceed with a responsive Hearthstone replay analysis workspace. Keep Balatro live and other DecideX tools as later, separate UI milestones.
