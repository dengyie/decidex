"""
Real-game replay demo: run the DecideX Hearthstone advisor on a real Power.log.

This loads a genuine recorded Hearthstone game (Power.log), reconstructs the
board at each of the friendly player's decision points, and runs the advisor —
pinned to the real game engine's own legal-action envelope (the Options packets)
— then compares each recommendation against what the human actually played.

Usage:
    python examples/hearthstone_real_replay.py [path/to/Power.log]

Requires: pip install hslog

No game client, no botting: this reads a log file and prints recommendations.
"""

from __future__ import annotations

import asyncio
import os
import sys

from decidex.hearthstone.perception import load_game_snapshots
from decidex.hearthstone.replay import replay_game, game_options_to_candidates

DEFAULT_LOG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "replays", "13619.log",
)


async def main(log_path: str) -> None:
    if not os.path.isfile(log_path):
        print(f"Log not found: {log_path}")
        print("Download a real Power.log, e.g. from "
              "https://github.com/HearthSim/hsreplay-test-data/tree/master/data")
        sys.exit(1)

    print(f"Replaying real game: {log_path}\n")

    snapshots = load_game_snapshots(log_path)
    print(f"Decision points found: {len(snapshots)}\n")

    # 1. Perception: show what the advisor "sees" reconstructed from the log.
    print("=" * 72)
    print("PERCEPTION (Power.log -> BoardState)")
    print("=" * 72)
    for i, s in enumerate(snapshots):
        b = s.board
        mine = ", ".join(f"{m.name}({m.attack}/{m.health})" for m in b.my_minions) or "-"
        opp = ", ".join(
            f"{m.name}({m.attack}/{m.health}{'^' if m.taunt else ''})" for m in b.opp_minions
        ) or "-"
        print(
            f"  T{b.turn:>2} mana={b.mana}/{b.max_mana} deck={b.deck_count:>2} "
            f"| my=[{mine}] opp=[{opp}] hand={len(b.hand)}"
        )

    # 2. Replay: advisor recommendation vs human choice.
    print("\n" + "=" * 72)
    print("ADVISOR vs HUMAN (advisor pinned to real legal envelope)")
    print("=" * 72)
    reports = await replay_game(log_path, solver_fn=heuristic_solver)
    agree = sum(1 for r in reports if r.agreed)
    fallback = sum(1 for r in reports if r.advisor_fallback)
    print(f"\nAgreement: {agree}/{len(reports)} ({100 * agree / len(reports):.0f}%)  "
          f"| fallback: {fallback}\n")
    for r in reports:
        mark = "MATCH    " if r.agreed else "DIFFER   "
        fb = " [FALLBACK]" if r.advisor_fallback else ""
        print(f"  {mark} T{r.turn:>2} mana={r.mana}/{r.max_mana}")
        print(f"           advisor: {r.advisor_candidate}{fb}")
        print(f"           human  : {r.human_candidate}")
        print()


def heuristic_solver(payload, question):
    """
    A deterministic stand-in for the Jev model (drop-in swap: TypeSafeJevProvider).

    Prefers developing threats and favourable trades, then hero power, then pass.
    """
    opts = question.options or []
    if not opts:
        return "end_turn", 0.5, {}
    for prefix in ("attack:", "play:", "hero_attack:", "hero_power", "end_turn"):
        matches = [o for o in opts if o.startswith(prefix)]
        if matches:
            selected = matches[0]
            conf = 0.75
            dist = {
                o: (conf if o == selected else (1 - conf) / max(1, len(opts) - 1))
                for o in opts
            }
            return selected, conf, dist
    selected = opts[0]
    return selected, 0.6, {o: 1.0 / len(opts) for o in opts}


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_LOG
    asyncio.run(main(arg))