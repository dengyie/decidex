"""
Deterministic Balatro score simulation for one played hand.

Simulates the vanilla scoring pass:
  base (chips, mult from hand type)
  -> each played card left-to-right: chips / +mult / xmult
     (enhancements, editions, red-seal retrigger)
  -> held-in-hand steel cards
  -> jokers left-to-right (static values + per-card hooks for a curated set)

Approximations (documented, deterministic):
  * glass / lucky use expected values, not RNG rolls;
  * xmult ordering is approximated as cards-then-jokers;
  * jokers outside :data:`KNOWN_JOKERS` contribute only their reported static
    chips/mult/xmult (the bridge reads those off the live joker object).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .constraints import DEBUFFED_SUIT_BOSSES, FACE_RANKS
from .poker import base_values, detect_hand_type, get_scoring_cards
from .state import Card, Joker

# ---- Curated vanilla joker table -------------------------------------------
# name -> dict of hooks evaluated against the played selection.
#   "per_card": {"chips"|"mult": amount, "match": callable(card)->bool}
#   "on_hand": {"chips"|"mult"|"xmult": amount, "when": callable(ctx)->bool}
_J = lambda **kw: kw


def _is_face(c: Card) -> bool:
    return c.rank in ("J", "Q", "K")


def _is_even(c: Card) -> bool:
    return c.rank.isdigit() and int(c.rank) % 2 == 0


def _is_odd(c: Card) -> bool:
    return c.rank == "A" or (c.rank.isdigit() and int(c.rank) % 2 == 1)


KNOWN_JOKERS: Dict[str, Dict] = {
    "Joker": {"on_hand": {"mult": 4}},
    "Greedy Joker": {"per_card": {"mult": 3, "match": lambda c: "D" in c.suits()}},
    "Lusty Joker": {"per_card": {"mult": 3, "match": lambda c: "H" in c.suits()}},
    "Wrathful Joker": {"per_card": {"mult": 3, "match": lambda c: "S" in c.suits()}},
    "Gluttonous Joker": {"per_card": {"mult": 3, "match": lambda c: "C" in c.suits()}},
    "Jolly Joker": {"on_hand": {"mult": 8, "when": lambda ctx: ctx["counts_pair"]}},
    "Zany Joker": {"on_hand": {"mult": 12, "when": lambda ctx: ctx["counts_trips"]}},
    "Mad Joker": {"on_hand": {"mult": 10, "when": lambda ctx: ctx["hand_type"] == "Two Pair"}},
    "Crazy Joker": {"on_hand": {"mult": 12, "when": lambda ctx: ctx["hand_type"] == "Straight"}},
    "Droll Joker": {"on_hand": {"mult": 10, "when": lambda ctx: ctx["hand_type"] == "Flush"}},
    "Sly Joker": {"on_hand": {"chips": 50, "when": lambda ctx: ctx["counts_pair"]}},
    "Wily Joker": {"on_hand": {"chips": 100, "when": lambda ctx: ctx["counts_trips"]}},
    "Clever Joker": {"on_hand": {"chips": 80, "when": lambda ctx: ctx["hand_type"] == "Two Pair"}},
    "Devious Joker": {"on_hand": {"chips": 100, "when": lambda ctx: ctx["hand_type"] == "Straight"}},
    "Crafty Joker": {"on_hand": {"chips": 80, "when": lambda ctx: ctx["hand_type"] == "Flush"}},
    "Half Joker": {"on_hand": {"mult": 20, "when": lambda ctx: ctx["n_played"] <= 3}},
    "Banner": {"on_hand": {"chips": "30*discards_left"}},
    "Mystic Summit": {"on_hand": {"mult": 15, "when": lambda ctx: ctx["discards_left"] == 0}},
    "Even Steven": {"per_card": {"mult": 4, "match": _is_even}},
    "Odd Todd": {"per_card": {"chips": 31, "match": _is_odd}},
    "Scholar": {"per_card": {"chips": 20, "mult": 4, "match": lambda c: c.rank == "A"}},
    "Walkie Talkie": {"per_card": {"chips": 10, "mult": 4, "match": lambda c: c.rank in ("10", "4")}},
    "Fibonacci": {"per_card": {"mult": 8, "match": lambda c: c.rank in ("A", "2", "3", "5", "8")}},
    "Scary Face": {"per_card": {"chips": 30, "match": _is_face}},
    "Abstract Joker": {"on_hand": {"mult": "3*n_jokers"}},
    "Blackboard": {"on_hand": {"xmult": 3, "when": lambda ctx: ctx["all_played_spades_clubs"]}},
    "Bull": {"on_hand": {"chips": "2*money"}},
    "Blue Joker": {"on_hand": {"chips": "2*cards_in_deck"}},
    "Baron": {"per_card": {"xmult": 1.5, "match": lambda c: c.rank == "K", "held": True}},
    "Shoot the Moon": {"per_card": {"mult": 13, "match": lambda c: c.rank == "Q", "held": True}},
}


@dataclass
class ScoreResult:
    hand_type: str
    chips: int
    mult: float
    score: float
    triggered: List[str]          # human-readable trigger log

    @property
    def display(self) -> str:
        fmt = lambda v: int(v) if float(v).is_integer() else round(v, 2)
        return f"{self.hand_type} {fmt(self.chips)}×{fmt(self.mult)} = {fmt(self.score)}"


def _card_score(card: Card) -> Tuple[int, float, float, List[str]]:
    """(chips, +mult, xmult, log) contribution of one scored card (no seals)."""
    chips, mult, xmult, log = card.chips, 0.0, 1.0, [f"{card.label} +{card.chips}"]
    if card.enhancement == "bonus":
        chips += 30; log.append("bonus+30")
    elif card.enhancement == "mult":
        mult += 4; log.append("mult+4")
    elif card.enhancement == "glass":
        xmult *= 2.0; log.append("glass×2(expect)")
    elif card.enhancement == "lucky":
        mult += 4.0; log.append("lucky+4(expect)")
    if card.edition == "foil":
        chips += 50; log.append("foil+50")
    elif card.edition == "holo":
        mult += 10; log.append("holo+10")
    elif card.edition == "poly":
        xmult *= 1.5; log.append("poly×1.5")
    return chips, mult, xmult, log


def simulate_play(
    played: Sequence[Card],
    state,  # BalatroState (typed loosely to avoid a cycle)
) -> ScoreResult:
    """Score one played selection deterministically. ``played`` is in play order."""
    hand_type = detect_hand_type(played)
    blind_name = getattr(getattr(state, "blind", None), "name", "")

    # The Psychic checks the selected hand size, not its scoring subset.
    if blind_name == "The Psychic" and len(played) < 5:
        return ScoreResult(
            hand_type,
            0,
            0.0,
            0.0,
            [f"The Psychic: 必须打满 5 张牌 (当前打出 {len(played)} 张，判定为 0 分)"],
        )

    base_chips, base_mult = base_values(hand_type)

    # Boss: The Flint halves base chips and mult
    if blind_name == "The Flint":
        base_chips = max(1, base_chips // 2)
        base_mult = max(1.0, base_mult / 2.0)

    chips, mult = float(base_chips), float(base_mult)
    log: List[str] = [f"base {base_chips}×{base_mult}"]

    n_by_rank: Dict[str, int] = {}
    for c in played:
        n_by_rank[c.rank] = n_by_rank.get(c.rank, 0) + 1
    played_set = {id(c) for c in played}
    held_cards = [c for c in state.hand if id(c) not in played_set]
    ctx = {
        "hand_type": hand_type,
        "n_played": len(played),
        "counts_pair": any(v >= 2 for v in n_by_rank.values()),
        "counts_trips": any(v >= 3 for v in n_by_rank.values()),
        "discards_left": state.discards_left,
        "money": state.money,
        "n_jokers": len(state.jokers),
        "cards_in_deck": 52 - 8,   # vanilla full-deck approximation
        # Blackboard: ALL cards *held in hand* (not the played ones) spades/clubs.
        "all_played_spades_clubs": bool(held_cards) and all(
            set(c.suits()) <= {"S", "C"} for c in held_cards),
    }

    known = [KNOWN_JOKERS.get(j.name, {}) for j in state.jokers]
    # Jokers with curated hooks use the hooks only — the bridge's reported
    # statics for those same jokers would double-count the effect.
    joker_static = [(j.chips, j.mult, j.xmult) for j, hooks in zip(state.jokers, known)
                    if not hooks]

    scoring_ids = {id(c) for c in get_scoring_cards(played, hand_type)}
    scoring_cards = [c for c in played if id(c) in scoring_ids]

    # Boss Debuffs
    debuffed_suit = DEBUFFED_SUIT_BOSSES.get(blind_name)
    is_plant = (blind_name == "The Plant")

    def _is_card_debuffed(c: Card) -> bool:
        if is_plant and _is_face(c):
            return True
        if debuffed_suit and debuffed_suit in c.suits():
            return True
        return False

    def _run(cards: Sequence[Card], held_mode: bool) -> None:
        nonlocal chips, mult
        for c in cards:
            debuffed = not held_mode and _is_card_debuffed(c)
            if held_mode:
                add_c, add_m = 0, 0.0
                xm = 2.0 if c.enhancement == "steel" else 1.0
                lg = [f"held {c.label}" + (" steel×2" if xm > 1 else "")]
            elif debuffed:
                add_c, add_m, xm, lg = 0, 0.0, 1.0, [f"{c.label} (封印失效)"]
            else:
                add_c, add_m, xm, lg = _card_score(c)
            retriggers = 2 if (c.seal == "red" and not held_mode and not debuffed) else 1
            for _ in range(retriggers):
                chips += add_c
                mult += add_m
                mult *= xm
                log.extend(lg)
                if not debuffed:
                    for hooks, joker in zip(known, state.jokers):
                        pc = hooks.get("per_card")
                        if pc and bool(pc.get("held", False)) != held_mode:
                            continue
                        if pc and (pc.get("match") is None or pc["match"](c)):
                            chips += pc.get("chips", 0)
                            mult += pc.get("mult", 0)
                            mult *= pc.get("xmult", 1.0)
                            log.append(f"{joker.name} → {c.label}")

    # 1) scoring cards left-to-right (joker per-card hooks interleave).
    _run(scoring_cards, held_mode=False)
    # 2) held-in-hand steel + held hooks.
    _run(held_cards, held_mode=True)

    # 3) jokers: static values (uncatalogued only), then on_hand hooks.
    for (jc, jm, jx) in joker_static:
        chips += jc
        mult += jm
        mult *= jx
    for hooks, joker in zip(known, state.jokers):
        oh = hooks.get("on_hand")
        if not oh:
            continue
        when = oh.get("when")
        if when is not None and not when(ctx):
            continue
        for key in ("chips", "mult", "xmult"):
            val = oh.get(key)
            if val is None:
                continue
            if isinstance(val, str):
                val = eval(val, {"__builtins__": {}}, dict(ctx))  # noqa: S307 - fixed formula strings
            if key == "chips":
                chips += val
            elif key == "mult":
                mult += val
            else:
                mult *= val
        log.append(f"{joker.name} on_hand")

    score = chips * mult
    return ScoreResult(hand_type, int(chips), mult, score, log)
