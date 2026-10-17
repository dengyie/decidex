"""
Constraints and Boss Defense Engine.

Certain jokers and boss blinds impose lethal conditions that can immediately ruin a run:
  1. Ride the Bus: Playing a scoring face card (J, Q, K) resets mult to 0!
  2. Green Joker: Discarding loses -1 mult, playing gains +1 mult. Discards are toxic.
  3. Square Joker: Playing exactly 4 cards permanently gains +4 chips.
  4. Banner: Retaining discards yields +30 chips each; squandering discards costs score.
  5. Boss Blinds:
     - The Needle: Exactly 1 hand allowed. Must burn all discards to build a 1-shot kill.
     - The Water: 0 discards. Every play must double as card filtering.
     - The Eye: Cannot repeat the same poker hand type.
     - The Mouth: Only 1 poker hand type permitted all round.
     - The Flint: Base chips and mult are halved.
     - Suit Debuffers (The Club, The Goad, The Window, The Head): Debuffed suits score 0 and trigger nothing.

This module detects active hazards and enforces strict safety filters on candidate plays and discards.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from .state import Card, Joker


FACE_RANKS = {"J", "Q", "K"}

DEBUFFED_SUIT_BOSSES = {
    "The Club": "C",
    "The Goad": "S",
    "The Window": "D",
    "The Head": "H",
}


@dataclass
class ActiveConstraints:
    has_ride_the_bus: bool = False
    has_green_joker: bool = False
    has_square_joker: bool = False
    has_banner: bool = False
    boss_name: str = ""
    is_the_needle: bool = False
    is_the_water: bool = False
    is_the_eye: bool = False
    is_the_mouth: bool = False
    is_the_flint: bool = False
    debuffed_suit: Optional[str] = None
    played_hand_types_this_round: Set[str] = field(default_factory=set)
    active_warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "has_ride_the_bus": self.has_ride_the_bus,
            "has_green_joker": self.has_green_joker,
            "has_square_joker": self.has_square_joker,
            "has_banner": self.has_banner,
            "boss_name": self.boss_name,
            "is_the_needle": self.is_the_needle,
            "is_the_water": self.is_the_water,
            "is_the_eye": self.is_the_eye,
            "is_the_mouth": self.is_the_mouth,
            "is_the_flint": self.is_the_flint,
            "debuffed_suit": self.debuffed_suit,
            "played_hands": list(self.played_hand_types_this_round),
            "warnings": self.active_warnings,
        }


def detect_active_constraints(
    jokers: List[Joker],
    blind_name: str = "",
    played_hands: Optional[Set[str]] = None,
) -> ActiveConstraints:
    """Scans jokers and current boss blind to identify active operational hazards."""
    jnames = {j.name for j in (jokers or [])}

    has_bus = "Ride the Bus" in jnames
    has_green = "Green Joker" in jnames
    has_square = "Square Joker" in jnames
    has_banner = "Banner" in jnames

    is_needle = (blind_name == "The Needle")
    is_water = (blind_name == "The Water")
    is_eye = (blind_name == "The Eye")
    is_mouth = (blind_name == "The Mouth")
    is_flint = (blind_name == "The Flint")
    debuff_suit = DEBUFFED_SUIT_BOSSES.get(blind_name)

    warnings = []
    if has_bus:
        warnings.append("🚨 [Ride the Bus] 严禁打出计分的人头牌 (J/Q/K)，否则倍率归零！")
    if has_green:
        warnings.append("⚠️ [Green Joker] 每次弃牌 -1 Mult；优先出杂牌单打充当伪弃牌。")
    if is_needle:
        warnings.append("☠️ [The Needle] 全局仅剩 1 次出牌机会！必须在出牌前用光弃牌凑出斩杀爆发。")
    if is_eye:
        warnings.append("👁️ [The Eye] 本回合不可重复打出相同牌型！")
    if debuff_suit:
        warnings.append(f"🛡️ [{blind_name}] 封印花色 '{debuff_suit}'，该花色牌不计分且无加成！")

    return ActiveConstraints(
        has_ride_the_bus=has_bus,
        has_green_joker=has_green,
        has_square_joker=has_square,
        has_banner=has_banner,
        boss_name=blind_name,
        is_the_needle=is_needle,
        is_the_water=is_water,
        is_the_eye=is_eye,
        is_the_mouth=is_mouth,
        is_the_flint=is_flint,
        debuffed_suit=debuff_suit,
        played_hand_types_this_round=played_hands or set(),
        active_warnings=warnings,
    )


@dataclass
class PlayConstraintVerdict:
    is_fatal_violation: bool
    penalty_multiplier: float  # Multiplier on candidate heuristic score (0.0 to 1.0)
    bonus_multiplier: float    # Growth bonus (e.g. 1.25 for Square Joker 4-card play)
    warning_title: Optional[str]
    reason: str


def evaluate_play_safety(
    cards: List[Card],
    hand_type: str,
    constraints: ActiveConstraints,
) -> PlayConstraintVerdict:
    """
    Checks whether a candidate play violates active rules or triggers joker growth.
    """
    # 1. Ride the Bus: Fatal if any played card is J, Q, or K
    if constraints.has_ride_the_bus:
        face_cards = [c for c in cards if c.rank in FACE_RANKS]
        if face_cards:
            return PlayConstraintVerdict(
                is_fatal_violation=True,
                penalty_multiplier=0.01,
                bonus_multiplier=1.0,
                warning_title="破戒清零警报",
                reason=f"打出人头牌 ({', '.join(c.label for c in face_cards)}) 将导致 Ride the Bus 倍率清零！",
            )

    # 2. The Eye: Boss blind forbids repeating the same poker hand type
    if constraints.is_the_eye and hand_type in constraints.played_hand_types_this_round:
        return PlayConstraintVerdict(
            is_fatal_violation=True,
            penalty_multiplier=0.01,
            bonus_multiplier=1.0,
            warning_title="The Eye 规则违规",
            reason=f"本回合已打过 '{hand_type}'，再次打出将判定为 0 分！",
        )

    # 3. Debuffed Suit Bosses: Heavy penalty if majority of cards belong to debuffed suit
    penalty = 1.0
    reason_parts = []
    if constraints.debuffed_suit:
        debuffed_count = sum(1 for c in cards if constraints.debuffed_suit in c.suits())
        if debuffed_count > 0:
            ratio = debuffed_count / max(1, len(cards))
            penalty *= max(0.2, 1.0 - ratio * 0.8)
            reason_parts.append(f"包含 {debuffed_count} 张被封印的 {constraints.debuffed_suit} 花色")

    # 4. Square Joker Growth Bonus: Playing exactly 4 cards grants permanent +4 chips
    growth_bonus = 1.0
    if constraints.has_square_joker and len(cards) == 4:
        growth_bonus = 1.25
        reason_parts.append("触发 Square Joker (+4 永久筹码成长)")

    reason_str = " | ".join(reason_parts) if reason_parts else "动作完全符合所有安全与小丑成长准则"
    return PlayConstraintVerdict(
        is_fatal_violation=False,
        penalty_multiplier=round(penalty, 2),
        bonus_multiplier=round(growth_bonus, 2),
        warning_title=None,
        reason=reason_str,
    )
