# DecideX Balatro HUD UI Design

## Goal

Make the DecideX advisor beside the Balatro WebAssembly game easy to scan and comfortable to use on desktop and narrow screens, while preserving the game canvas and all recommendation behavior.

## Audience and visual direction

The interface serves players who need to make a quick decision while keeping the game visible. Keep Balatro's dark table atmosphere, with restrained gold for key game information, green for the recommended play, and red for discard or caution. Use compact typography and clear spacing rather than adding decorative panels or changing the game's own visual style.

## Layout and hierarchy

1. Keep the game canvas as the primary surface and the DecideX HUD as a docked advisor on desktop.
2. Give the HUD a compact, legible header with product identity, live status, and an accessible collapse control.
3. Place Auto-Play state and control near the top, followed by a compact strip for ante, round, money, hands, and discards. Keep deck, discard pile, and joker capacity visible as secondary context.
4. Make the current phase and recommended action the strongest content. Preserve its score explanation, confidence or decision tags, selected cards, and direct action button.
5. Keep the alternative action visible but visually secondary. Distinguish the recommended choice using semantic color and a concise status label; do not rely on color alone.
6. Present candidate actions as a compact ranked comparison with legible scores and progress bars. Keep secondary sorting, reevaluation, and save utilities lower in the hierarchy.

## Responsive behavior

- Desktop: retain a side-by-side game and advisor layout with a stable HUD width and independent HUD scrolling.
- Narrow screens: let the game canvas use the available viewport. Present the HUD as a bottom sheet with a compact collapsed handle and a bounded, scrollable expanded state. Preserve the existing header collapse interaction and keep game controls reachable.
- Respect reduced-motion preferences and avoid viewport-dependent font scaling.

## Implementation boundaries

- Scope is `tools/balatro_wasm/web/style.css`, with minimal markup or display-only JavaScript changes only if required for responsive access or accessible labels.
- Preserve current element IDs, dynamic recommendation templates, action handlers, Auto-Play behavior, game bridge behavior, and shared probability visualization semantics.
- Do not modify Balatro gameplay assets, decision algorithms, provider logic, or the separate Hearthstone/replay interfaces in this milestone.
- Existing changes in `app.js`, `index.html`, bridge files, build files, tests, and test profiles are out of scope and must remain intact.

## Verification and acceptance

- Verify the existing WASM page at a desktop viewport and a narrow mobile viewport.
- Confirm game canvas remains visible and interactive; HUD collapse/expand works; the HUD content does not overlap critical game interaction areas on mobile.
- Check recommendation, alternative action, candidates, Auto-Play, context values, and footer utilities remain readable in representative game states.
- Run relevant existing tests and inspect the final diff for accidental changes to user-owned work.

## Alternatives considered

- Restyle every DecideX web tool in one pass: deferred because the Balatro game HUD is the clearest primary interactive surface and broader scope would increase regression risk.
- Restyle only the shared visualization widgets: deferred because the screenshots show the larger information hierarchy and narrow-screen layout as the main usability issues.

## Decision

Proceed with a focused, responsive Balatro WASM HUD adaptation. Revisit the other web tools in a later milestone if a shared product-wide visual system is needed.
