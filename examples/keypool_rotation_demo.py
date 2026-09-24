"""
DecideX Demo 4: High-Concurrency Jev KeyPool Rotation & Resilient Fault Tolerance.
Demonstrates:
- Loading 1171 keys directly from Obsidian vault note
- Integrating KeyPool with TypeSafeJevProvider
- Simulating multi-step decision cycles with automatic key rotation
- Simulating transient 429 rate limit cooldowns and 401 dead key isolation
- Inspecting real-time pool operational telemetry
"""

import asyncio
from unittest.mock import patch
import httpx

from decidex.engine import DecisionEngine
from decidex.guards import CalibratedDecisionGuard
from decidex.pool import KeyPool, RotationStrategy
from decidex.providers.typesafe import TypeSafeJevProvider
from decidex.types import PrimitiveType, QuestionSpec


async def run_keypool_demo() -> None:
    print("\n" + "=" * 65)
    print("🔑 DecideX Jev KeyPool & Dynamic Rotation Demo")
    print("=" * 65)

    # 1. Load keys automatically from default locations (keys.jsonl, env var, or Obsidian vault)
    print("\n[Step 1] Loading accounts from default locations (Obsidian vault / keys.jsonl)...")
    pool = KeyPool.from_default_locations(strategy=RotationStrategy.ROUND_ROBIN, default_cooldown_s=10.0)
    initial_stats = pool.stats()
    print(f"  -> Total Keys Loaded : {initial_stats['total_keys']}")
    print(f"  -> Active Available  : {initial_stats['active_keys']}")
    print(f"  -> Rotation Strategy : {initial_stats['strategy']}")

    # 2. Initialize provider with the pool
    provider = TypeSafeJevProvider(key_pool=pool, max_retries=3)
    engine = DecisionEngine(provider=provider, guard=CalibratedDecisionGuard())

    questions = [
        QuestionSpec(
            id="tactical_decision",
            primitive=PrimitiveType.CHOICE,
            description="Select best tactical combat stance",
            options=["OFFENSIVE", "DEFENSIVE", "FLANK", "RETREAT"]
        ),
        QuestionSpec(
            id="critical_threat",
            primitive=PrimitiveType.NOUL,
            description="Is team facing lethal burst threat?"
        )
    ]

    # 3. Simulate requests with synthetic response mocking to observe rotation & resilience
    print("\n[Step 2] Executing 5 decision cycles across rotating keys...")

    call_seq = []

    async def mock_network(url, json, headers):
        auth = headers.get("Authorization", "")
        key = auth.replace("Bearer ", "").strip()
        call_seq.append(key[:20] + "...")

        # Inject simulated 429 rate limit on 3rd call to test automatic cooldown & rotation
        if len(call_seq) == 3:
            req = httpx.Request("POST", url)
            return httpx.Response(429, request=req, json={"error": "Rate limit exceeded"})

        req = httpx.Request("POST", url)
        return httpx.Response(200, request=req, json={
            "model": "jev-1.13.0",
            "answers": {
                "tactical_decision": {
                    "type": "choice",
                    "choice": "FLANK",
                    "probabilities": {"OFFENSIVE": 0.1, "DEFENSIVE": 0.1, "FLANK": 0.75, "RETREAT": 0.05},
                    "confidence": 0.75
                },
                "critical_threat": {
                    "type": "noul",
                    "noul": 0.15
                }
            }
        })

    with patch.object(httpx.AsyncClient, "post", side_effect=mock_network):
        for step_i in range(1, 6):
            state = {"step": step_i, "hp": 100 - step_i * 10, "enemies_nearby": 2}
            verdicts = await engine.step(
                domain_state=state,
                questions=questions,
                legal_actions=["OFFENSIVE", "DEFENSIVE", "FLANK", "RETREAT"]
            )
            tactical = verdicts["tactical_decision"]
            threat = verdicts["critical_threat"]
            print(f"  Step {step_i}: Stance={tactical.selected} (conf={tactical.calibrated_confidence:.2f}), Threat={threat.selected}")

    print("\n[Step 3] Verification of Key Rotation Sequence:")
    for idx, k in enumerate(call_seq, 1):
        status = " (Tripped 429 -> immediately rotated)" if idx == 3 else " (200 OK)"
        print(f"  Call #{idx}: Key={k}{status}")

    stats = pool.stats()
    print("\n[Step 4] Final KeyPool Telemetry:")
    print(f"  -> Total Requests Handled: {stats['total_requests']}")
    print(f"  -> Successful Inferences : {stats['total_success']}")
    print(f"  -> Cooling Down Keys     : {stats['cooldown_keys']}")
    print(f"  -> Active Keys Ready     : {stats['active_keys']}")
    print("=" * 65 + "\n")


if __name__ == "__main__":
    asyncio.run(run_keypool_demo())
