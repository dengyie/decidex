"""
DecideX Demo 2: Combinatorial Roguelike Card Game (Balatro-style).
Demonstrates:
- Combinatorial pruning (CandidatePruner) cutting 56+ combinations to Top-4 options
- Multi-head inference (Choice action + Noul tarot decision + Score win likelihood)
- CalibratedDecisionGuard checking margin and legal bounds
- What vs. How decoupling
"""

import asyncio
import os

from decidex.engine import DecisionEngine
from decidex.guards import CalibratedDecisionGuard
from decidex.pruner import CandidatePruner
from decidex.providers.mock import MockReplayProvider
from decidex.types import (
    ActionCandidate,
    PrimitiveType,
    QuestionSpec,
    TurnPhase,
)


async def run_balatro_simulation():
    print("\n" + "=" * 60)
    print("🃏 Starting Balatro Tactical Turn-based Demo")
    print("=" * 60)

    journal_path = os.path.join(os.path.dirname(__file__), "balatro_journal.jsonl")
    if os.path.exists(journal_path):
        os.remove(journal_path)

    # 1. Generate large pool of candidate actions from current 8-card hand
    # Hand: [A♠, K♠, Q♠, J♠, 10♠, 7♥, 7♦, 2♣]
    raw_candidates = [
        ActionCandidate(
            candidate_id="play_royal_flush_spades",
            description="Play Royal Flush: A♠ K♠ Q♠ J♠ 10♠",
            heuristic_score=100.0 * 8.0,  # 800 pts
            metadata={"type": "PLAY", "hand_type": "Straight Flush", "cards": ["A♠", "K♠", "Q♠", "J♠", "10♠"]}
        ),
        ActionCandidate(
            candidate_id="play_pair_sevens",
            description="Play Pair: 7♥ 7♦",
            heuristic_score=20.0 * 2.0,  # 40 pts
            metadata={"type": "PLAY", "hand_type": "Pair", "cards": ["7♥", "7♦"]}
        ),
        ActionCandidate(
            candidate_id="play_high_card_ace",
            description="Play High Card: A♠",
            heuristic_score=15.0 * 1.0,  # 15 pts
            metadata={"type": "PLAY", "hand_type": "High Card", "cards": ["A♠"]}
        ),
        ActionCandidate(
            candidate_id="discard_low_cards",
            description="Discard 7♥ 7♦ 2♣ to draw stronger Jokers synergy",
            heuristic_score=65.0,
            metadata={"type": "DISCARD", "cards": ["7♥", "7♦", "2♣"]}
        ),
        ActionCandidate(
            candidate_id="discard_deuce",
            description="Discard single 2♣",
            heuristic_score=30.0,
            metadata={"type": "DISCARD", "cards": ["2♣"]}
        )
    ]

    print(f"📊 Initial combinatorial actions available: {len(raw_candidates) + 50} (simulated combinations)")

    # 2. Prune candidates to top-3 using CandidatePruner
    top_candidates = CandidatePruner.prune_to_top_k(raw_candidates, k=3)
    options, lookup, descriptions = CandidatePruner.to_choice_bundle(top_candidates)

    print("✂️  Pruned candidate actions for Model Evaluation:")
    for opt in options:
        print(f"   - [{opt}] {descriptions[opt]} (Heuristic Score: {lookup[opt].heuristic_score:.0f})")

    # 3. Setup mock provider simulating calibrated model output
    def balatro_solver(payload, question):
        if question.id == "hand_action":
            return "play_royal_flush_spades", 0.98, {
                "play_royal_flush_spades": 0.98,
                "discard_low_cards": 0.015,
                "play_pair_sevens": 0.005
            }
        elif question.id == "use_tarot_first":
            # Consumable: The Hermit (doubles money up to $20)
            return False, 0.92, {"false": 0.92, "true": 0.08}
        elif question.id == "blind_clear_likelihood":
            return 0.99, 0.95, {}
        return options[0], 0.90, {}

    provider = MockReplayProvider(solver_fn=balatro_solver)
    guard = CalibratedDecisionGuard(temperature=1.25, min_confidence=0.60, min_margin=0.20)
    engine = DecisionEngine(provider=provider, guard=guard, journal_path=journal_path)

    # 4. Formulate multi-head questions for the step
    questions = [
        QuestionSpec(
            id="hand_action",
            primitive=PrimitiveType.CHOICE,
            description="Select the optimal card play or discard to beat the 600-chip Boss Blind.",
            options=options
        ),
        QuestionSpec(
            id="use_tarot_first",
            primitive=PrimitiveType.NOUL,
            description="Should we activate our held Tarot card 'The Hermit' before playing?"
        ),
        QuestionSpec(
            id="blind_clear_likelihood",
            primitive=PrimitiveType.SCORE,
            description="Estimated probability of clearing the Boss Blind this round",
            scale=(0.0, 1.0)
        )
    ]

    domain_state = {
        "blind_name": "The Pillar",
        "chips_needed": 600,
        "current_chips": 0,
        "hands_left": 4,
        "discards_left": 3,
        "held_tarots": ["The Hermit"],
        "hand_cards": ["A♠", "K♠", "Q♠", "J♠", "10♠", "7♥", "7♦", "2♣"]
    }

    # 5. Execute decision step
    verdicts = await engine.step(
        domain_state=domain_state,
        questions=questions,
        legal_actions=options,
        phase=TurnPhase.MAIN
    )

    action_verdict = verdicts["hand_action"]
    tarot_verdict = verdicts["use_tarot_first"]
    score_verdict = verdicts["blind_clear_likelihood"]

    print("\n🎯 Model Decision Results:")
    print(f"   Selected Action       : {action_verdict.selected}")
    print(f"   Action Confidence     : {action_verdict.calibrated_confidence:.3f}")
    print(f"   Use Tarot First?      : {tarot_verdict.selected} (conf: {tarot_verdict.calibrated_confidence:.3f})")
    print(f"   Blind Clear Score     : {score_verdict.selected}")

    # 6. Actuate: deterministic game execution based on model's intent
    selected_candidate = lookup[str(action_verdict.selected)]
    print(f"\n🚀 Actuating game client dispatch:")
    print(f"   Action Type : {selected_candidate.metadata['type']}")
    print(f"   Cards Played: {selected_candidate.metadata['cards']}")
    print(f"   Outcome     : Blind CLEARED in 1 hand! (+800 chips vs 600 required)")

    engine.record_feedback("ROUND_WON", {"chips_scored": 800})
    engine.close()

    print("-" * 60)
    print(f"✅ Balatro demo completed.")
    print(f"📁 Telemetry journal saved to: {journal_path}\n")


if __name__ == "__main__":
    asyncio.run(run_balatro_simulation())
