"""
Balatro state model: what the advisor "sees" at one decision point.

The shape mirrors the JSON the LÖVE/Steamodded bridge dumps
(``tools/balatro_bridge``), so a snapshot file parses straight into
:data:`BalatroState`. Everything is a plain dataclass — no game install
required to reason about a state offline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Suits (single letters used by the bridge).
SUITS = ("S", "H", "D", "C")
SUIT_SYMBOL = {"S": "♠", "H": "♥", "D": "♦", "C": "♣"}
# Ranks "2".."9", "10", "J", "Q", "K", "A".
RANKS = ("2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A")
RANK_CHIPS = {r: int(r) for r in RANKS if r.isdigit()}
RANK_CHIPS.update({"10": 10, "J": 10, "Q": 10, "K": 10, "A": 11})
RANK_ORDER = {r: i for i, r in enumerate(RANKS)}
# Ace plays high for straights (A also low in vanilla: A2345 is a straight).
ACE_LOWTAIL = ("A", "2", "3", "4", "5")

ENHANCEMENTS = ("bonus", "mult", "wild", "glass", "steel", "gold", "lucky")
EDITIONS = ("foil", "holo", "poly")
SEALS = ("red", "blue", "gold", "purple")


@dataclass
class Card:
    """One playing card as the bridge reports it (display order preserved)."""

    rank: str                      # "A", "10", "7", ...
    suit: str                      # S/H/D/C
    enhancement: Optional[str] = None
    edition: Optional[str] = None
    seal: Optional[str] = None

    @property
    def chips(self) -> int:
        return RANK_CHIPS.get(self.rank, 0)

    @property
    def label(self) -> str:
        return f"{self.rank}{SUIT_SYMBOL.get(self.suit, self.suit)}"

    def suits(self) -> tuple:
        """Suits this card counts as (wild counts as every suit)."""
        if self.enhancement == "wild":
            return SUITS
        return (self.suit,)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rank": self.rank, "suit": self.suit,
            "enhancement": self.enhancement, "edition": self.edition, "seal": self.seal,
        }


@dataclass
class Joker:
    """One joker slot. ``chips``/``mult``/``xmult`` are the joker's *current*
    static values as reported by the bridge; per-card triggered effects for a
    curated subset are resolved by name in ``scoring.py``."""

    name: str
    chips: int = 0
    mult: int = 0
    xmult: float = 1.0
    counter: Optional[int] = None   # e.g. Ride the Bus / Green Joker stack
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "chips": self.chips, "mult": self.mult,
                "xmult": self.xmult, "counter": self.counter, "extra": self.extra}


@dataclass
class Consumable:
    """Tarot / Planet / Spectral slot (effects are not simulated; the advisor
    only decides *whether* to use it before acting)."""

    name: str
    type: str = "tarot"            # tarot | planet | spectral

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "type": self.type}


@dataclass
class Blind:
    name: str = "Small Blind"
    type: str = "small"            # small | big | boss
    chips: int = 300               # target score
    scored: int = 0                # chips already banked this blind

    @property
    def remaining(self) -> int:
        return max(0, self.chips - self.scored)

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "type": self.type, "chips": self.chips, "scored": self.scored}


@dataclass
class BalatroState:
    """Full observable state at one in-blind decision point."""

    hand: List[Card]
    blind: Blind = field(default_factory=Blind)
    jokers: List[Joker] = field(default_factory=list)
    consumables: List[Consumable] = field(default_factory=list)
    hands_left: int = 4
    discards_left: int = 3
    hand_size: int = 8
    money: int = 4
    ante: int = 1
    round: int = 1
    # What the human actually did at this decision point (bridge-filled), for
    # advisor-vs-player comparison. {"type": "play"|"discard"|"use", "cards": [idx]}
    actual: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "hand": [c.to_dict() for c in self.hand],
            "blind": self.blind.to_dict(),
            "jokers": [j.to_dict() for j in self.jokers],
            "consumables": [c.to_dict() for c in self.consumables],
            "hands_left": self.hands_left, "discards_left": self.discards_left,
            "hand_size": self.hand_size, "money": self.money,
            "ante": self.ante, "round": self.round,
            "actual": self.actual,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "BalatroState":
        blind = d.get("blind") or {}
        return cls(
            hand=[Card(**{k: c.get(k) for k in ("rank", "suit", "enhancement", "edition", "seal")})
                  for c in d.get("hand", [])],
            blind=Blind(
                name=blind.get("name", "Small Blind"),
                type=blind.get("type", "small"),
                chips=int(blind.get("chips", 300)),
                scored=int(blind.get("scored", 0)),
            ),
            jokers=[Joker(
                name=j.get("name", "?"),
                chips=int(j.get("chips", 0) or 0),
                mult=int(j.get("mult", 0) or 0),
                xmult=float(j.get("xmult", 1) or 1),
                counter=j.get("counter"),
                extra=j.get("extra") or {},
            ) for j in d.get("jokers", [])],
            consumables=[Consumable(name=c.get("name", "?"), type=c.get("type", "tarot"))
                         for c in d.get("consumables", [])],
            hands_left=int(d.get("hands_left", 4)),
            discards_left=int(d.get("discards_left", 3)),
            hand_size=int(d.get("hand_size", 8)),
            money=int(d.get("money", 4)),
            ante=int(d.get("ante", 1)),
            round=int(d.get("round", 1)),
            actual=d.get("actual"),
        )


def load_state(path) -> BalatroState:
    """Load one bridge snapshot (JSON) into a :class:`BalatroState`."""
    import json
    with open(path, "r", encoding="utf-8") as f:
        return BalatroState.from_dict(json.load(f))
