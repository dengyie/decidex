# DecideX Balatro Live Console UI Design

## Goal

Make the live Balatro console easier to scan and operate by connecting the real game feed, current table state, advisor output, decision history, and replay comparison in one responsive workspace.

## Audience and visual direction

The audience is operating or observing a real Balatro game through a remote browser. The game feed is the primary surface. Keep the felt green and warm gold as restrained game cues, with charcoal control surfaces, clear status text, and green/red reserved for operational and recommendation meaning. The interface should feel like a compact game operations console rather than a generic SaaS dashboard.

Do not add decorative gradients, card stacks, or motion unrelated to state changes. Keep repeated panel framing light and use spacing and headings to establish hierarchy.

## Information architecture

1. **Control bar:** product identity; selected live game or recorded session; provider; recommend and run-all commands; auto-follow toggle; authentication state and logout; concise run status.
2. **Live game surface:** real-time frame, live connection/latency status, and an explicit play-input enabled/disabled control. Keep the current click-to-coordinate mapping and frame aspect ratio intact.
3. **Table state and actions:** blind target, hands/discards/money/ante/score, projected score, played cards, hand, jokers, consumables, shop, and existing play/discard/sort/stage controls.
4. **Decision inspector:** snapshot timeline, selected decision, recommendation with confidence/provenance, legal action envelope, and advisor status.
5. **Run comparison:** advisor-versus-human replay table with its existing refresh action.

## Responsive behavior

- **Wide desktop:** use a compact sticky control bar. Place the live feed and table state/actions in the primary column; place the decision timeline and advisor inspector in a stable side column. Keep replay comparison as a full-width section below the primary workspace.
- **Tablet:** use a two-column layout while the game feed remains the largest region. Allow the inspector to move below the game region when the available width cannot support readable controls.
- **Mobile:** use one normal scrolling column in this order: control bar, live feed and play-mode status, table state/actions, timeline, advisor, replay comparison. Keep the feed fully visible and uncropped so click coordinates remain accurate. Keep hand/shop controls reachable and wrapped without page-level horizontal scrolling.
- Preserve the current 16:10 fallback sizing behavior and server-provided `BALATRO_GAME_W/H` dimensions.

## State and interaction behavior

- **Authentication:** show signed-out, signed-in, and SSO identity states without exposing credentials. Keep logout available in the control area.
- **Live feed:** distinguish connecting, live, and stale/unavailable frames with text as well as color. Keep latency and frame freshness visible without competing with the board.
- **Play input:** make enabled and disabled states explicit. Preserve the current toggle behavior, image click/double-click coordinate conversion, keyboard forwarding, and authentication redirects.
- **Advisor:** keep manual recommendation, provider selection, auto-follow, execution, and run-all as separate controls with distinct visual priority. Do not make recommendation or viewing controls trigger game input.
- **Recommendation/execution:** preserve existing action boundaries and API calls. Make the execute command visually distinct from analysis, while retaining current authorization and confirmation behavior.
- **History and comparison:** retain live-session and recorded snapshot selection, current snapshot emphasis, and full-run comparison semantics.
- **Errors/loading:** keep failures and pending states near the affected area and leave retry controls available. Do not hide an authentication or input error in a transient toast.

## Implementation boundaries

- Primary page: `tools/balatro_web/index.html` and its inline CSS/JavaScript.
- Login page `tools/balatro_web/login.html` may receive only matching visual treatment if needed for a coherent transition; preserve its form, redirect, and authentication behavior.
- Preserve functional DOM IDs and existing browser handlers where practical.
- Preserve routes and payloads, including `/api/health`, `/api/game`, `/api/session`, `/api/recommend`, `/api/replay`, `/api/frame.jpg`, `/api/input/click`, `/api/input/key`, `/api/login`, `/api/logout`, and existing shop/action routes.
- Do not change `tools/balatro_web/app.py`, provider/strategy logic, session authentication, input authorization, rate limits, game automation, or shared visualization assets.
- Visual QA must not issue real input to a game. Mock `/api/input/*` and live-feed endpoints in browser checks; use existing authentication/input tests to guard the server boundary.

## Verification and acceptance

- Run focused Balatro web input, metadata, visualization, and WASM build tests relevant to the touched interface.
- Use Chromium at desktop, tablet, and mobile sizes to inspect the signed-out console and a mocked signed-in live state.
- Verify the feed is fully visible, its displayed dimensions remain proportional, and the page has no horizontal overflow.
- Verify existing control IDs and requests remain present; mocked input checks confirm no action is sent by passive viewing, provider changes, or recommendation requests.
- Verify button focus is visible, controls remain reachable on mobile, and loading/error states identify the affected operation.
- Inspect the final diff to confirm server logic and unrelated Balatro user changes remain untouched.

## Alternatives considered

- **Polish the login page alone:** smaller effort, but leaves the main live workflow crowded and difficult to scan.
- **Redesign shared visualization widgets:** could improve consistency across pages, but does not address the live feed, game controls, and authentication hierarchy.
- **Responsive live console workspace:** selected because it is the remaining primary Balatro browser workflow and brings observation, advice, and existing controls together without changing their behavior.

## Decision

Proceed with a focused responsive redesign of the Balatro live web console. Preserve all current authentication and game-input semantics, and keep visual verification isolated from real game actuation. Review this specification before creating the implementation plan.
