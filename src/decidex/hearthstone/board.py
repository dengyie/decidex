"""
Hearthstone domain model: board state entities and evidence assembly.

The advisor is READ-ONLY by design: it turns an already-observed board state
(populated from Power.log or a manual snapshot) into a compact, model-facing
evidence dict. It never touches the game client.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Hero:
    """A player's hero (face), including any equipped weapon attack."""
    name: str
    health: int
    max_health: int
    armor: int = 0
    attack: int = 0                      # current weapon attack, 0 if none
    can_attack: bool = False             # hero may attack this turn (weapon equipped + not used)
    hero_power_cost: int = 2
    hero_power_usable: bool = True


@dataclass
class Minion:
    """A minion on the board."""
    id: str
    name: str
    attack: int
    health: int
    max_health: int
    taunt: bool = False
    divine_shield: bool = False
    can_attack: bool = False             # False = summoning sickness / already attacked


@dataclass
class HandCard:
    """A card in the player's hand."""
    id: str
    name: str
    cost: int
    card_type: str = "minion"            # "minion" | "spell" | "weapon"
    attack: int = 0
    health: int = 0
    taunt: bool = False
    charge: bool = False
    battlecry: str = ""                  # human-readable battlecry note, if any
    target_scopes: Optional[List[str]] = None
    # target_scopes subsets: "enemy_minion", "enemy_hero", "friendly_minion",
    # "friendly_hero", "any_minion". None / empty => plays without a target.


@dataclass
class BoardState:
    """Full snapshot of a single turn's decision point (from the acting player's view)."""
    turn: int
    mana: int
    max_mana: int
    my_hero: Hero
    opp_hero: Hero
    my_minions: List[Minion] = field(default_factory=list)
    opp_minions: List[Minion] = field(default_factory=list)
    hand: List[HandCard] = field(default_factory=list)
    deck_count: int = 0
    fatigue: int = 0


def _hero_evidence(h: Hero) -> Dict[str, Any]:
    return {
        "name": h.name,
        "health": h.health,
        "max_health": h.max_health,
        "armor": h.armor,
        "attack": h.attack,
        "can_attack": h.can_attack,
    }


def _minion_evidence(m: Minion) -> Dict[str, Any]:
    return {
        "id": m.id,
        "name": m.name,
        "attack": m.attack,
        "health": m.health,
        "max_health": m.max_health,
        "taunt": m.taunt,
        "can_attack": m.can_attack,
    }


def _card_evidence(c: HandCard) -> Dict[str, Any]:
    return {
        "id": c.id,
        "name": c.name,
        "cost": c.cost,
        "type": c.card_type,
        "attack": c.attack,
        "health": c.health,
        "taunt": c.taunt,
        "charge": c.charge,
        "targets_required": bool(c.target_scopes),
    }


def build_evidence(state: BoardState, legal_action_count: int) -> Dict[str, Any]:
    """
    Compress the board into a compact, model-readable observation.
    This is DecideX stage 1 (Evidence Assembly) specialized for Hearthstone.
    """
    return {
        "turn": state.turn,
        "phase": "main",
        "mana": {"available": state.mana, "max": state.max_mana},
        "my_hero": _hero_evidence(state.my_hero),
        "opp_hero": _hero_evidence(state.opp_hero),
        "my_board": [_minion_evidence(m) for m in state.my_minions],
        "opp_board": [_minion_evidence(m) for m in state.opp_minions],
        "hand": [_card_evidence(c) for c in state.hand],
        "deck": {"remaining": state.deck_count, "fatigue": state.fatigue},
        "legal_action_count": legal_action_count,
    }
