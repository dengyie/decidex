"""
Synergy Engine: Four Pillars of Balatro Joker Building.

In high-stake Balatro play, a winning build requires balancing four pillars:
  1. CHIPS: Flat chip generators (e.g. Blue Joker, Bull, Castle, Ice Cream, Scary Face)
  2. FLAT_MULT: Additive multiplier builders (e.g. Half Joker, Gros Michel, Ride the Bus, Green Joker)
  3. X_MULT: Multiplicative scaling finishers (e.g. Cavendish, Card Sharp, The Duo/Trio, Constellation)
  4. ECONOMY: Cash flow and deck thinners (e.g. Golden Joker, Mail-in Rebate, Trading Card, To the Moon)
  5. UTILITY: Re-trigger and duplication tools (e.g. Blueprint, Brainstorm, Hanging Chad, Hack)

A build with 5 Flat Mult jokers plateaus quickly; a build with X_Mult but no Flat Mult deals negligible damage.
This module evaluates build balance, detects missing pillars, and computes marginal purchase value.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set

from .state import Joker


class PillarType(str, Enum):
    CHIPS = "chips"
    FLAT_MULT = "flat_mult"
    X_MULT = "x_mult"
    ECONOMY = "economy"
    UTILITY = "utility"


@dataclass
class JokerMeta:
    name: str
    primary_pillar: PillarType
    secondary_pillar: Optional[PillarType] = None
    base_tier: str = "A"  # S+, S, A, B, C
    base_chips: int = 0
    base_mult: int = 0
    base_xmult: float = 1.0
    scaling: bool = False
    notes: str = ""


# Curated catalog of major Balatro jokers with pillar tagging and power tiers
JOKER_CATALOG: Dict[str, JokerMeta] = {
    # --- Pillar: CHIPS ---
    "Blue Joker": JokerMeta("Blue Joker", PillarType.CHIPS, base_tier="A", base_chips=70, notes="+2 chips per card in deck"),
    "Bull": JokerMeta("Bull", PillarType.CHIPS, base_tier="A", base_chips=50, scaling=True, notes="+2 chips per $1 held"),
    "Ice Cream": JokerMeta("Ice Cream", PillarType.CHIPS, base_tier="A", base_chips=100, notes="+100 chips decaying"),
    "Castle": JokerMeta("Castle", PillarType.CHIPS, base_tier="S", base_chips=60, scaling=True, notes="+3 chips per discarded suit card"),
    "Banner": JokerMeta("Banner", PillarType.CHIPS, base_tier="B", base_chips=90, notes="+30 chips per remaining discard"),
    "Scary Face": JokerMeta("Scary Face", PillarType.CHIPS, base_tier="A", base_chips=60, notes="+30 chips per face card scored"),
    "Odd Todd": JokerMeta("Odd Todd", PillarType.CHIPS, base_tier="B", base_chips=62, notes="+31 chips per odd card scored"),
    "Square Joker": JokerMeta("Square Joker", PillarType.CHIPS, base_tier="B", base_chips=40, scaling=True, notes="+4 chips when playing exactly 4 cards"),
    "Stuntman": JokerMeta("Stuntman", PillarType.CHIPS, base_tier="S", base_chips=250, notes="+250 chips, -2 hand size"),
    "Wee Joker": JokerMeta("Wee Joker", PillarType.CHIPS, base_tier="S", base_chips=50, scaling=True, notes="+8 chips per 2 scored"),
    "Arrowhead": JokerMeta("Arrowhead", PillarType.CHIPS, base_tier="B", base_chips=50, notes="+50 chips per spade scored"),

    # --- Pillar: FLAT_MULT ---
    "Gros Michel": JokerMeta("Gros Michel", PillarType.FLAT_MULT, base_tier="A", base_mult=15, notes="+15 mult, 1/6 extinct to unlock Cavendish"),
    "Half Joker": JokerMeta("Half Joker", PillarType.FLAT_MULT, base_tier="A", base_mult=20, notes="+20 mult if playing <= 3 cards"),
    "Ride the Bus": JokerMeta("Ride the Bus", PillarType.FLAT_MULT, base_tier="S", base_mult=20, scaling=True, notes="+1 mult per hand with no face cards"),
    "Green Joker": JokerMeta("Green Joker", PillarType.FLAT_MULT, base_tier="A", base_mult=20, scaling=True, notes="+1 mult per play, -1 per discard"),
    "Abstract Joker": JokerMeta("Abstract Joker", PillarType.FLAT_MULT, base_tier="A", base_mult=15, notes="+3 mult per owned joker"),
    "Even Steven": JokerMeta("Even Steven", PillarType.FLAT_MULT, base_tier="B", base_mult=16, notes="+4 mult per even card scored"),
    "Fibonacci": JokerMeta("Fibonacci", PillarType.FLAT_MULT, base_tier="S", base_mult=32, notes="+8 mult for Ace, 2, 3, 5, 8"),
    "Supernova": JokerMeta("Supernova", PillarType.FLAT_MULT, base_tier="A", base_mult=18, scaling=True, notes="+mult equal to times poker hand played"),
    "Red Card": JokerMeta("Red Card", PillarType.FLAT_MULT, base_tier="B", base_mult=15, scaling=True, notes="+3 mult when skipping a booster pack"),
    "Joker": JokerMeta("Joker", PillarType.FLAT_MULT, base_tier="C", base_mult=4, notes="+4 flat mult"),
    "Jolly Joker": JokerMeta("Jolly Joker", PillarType.FLAT_MULT, base_tier="B", base_mult=8, notes="+8 mult on Pair"),
    "Mad Joker": JokerMeta("Mad Joker", PillarType.FLAT_MULT, base_tier="B", base_mult=10, notes="+10 mult on Two Pair"),
    "Zany Joker": JokerMeta("Zany Joker", PillarType.FLAT_MULT, base_tier="B", base_mult=12, notes="+12 mult on Three of a Kind"),
    "Crazy Joker": JokerMeta("Crazy Joker", PillarType.FLAT_MULT, base_tier="B", base_mult=12, notes="+12 mult on Straight"),
    "Droll Joker": JokerMeta("Droll Joker", PillarType.FLAT_MULT, base_tier="B", base_mult=10, notes="+10 mult on Flush"),

    # --- Pillar: X_MULT ---
    "Cavendish": JokerMeta("Cavendish", PillarType.X_MULT, base_tier="S+", base_xmult=3.0, notes="x3 mult unconditionally"),
    "Card Sharp": JokerMeta("Card Sharp", PillarType.X_MULT, base_tier="S", base_xmult=3.0, notes="x3 mult if hand type already played this round"),
    "The Duo": JokerMeta("The Duo", PillarType.X_MULT, base_tier="S", base_xmult=2.0, notes="x2 mult on Pair"),
    "The Trio": JokerMeta("The Trio", PillarType.X_MULT, base_tier="S", base_xmult=3.0, notes="x3 mult on Three of a Kind"),
    "The Family": JokerMeta("The Family", PillarType.X_MULT, base_tier="S", base_xmult=4.0, notes="x4 mult on Four of a Kind"),
    "The Order": JokerMeta("The Order", PillarType.X_MULT, base_tier="S", base_xmult=3.0, notes="x3 mult on Straight"),
    "The Tribe": JokerMeta("The Tribe", PillarType.X_MULT, base_tier="S", base_xmult=2.0, notes="x2 mult on Flush"),
    "Constellation": JokerMeta("Constellation", PillarType.X_MULT, base_tier="S", base_xmult=2.2, scaling=True, notes="x0.1 mult per Planet card used"),
    "Hologram": JokerMeta("Hologram", PillarType.X_MULT, base_tier="S", base_xmult=2.5, scaling=True, notes="x0.25 mult per playing card added to deck"),
    "Steel Joker": JokerMeta("Steel Joker", PillarType.X_MULT, base_tier="A", base_xmult=2.0, scaling=True, notes="x0.2 mult per Steel Card in deck"),
    "Blackboard": JokerMeta("Blackboard", PillarType.X_MULT, base_tier="A", base_xmult=3.0, notes="x3 mult if all cards held in hand are spades or clubs"),
    "Baron": JokerMeta("Baron", PillarType.X_MULT, base_tier="S+", base_xmult=2.5, notes="Each held King gives x1.5 mult"),
    "Baseball Card": JokerMeta("Baseball Card", PillarType.X_MULT, base_tier="S", base_xmult=2.25, notes="Each Uncommon joker gives x1.5 mult"),
    "Campfire": JokerMeta("Campfire", PillarType.X_MULT, base_tier="S", base_xmult=2.5, scaling=True, notes="x0.25 mult per sold card, resets on boss"),
    "Vampire": JokerMeta("Vampire", PillarType.X_MULT, base_tier="S", base_xmult=2.5, scaling=True, notes="x0.1 mult per enhanced card played"),

    # --- Pillar: ECONOMY & THINNING ---
    "Golden Joker": JokerMeta("Golden Joker", PillarType.ECONOMY, base_tier="A", notes="+$4 at end of round"),
    "Mail-In Rebate": JokerMeta("Mail-In Rebate", PillarType.ECONOMY, base_tier="S", notes="+$5 per discarded designated rank"),
    "Trading Card": JokerMeta("Trading Card", PillarType.ECONOMY, base_tier="S+", notes="First discard with 1 card destroys it and gives +$3"),
    "To the Moon": JokerMeta("To the Moon", PillarType.ECONOMY, base_tier="S", notes="+$1 extra interest per $5 held (cap raises from $5 to $10)"),
    "Rocket": JokerMeta("Rocket", PillarType.ECONOMY, base_tier="S", scaling=True, notes="Earns escalating cash on defeating boss"),
    "Business Card": JokerMeta("Business Card", PillarType.ECONOMY, base_tier="A", notes="Played face cards have 1/2 chance to give $2"),
    "Vagabond": JokerMeta("Vagabond", PillarType.ECONOMY, base_tier="S", notes="Creates Tarot when playing hand with <= $4"),
    "Cloud 9": JokerMeta("Cloud 9", PillarType.ECONOMY, base_tier="B", notes="+$1 per 9 in deck at end of round"),

    # --- Pillar: UTILITY & COPY ---
    "Blueprint": JokerMeta("Blueprint", PillarType.UTILITY, base_tier="S+", notes="Copies ability of joker to the right"),
    "Brainstorm": JokerMeta("Brainstorm", PillarType.UTILITY, base_tier="S+", notes="Copies ability of leftmost joker"),
    "Hanging Chad": JokerMeta("Hanging Chad", PillarType.UTILITY, base_tier="S", notes="Retrigger first played card 2 times"),
    "Hack": JokerMeta("Hack", PillarType.UTILITY, base_tier="A", notes="Retrigger 2, 3, 4, 5 cards 1 time"),
    "Dusk": JokerMeta("Dusk", PillarType.UTILITY, base_tier="A", notes="Retrigger all cards in final hand of round"),
    "Four Fingers": JokerMeta("Four Fingers", PillarType.UTILITY, base_tier="B", notes="Straights and Flushes can be made with 4 cards"),
    "Shortcut": JokerMeta("Shortcut", PillarType.UTILITY, base_tier="A", notes="Allows Straights with gaps of 1"),
    "Mime": JokerMeta("Mime", PillarType.UTILITY, base_tier="S", notes="Retrigger all held-in-hand card abilities"),
}


@dataclass
class SynergyProfile:
    """Radar profile of current joker lineup across the four pillars."""
    chips_power: float = 0.0
    flat_mult_power: float = 0.0
    xmult_power: float = 1.0
    economy_power: float = 0.0
    utility_count: int = 0
    total_jokers: int = 0
    missing_pillars: List[PillarType] = field(default_factory=list)
    has_copy_joker: bool = False
    radar_scores: Dict[str, int] = field(default_factory=dict)
    summary_text: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "chips_power": round(self.chips_power, 1),
            "flat_mult_power": round(self.flat_mult_power, 1),
            "xmult_power": round(self.xmult_power, 2),
            "economy_power": round(self.economy_power, 1),
            "utility_count": self.utility_count,
            "total_jokers": self.total_jokers,
            "missing_pillars": [p.value for p in self.missing_pillars],
            "has_copy_joker": self.has_copy_joker,
            "radar_scores": self.radar_scores,
            "summary": self.summary_text,
        }


def analyze_synergy(jokers: List[Joker], ante: int = 1) -> SynergyProfile:
    """Computes build strength along the 4 pillars and diagnoses critical gaps."""
    chips_power = 0.0
    flat_mult_power = 0.0
    xmult_power = 1.0
    economy_power = 0.0
    utility_count = 0
    has_copy = False

    for j in (jokers or []):
        meta = JOKER_CATALOG.get(j.name)
        if j.name in ("Blueprint", "Brainstorm"):
            has_copy = True
            utility_count += 1

        if meta:
            if meta.primary_pillar == PillarType.CHIPS:
                chips_power += max(meta.base_chips, j.chips or 0)
            elif meta.primary_pillar == PillarType.FLAT_MULT:
                flat_mult_power += max(meta.base_mult, j.mult or 0)
            elif meta.primary_pillar == PillarType.X_MULT:
                val = max(meta.base_xmult, j.xmult or 1.0)
                xmult_power *= val
            elif meta.primary_pillar == PillarType.ECONOMY:
                economy_power += 25.0
            elif meta.primary_pillar == PillarType.UTILITY:
                utility_count += 1
        else:
            # Fallback for uncatalogued jokers using reported values
            if (j.chips or 0) > 0:
                chips_power += j.chips
            if (j.mult or 0) > 0:
                flat_mult_power += j.mult
            if (j.xmult or 1.0) > 1.0:
                xmult_power *= j.xmult

    # Identify missing pillars based on current Ante stage
    missing = []
    # Early game (Ante 1-2): Needs flat mult and chips to survive baseline rounds
    if chips_power < 30:
        missing.append(PillarType.CHIPS)
    if flat_mult_power < 12:
        missing.append(PillarType.FLAT_MULT)

    # Economy is essential before Ante 5 to build snowball interest
    if economy_power < 15 and ante <= 4:
        missing.append(PillarType.ECONOMY)

    # Mid to Late game (Ante 3+): X_Mult is mandatory as blind scores scale exponentially
    if xmult_power < 1.4 and ante >= 3:
        missing.append(PillarType.X_MULT)

    # Compute normalized radar index (0-100)
    c_idx = min(100, int((chips_power / 150.0) * 100))
    m_idx = min(100, int((flat_mult_power / 40.0) * 100))
    x_idx = min(100, int(((xmult_power - 1.0) / 3.0) * 100))
    e_idx = min(100, int((economy_power / 50.0) * 100))

    radar = {
        "chips": c_idx,
        "flat_mult": m_idx,
        "x_mult": x_idx,
        "economy": e_idx,
    }

    # Generate actionable diagnosis
    reasons = []
    if PillarType.CHIPS in missing:
        reasons.append("极度缺乏基础筹码 (Chips)")
    if PillarType.FLAT_MULT in missing:
        reasons.append("缺乏基础加法倍率 (Flat Mult)")
    if PillarType.X_MULT in missing:
        reasons.append("缺少乘法倍率 (X-Mult) 终结者")
    if PillarType.ECONOMY in missing:
        reasons.append("缺少经济滚雪球来源")

    summary = "四角大厦均衡" if not reasons else " | ".join(reasons)

    return SynergyProfile(
        chips_power=chips_power,
        flat_mult_power=flat_mult_power,
        xmult_power=xmult_power,
        economy_power=economy_power,
        utility_count=utility_count,
        total_jokers=len(jokers or []),
        missing_pillars=missing,
        has_copy_joker=has_copy,
        radar_scores=radar,
        summary_text=summary,
    )


@dataclass
class PurchaseAdvice:
    joker_name: str
    score: float
    recommendation: str  # "强烈推荐" | "值得考虑" | "暂不推荐"
    reason: str
    target_pillar: PillarType
    synergy_multiplier: float


def score_joker_candidate(
    card_name: str,
    profile: SynergyProfile,
    cost: int,
    current_money: int,
    has_slot: bool,
    ante: int = 1,
) -> PurchaseAdvice:
    """Evaluates whether to purchase a shop joker based on build deficiency and cost."""
    meta = JOKER_CATALOG.get(card_name)
    primary = meta.primary_pillar if meta else PillarType.UTILITY
    base_tier = meta.base_tier if meta else "B"

    # Base score by tier
    tier_scores = {"S+": 92, "S": 82, "A": 70, "B": 58, "C": 42}
    score = float(tier_scores.get(base_tier, 55))

    synergy_mult = 1.0
    reasons = []

    # 1. Synergy Bonus: Does this joker fix our most critical missing pillar?
    if primary in profile.missing_pillars:
        if primary == PillarType.CHIPS:
            synergy_mult = 2.2 if profile.chips_power == 0 else 1.6
            reasons.append(f"完美补充当前阵容最急缺的基础筹码 (+{meta.base_chips if meta else 50} Chips)")
        elif primary == PillarType.FLAT_MULT:
            synergy_mult = 2.4 if profile.flat_mult_power == 0 else 1.8
            reasons.append(f"补强关键基础倍率，激活后续 X-Mult 收益")
        elif primary == PillarType.X_MULT:
            # X-Mult is only god-tier if we already have base chips and mult
            if profile.flat_mult_power >= 15:
                synergy_mult = 2.5
                reasons.append(f"已有充沛基础倍率，引入乘法倍率 (x{meta.base_xmult if meta else 2.0}) 呈指数爆发")
            else:
                synergy_mult = 1.2
                reasons.append("具有乘法潜力，但当前基础倍率尚显薄弱")
        elif primary == PillarType.ECONOMY:
            synergy_mult = 1.9 if ante <= 3 else 1.2
            reasons.append("前期黄金经济小丑，快速积累本金滚 $25 满利息")

    # Diminishing returns: if we already have massive flat mult, don't stack mediocre flat mult
    if primary == PillarType.FLAT_MULT and profile.flat_mult_power >= 35:
        synergy_mult *= 0.65
        reasons.append("当前基础倍率已饱和，边际收益递减")

    score *= synergy_mult

    # 2. Universal S+ god jokers (Blueprint, Brainstorm, Cavendish, Trading Card)
    if card_name in ("Blueprint", "Brainstorm", "Cavendish", "Trading Card"):
        score = max(score, 95.0)
        reasons.append("顶尖神级小丑，任何流派无脑必拿")

    # 3. Affordability and Slot constraints
    can_afford = current_money >= cost
    if not can_afford:
        score -= 45.0
        reasons.append(f"资金不足 (${current_money} < ${cost})")
    elif not has_slot:
        score -= 25.0
        reasons.append("小丑槽已满 (需考虑是否变卖劣质小丑置换)")

    final_score = round(score, 1)
    if final_score >= 80 and can_afford:
        rec = "强烈推荐"
    elif final_score >= 62 and can_afford:
        rec = "值得考虑"
    else:
        rec = "暂不推荐"

    reason_str = " | ".join(reasons) if reasons else "普通补充小丑"
    return PurchaseAdvice(
        joker_name=card_name,
        score=final_score,
        recommendation=rec,
        reason=reason_str,
        target_pillar=primary,
        synergy_multiplier=round(synergy_mult, 2),
    )
