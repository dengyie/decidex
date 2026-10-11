"""
Hearthstone advisor: a read-only "best play" recommender built on DecideX.

Legal-action enumeration (rules) + board evidence assembly (board) + the
HearthstoneAdvisor facade (advisor) that drives DecisionEngine with the legal
envelope, so the model only ever chooses among legal actions.

New in 0.2: perception (real Power.log -> BoardState) and replay (compare
advisor recommendations against real human choices in recorded games). These
need the optional ``hslog`` dependency; the core advisor works without it.
"""

from decidex.hearthstone.advisor import HearthstoneAdvisor, Recommendation
from decidex.hearthstone.board import (
    BoardState,
    HandCard,
    Hero,
    Minion,
    build_evidence,
)
from decidex.hearthstone.rules import enumerate_legal_actions

__all__ = [
    "HearthstoneAdvisor",
    "Recommendation",
    "BoardState",
    "HandCard",
    "Hero",
    "Minion",
    "build_evidence",
    "enumerate_legal_actions",
]

# Optional real-game perception / replay (require hslog + hearthstone libs).
try:
    from decidex.hearthstone.perception import (
        BoardSnapshot,
        HearthstoneGameReader,
        load_game_snapshots,
    )
    from decidex.hearthstone.replay import (
        TurnReport,
        replay_game,
    )
    __all__ += [
        "BoardSnapshot",
        "HearthstoneGameReader",
        "load_game_snapshots",
        "TurnReport",
        "replay_game",
    ]
except ImportError:
    # hslog not installed: core read-only advisor still works on manual BoardState.
    pass