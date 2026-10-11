"""
Real-bridge replay demo: run the DecideX Balatro advisor over dumped
decision-point snapshots and compare each recommendation with what the human
actually played.

Usage:
    python examples/balatro_real_replay.py [dir-or-file ...]

No game install needed: point it at a directory of bridge JSON snapshots
(default: tests/fixtures/balatro for a quick smoke, or the mod's output dir
%APPDATA%/Balatro/decidex_bridge on the machine running the game).

Read-only: this only prints recommendations, never touches the game.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from decidex.balatro import BalatroAdvisor, load_state
from decidex.providers.mock import MockReplayProvider

DEFAULT_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "balatro"


def heuristic_solver(payload, question):
    """Stand-in for the Jev model: trust the deterministic simulator's ranking."""
    opts = question.options or []
    if not opts:
        return "discard:0", 0.4, {}
    return opts[0], 0.82, {o: (0.82 if o == opts[0] else 0.18 / max(1, len(opts) - 1)) for o in opts}


def collect_snapshots(args):
    files = []
    for a in args:
        p = Path(a)
        if p.is_dir():
            files.extend(sorted(p.glob("*.json")))
        elif p.is_file():
            files.append(p)
    return files or sorted(DEFAULT_DIR.glob("*.json"))


def pair_decision_points(states):
    """Yield (pre_state, human_action_id) pairs: a pre snapshot (actual=None)
    paired with the next action snapshot's recorded move (same hand only)."""
    for i, (name, st) in enumerate(states):
        if st.actual is not None:
            continue
        human = None
        for j in range(i + 1, len(states)):
            nxt = states[j][1]
            if nxt.actual is not None:
                if [c.label for c in nxt.hand] == [c.label for c in st.hand]:
                    human = f"{nxt.actual['type']}:{'-'.join(str(k) for k in nxt.actual['cards'])}"
                break
        yield name, st, human


async def main(argv):
    files = collect_snapshots(argv)
    if not files:
        print("No bridge snapshots found.")
        return
    print(f"Replaying {len(files)} bridge snapshots from: {files[0].parent}\n")

    advisor = BalatroAdvisor(provider=MockReplayProvider(solver_fn=heuristic_solver), top_k=10)
    states = [(f.name, load_state(f)) for f in files]

    print("=" * 72)
    print("ADVISOR vs HUMAN (advisor pinned to the legal play/discard/use envelope)")
    print("=" * 72)
    results = []
    for name, st, human in pair_decision_points(states):
        rec = await advisor.recommend(st)
        results.append((name, rec, human))
        mark = "MATCH  " if rec.candidate_id == human else "DIFFER "
        print(f"  {mark} {name}  {st.blind.name}(目标 {st.blind.chips}) "
              f"手{st.hands_left} 弃{st.discards_left}")
        print(f"           advisor: {rec.candidate_id}  {rec.description}")
        print(f"           human  : {human or '(未记录)'}")
        print()
    advisor.close()

    compared = [(n, r, h) for n, r, h in results if h]
    if compared:
        agree = sum(1 for _, r, h in compared if r.candidate_id == h)
        print(f"Agreement: {agree}/{len(compared)} ({100 * agree / len(compared):.0f}%)")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
