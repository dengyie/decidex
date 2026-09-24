"""
DecideX Autonomous 2048 Live Player
Connects to Chrome port 9222 (https://game.ark717.com/), extracts live board state,
and executes real-time gameplay decisions via DecideX DecisionEngine & KeyPool.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from typing import Any, Dict, List, Optional

from decidex.engine import DecisionEngine
from decidex.guards import CalibratedDecisionGuard
from decidex.memory import MemoryHarness
from decidex.pool import KeyPool, RotationStrategy
from decidex.providers.typesafe import TypeSafeJevProvider
from decidex.tools.browser_2048 import Chrome2048Controller
from decidex.tools.solver2048 import (
    evaluate_board,
    generate_move_criteria,
    get_legal_moves,
    search_best_move,
    simulate_move,
)
from decidex.types import ObservationPayload, PrimitiveType, QuestionSpec


# ANSI Color formatting for terminal grid rendering
TILE_COLORS = {
    0: "\033[90m    .\033[0m",
    2: "\033[37m    2\033[0m",
    4: "\033[36m    4\033[0m",
    8: "\033[34m    8\033[0m",
    16: "\033[32m   16\033[0m",
    32: "\033[33m   32\033[0m",
    64: "\033[31m   64\033[0m",
    128: "\033[35m  128\033[0m",
    256: "\033[1;36m  256\033[0m",
    512: "\033[1;33m  512\033[0m",
    1024: "\033[1;35m 1024\033[0m",
    2048: "\033[1;31;47m 2048\033[0m",
    4096: "\033[1;37;41m 4096\033[0m",
    8192: "\033[1;37;45m 8192\033[0m"
}


def print_board(grid: List[List[int]], score: int, moves: int, max_tile: int, latency_ms: float, move_info: str, force_full: bool = False) -> None:
    """Renders ANSI formatted 4x4 2048 board and telemetry dashboard."""
    if sys.stdout.isatty():
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()
        print("\033[1;34m" + "=" * 52 + "\033[0m")
        print(f"\033[1;32m🎮 DecideX 2048 Live Game Engine\033[0m  |  \033[1mScore:\033[0m \033[1;33m{score:<6}\033[0m")
        print(f"\033[1mMoves:\033[0m {moves:<5} | \033[1mMax Tile:\033[0m \033[1;31m{max_tile:<5}\033[0m | \033[1mStep Latency:\033[0m {latency_ms:.1f}ms")
        print(f"\033[1mLast Decision:\033[0m {move_info}")
        print("\033[1;34m" + "-" * 52 + "\033[0m")

        for row in grid:
            line_str = " | ".join(TILE_COLORS.get(v, f"{v:>5}") for v in row)
            print(f"  | {line_str} |")
        print("\033[1;34m" + "=" * 52 + "\033[0m\n")
    else:
        # Non-TTY clean output: print single line, and full board periodically or on game over
        print(f"[Move {moves:>3}] Score: {score:<6} | MaxTile: {max_tile:<4} | {move_info} | Step: {latency_ms:.1f}ms")
        if force_full or moves % 25 == 0:
            print("  +" + "------+" * 4)
            for row in grid:
                line_str = " | ".join(TILE_COLORS.get(v, f"{v:>5}") for v in row)
                print(f"  | {line_str} |")
            print("  +" + "------+" * 4)
        sys.stdout.flush()


def resolve_2048_fallback(q: QuestionSpec, legal_actions: List[str], reason: str, domain_state: Any = None) -> str:
    """Intelligent fallback for 2048: uses Expectimax lookahead if model/guard fails."""
    if domain_state and isinstance(domain_state, dict) and "grid" in domain_state:
        best_m, _, _ = search_best_move(domain_state["grid"], depth=2)
        if best_m in legal_actions:
            return best_m
    return legal_actions[0] if legal_actions else "DOWN"


async def main() -> None:
    parser = argparse.ArgumentParser(description="DecideX Autonomous 2048 Live Player")
    parser.add_argument("--port", type=int, default=9222, help="Chrome CDP debugging port (default: 9222)")
    parser.add_argument("--delay", type=float, default=0.02, help="Delay in seconds between moves (default: 0.02s)")
    parser.add_argument("--mode", type=str, default="jev", choices=["hybrid", "fast", "jev"],
                        help="Decision mode: 'jev' (pure Jev model on every single step), 'hybrid' (periodic Jev), 'fast' (pure expectimax)")
    parser.add_argument("--max-moves", type=int, default=0, help="Maximum moves before stopping (0 for unlimited until Game Over)")
    parser.add_argument("--jev-interval", type=int, default=20, help="Steps between Jev model consultations in hybrid mode (default: 20)")
    args = parser.parse_args()

    controller: Optional[Chrome2048Controller] = None
    jev_provider: Optional[TypeSafeJevProvider] = None
    engine: Optional[DecisionEngine] = None
    pool: Optional[KeyPool] = None
    moves_count = 0
    total_latency_ms = 0.0
    jev_calls_count = 0
    jev_fallback_count = 0
    jev_latencies: List[float] = []

    try:
        print("\n[DecideX 2048] Connecting to Chrome CDP on port", args.port, "...")
        controller = Chrome2048Controller(cdp_port=args.port)
        await controller.connect()
        print("  -> Connected to tab:", controller.ws_url)

        # 1. Install non-invasive hooks
        print("[DecideX 2048] Installing non-invasive runtime interceptor...")
        hook_res = await controller.setup_hooks()
        print("  -> Interceptor installed:", hook_res)

        # 2. Open game and ensure practice mode is active
        print("[DecideX 2048] Navigating to 2048 Practice Mode...")
        start_res = await controller.start_practice_mode()
        print("  -> Practice mode started:", start_res)
        await asyncio.sleep(0.3)

        # 3. Setup Decision Layer Components
        if args.mode in ("hybrid", "jev"):
            try:
                pool = KeyPool.from_default_locations(strategy=RotationStrategy.ROUND_ROBIN)
                jev_provider = TypeSafeJevProvider(key_pool=pool, timeout_s=3.0)
                print(f"  -> Jev KeyPool active with {len(pool)} available keys")
                print("  -> Pre-warming Jev HTTP connection pool...")
                warm = await jev_provider.warmup()
                print(f"  -> Jev HTTP connection pre-warmed: {'Success (HTTP 200)' if warm else 'Skipped/Offline'}")

                guard = CalibratedDecisionGuard(
                    temperature=1.0,
                    min_confidence=0.1,
                    min_margin=0.01,
                    max_consecutive_action=0,  # Game-safe: 0 disables blind stuck limit
                    anti_oscillation=True
                )
                memory = MemoryHarness(capacity=16)
                journal_path = os.path.join(os.path.dirname(__file__), "game2048_live_journal.jsonl")
                engine = DecisionEngine(
                    provider=jev_provider,
                    guard=guard,
                    memory=memory,
                    journal_path=journal_path,
                    fallback_resolver=resolve_2048_fallback
                )
            except Exception as e:
                print(f"  -> Warning: Could not initialize DecisionEngine ({e}), falling back to local lookahead")
                args.mode = "fast"

        print("\n🚀 Starting autonomous decision loop (Press Ctrl+C to stop)...")
        await asyncio.sleep(0.5)

        while args.max_moves <= 0 or moves_count < args.max_moves:
            t0 = time.perf_counter()

            # Read current board
            state = await controller.get_state()
            grid = state.get("grid", [[0]*4 for _ in range(4)])
            score = state.get("score", 0)
            dead = state.get("dead", False)

            max_tile = max(max(row) for row in grid) if grid else 0

            # If board hasn't loaded any tiles yet, wait briefly
            if max_tile == 0 and not dead:
                await asyncio.sleep(0.05)
                continue

            # Check if game is over
            legal_moves = get_legal_moves(grid)
            empty_count = sum(row.count(0) for row in grid)
            if dead or (empty_count == 0 and not legal_moves):
                print_board(grid, score, moves_count, max_tile, 0.0, "🏁 GAME OVER", force_full=True)
                print(f"\n[DecideX 2048] 🏁 Game Over condition reached! Final Score: {score} | Max Tile: {max_tile} | Total Moves: {moves_count}")
                break

            # Decision Logic
            chosen_move: Optional[str] = None
            decision_info = ""

            use_jev = (args.mode == "jev") or (args.mode == "hybrid" and moves_count % args.jev_interval == 0 and engine)

            if use_jev and engine and pool:
                # Orchestrate through DecideX DecisionEngine
                t_jev_start = time.perf_counter()
                try:
                    _, best_s, lookahead_scores = search_best_move(grid, depth=2)
                    criteria = generate_move_criteria(grid, legal_moves, lookahead_scores=lookahead_scores)
                    q = QuestionSpec(
                        id="move_direction",
                        primitive=PrimitiveType.CHOICE,
                        description="Select the optimal move in 2048 to synthesize high-tier tiles, anchor maximum numbers in bottom-left corner, prevent trapping small tiles above anchor, preserve S-curve snake order (Row 3 left-to-right, Row 2 right-to-left), and maximize free breathing cells.",
                        options=legal_moves,
                        criteria=criteria
                    )
                    decisions = await engine.step(
                        domain_state={"grid": grid, "score": score, "legal_moves": legal_moves},
                        questions=[q],
                        legal_actions=legal_moves
                    )
                    v = decisions["move_direction"]
                    chosen_move = str(v.selected)
                    t_jev_ms = (time.perf_counter() - t_jev_start) * 1000.0

                    if not v.is_fallback:
                        jev_calls_count += 1
                        jev_latencies.append(t_jev_ms)
                        opt_mark = "★" if chosen_move == max(lookahead_scores, key=lambda m: lookahead_scores[m]) else "▲"
                        decision_info = f"\033[1;35m[Jev Model]\033[0m {chosen_move} {opt_mark} (p={v.calibrated_confidence:.2f}, {t_jev_ms:.0f}ms)"
                    else:
                        jev_fallback_count += 1
                        decision_info = f"\033[1;33m[Engine FB]\033[0m {chosen_move} (Fallback: {v.fallback_reason[:25]})"
                except Exception as exc:
                    jev_fallback_count += 1
                    best_m, best_s, _ = search_best_move(grid, depth=2)
                    chosen_move = best_m
                    decision_info = f"\033[1;33m[Lookahead FB]\033[0m {chosen_move} (Error: {str(exc)[:30]})"
            else:
                # High-speed expectimax lookahead
                best_m, best_s, scores = search_best_move(grid, depth=2)
                chosen_move = best_m
                decision_info = f"\033[1;36m[Lookahead-2]\033[0m {chosen_move} (score={best_s:.0f})"

            if not chosen_move or chosen_move not in legal_moves:
                chosen_move = legal_moves[0]
                decision_info += " [Forced fallback]"

            # Execute move in browser and await state settlement to eliminate ghost polling
            settled = await controller.send_move_and_wait_settlement(
                chosen_move,
                initial_grid=grid,
                timeout_s=0.15
            )
            moves_count += 1

            # Update engine memory and feedback
            if engine:
                score_gain = settled.get("score", score) - score
                engine.record_feedback(
                    outcome="SUCCESS",
                    metadata={"score_gain": score_gain, "score": settled.get("score", score)}
                )

            t1 = time.perf_counter()
            step_latency_ms = (t1 - t0) * 1000.0
            total_latency_ms += step_latency_ms

            # Render dashboard
            print_board(grid, score, moves_count, max_tile, step_latency_ms, decision_info)

            # Sleep delay
            if args.delay > 0:
                await asyncio.sleep(args.delay)

    except KeyboardInterrupt:
        print("\n[DecideX 2048] Stopped by user (Ctrl+C).")
    except Exception as exc:
        print(f"\n[DecideX 2048] Run encountered error: {exc}")
    finally:
        if engine:
            engine.close()
        if jev_provider:
            await jev_provider.aclose()
        if controller:
            await controller.close()
        print("\n" + "=" * 52)
        print("📊 Session Summary:")
        print(f"  Total Moves Executed: {moves_count}")
        if moves_count > 0:
            print(f"  Average Step Latency: {total_latency_ms / moves_count:.2f}ms")
        print(f"  Jev Decisions Count : {jev_calls_count} (Fallbacks: {jev_fallback_count})")
        if jev_latencies:
            print(f"  Average Jev Latency : {sum(jev_latencies) / len(jev_latencies):.1f}ms")
        if pool:
            stats = pool.stats()
            print(f"  KeyPool Requests    : {stats['total_requests']}")
            print(f"  KeyPool Success Rate: {stats['success_rate'] * 100:.1f}%")
        print("=" * 52 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
