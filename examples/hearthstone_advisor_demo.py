"""
DecideX Demo: Hearthstone read-only "best play" advisor.

Shows the full loop with a MockReplayProvider standing in for a Jev / cloned
player policy: deterministic legal-action enumeration -> evidence assembly ->
DecisionEngine -> gated recommendation (with fallback if confidence is low).

Run:  python examples/hearthstone_advisor_demo.py
"""

import asyncio

from decidex.hearthstone import (
    BoardState,
    HandCard,
    HearthstoneAdvisor,
    Hero,
    Minion,
)
from decidex.providers.mock import MockReplayProvider


def sample_board() -> BoardState:
    """A mid-game board: opponent has taunt; we hold a removal spell and a minion."""
    return BoardState(
        turn=6,
        mana=6,
        max_mana=6,
        my_hero=Hero(name="Rexxar", health=30, max_health=30, hero_power_cost=2),
        opp_hero=Hero(name="Jaina", health=18, max_health=30),
        my_minions=[
            Minion(id="ally1", name="Alleycat", attack=1, health=1, max_health=1, can_attack=True),
            Minion(id="ally2", name="Houndmaster", attack=4, health=3, max_health=3, can_attack=True),
        ],
        opp_minions=[
            Minion(id="enemy_taunt", name="Sen'jin Shieldmasta", attack=3, health=5, max_health=5, taunt=True),
            Minion(id="enemy2", name="Water Elemental", attack=3, health=6, max_health=6),
        ],
        hand=[
            HandCard(id="c_kill", name="Kill Command", cost=3, card_type="spell",
                     target_scopes=["enemy_minion", "enemy_hero"]),
            HandCard(id="c_beast", name="Animal Companion", cost=3, card_type="minion", attack=4, health=4),
        ],
        deck_count=12,
    )


def aggressive_policy(payload, question):
    """
    A hand-written stand-in for a cloned player style ("go face when ahead").
    In production this is a Jev/NanoJev call or a behaviour-cloned policy.
    """
    obs = payload.observation
    opp_hero_hp = obs["opp_hero"]["health"]
    for opt in question.options:
        if opt.startswith("play:c_kill->Jaina") and opp_hero_hp <= 20:
            return "play:c_kill->Jaina", 0.90, {"play:c_kill->Jaina": 0.90, "end_turn": 0.10}
    return question.options[0], 0.62, {question.options[0]: 0.62, "end_turn": 0.38}


async def main():
    provider = MockReplayProvider(solver_fn=aggressive_policy)
    advisor = HearthstoneAdvisor(provider=provider, top_k=8)

    state = sample_board()
    rec = await advisor.recommend(state)

    print("=" * 60)
    print("🃏 Hearthstone Advisor — recommended play")
    print("=" * 60)
    print(f"   Legal actions : {len(rec.legal_actions)}")
    print(f"   Recommendation: {rec.candidate_id}")
    print(f"   Description   : {rec.description}")
    print(f"   Confidence    : {rec.confidence:.3f}")
    print(f"   Fallback      : {rec.is_fallback} ({rec.fallback_reason or '-'})")
    print()
    print("   Legal envelope (what the model chose from):")
    for a in rec.legal_actions:
        print(f"     - [{a['action_type']:>11}] {a['id']}")
    advisor.close()


if __name__ == "__main__":
    asyncio.run(main())
