"""
decidex.balatro: read-only "best move" advisor for the real Balatro game.

Public surface mirrors decidex.hearthstone:
  - BalatroState / Card / Joker / Consumable / Blind  (state model)
  - load_state(path)                                  (bridge snapshot -> state)
  - enumerate_legal_actions(state)                    (deterministic rules)
  - simulate_play(cards, state)                       (deterministic scoring)
  - BalatroAdvisor                                    (five-stage recommender)

Compliance: read-only. The advisor recommends; it never actuates the game
client. The bridge mod (tools/balatro_bridge) only *observes* and dumps state.
"""

from .advisor import BalatroAdvisor, Recommendation, build_evidence
from .constraints import (
    ActiveConstraints,
    BOSS_CATALOG,
    BossRule,
    detect_active_constraints,
    evaluate_play_safety,
)
from .economy import EconomyStatus, calculate_economy_status
from .joker_order import compute_optimal_joker_order, estimate_ordering_gain
from .poker import (
    base_values,
    can_attach_junk,
    detect_hand_type,
    enumerate_selections,
    find_safe_junk_combinations,
    get_scoring_cards,
)
from .rules import choose_safe_default, enumerate_legal_actions
from .scoring import ScoreResult, simulate_play
from .state import (
    Blind,
    Card,
    Consumable,
    Joker,
    RANKS,
    SUITS,
    SUIT_SYMBOL,
    BalatroState,
    load_state,
)
from .synergy import SynergyProfile, analyze_synergy

__all__ = [
    "BalatroAdvisor", "Recommendation", "build_evidence",
    "BalatroState", "Card", "Joker", "Consumable", "Blind",
    "RANKS", "SUITS", "SUIT_SYMBOL", "load_state",
    "enumerate_legal_actions", "choose_safe_default",
    "simulate_play", "ScoreResult",
    "detect_hand_type", "base_values", "enumerate_selections",
    "get_scoring_cards", "can_attach_junk", "find_safe_junk_combinations",
    "BOSS_CATALOG", "BossRule", "ActiveConstraints",
    "detect_active_constraints", "evaluate_play_safety",
    "analyze_synergy", "SynergyProfile",
    "compute_optimal_joker_order", "estimate_ordering_gain",
    "calculate_economy_status", "EconomyStatus",
]
