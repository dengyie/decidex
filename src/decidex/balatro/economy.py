"""
Economy Engine: The $25 Interest Rule and Dynamic Budget Guard.

In high-difficulty Balatro, money is health. Every $5 held earns $1 guaranteed interest
at the end of each round (capped at $25 for $5/round, or $50 for $10 with To the Moon).
Early gold compounding provides the fuel to reroll, buy celestial packs, and adapt.

This module enforces:
  1. Interest Tier Protection ($5, $10, $15, $20, $25 thresholds).
  2. Interest Opportunity Cost: Calculates cash flow loss from dropping a tier.
  3. Budget Clearance Tiers:
     - EMERGENCY: Imminent defeat allows bankruptcy spending.
     - S_TIER_OVERRIDE: God-tier items (Blueprint, Cavendish, Trading Card) bypass interest locks.
     - SURPLUS_ONLY: Standard A/B jokers, packs, and rerolls must use surplus above the interest line.
     - FORBIDDEN: Prevents squandering interest on mediocre items or blind rerolls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class BudgetTier(str, Enum):
    EMERGENCY = "emergency"              # Blind unwinnable, spend everything to survive
    S_TIER_EXEMPT = "s_tier_exempt"      # Transcendent item, worth breaking interest
    SURPLUS_ALLOWED = "surplus_allowed"  # Safe to buy using surplus money above interest threshold
    BREAKS_INTEREST = "breaks_interest"  # Drops interest tier; rejected unless vital
    UNEVEN_SPEND = "uneven_spend"        # Wasteful spend (e.g. blind reroll below $25)


@dataclass
class EconomyStatus:
    money: int
    interest_cap: int = 25
    current_interest: int = 0
    max_interest: int = 5
    next_threshold: int = 5
    to_next_threshold: int = 0
    surplus_above_cap: int = 0
    safe_spend_budget: int = 0
    summary: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "money": self.money,
            "current_interest": self.current_interest,
            "max_interest": self.max_interest,
            "next_threshold": self.next_threshold,
            "to_next_threshold": self.to_next_threshold,
            "surplus_above_cap": self.surplus_above_cap,
            "safe_spend_budget": self.safe_spend_budget,
            "summary": self.summary,
        }


def calculate_economy_status(
    money: int,
    ante: int = 1,
    has_to_the_moon: bool = False,
) -> EconomyStatus:
    """Calculates current interest tier, distance to next step, and available safe budget."""
    cap = 50 if has_to_the_moon else 25
    max_interest = 10 if has_to_the_moon else 5

    capped_money = min(money, cap)
    current_interest = min(max_interest, max(0, capped_money // 5))

    if money >= cap:
        next_thresh = cap
        to_next = 0
        surplus_above_cap = money - cap
        safe_budget = surplus_above_cap
        summary = f"已达 ${cap} 顶格利息 (每轮稳拿 ${max_interest} 利息)，盈余 ${surplus_above_cap} 可自由支配"
    else:
        current_tier_base = current_interest * 5
        next_thresh = current_tier_base + 5
        to_next = max(0, next_thresh - money)
        surplus_above_cap = 0
        # Safe budget within the current tier without dropping the current $1 step
        safe_budget = max(0, money - current_tier_base)
        summary = f"资金 ${money}，当前利息 ${current_interest}；差 ${to_next} 达 ${next_thresh} (下阶利息)"

    return EconomyStatus(
        money=money,
        interest_cap=cap,
        current_interest=current_interest,
        max_interest=max_interest,
        next_threshold=next_thresh,
        to_next_threshold=to_next,
        surplus_above_cap=surplus_above_cap,
        safe_spend_budget=safe_budget,
        summary=summary,
    )


@dataclass
class BudgetVerdict:
    allowed: bool
    budget_tier: BudgetTier
    interest_cost_per_round: int
    projected_money: int
    projected_interest: int
    verdict_title: str
    reason: str


def evaluate_purchase_budget(
    cost: int,
    item_type: str,     # "joker" | "pack" | "voucher" | "reroll"
    item_tier: str,     # "S+", "S", "A", "B", "C"
    econ: EconomyStatus,
    is_emergency: bool = False,
    is_critical_synergy_fix: bool = False,
) -> BudgetVerdict:
    """
    Evaluates whether a purchase is financially sound based on interest impact.
    """
    if cost > econ.money:
        return BudgetVerdict(
            allowed=False,
            budget_tier=BudgetTier.BREAKS_INTEREST,
            interest_cost_per_round=0,
            projected_money=econ.money,
            projected_interest=econ.current_interest,
            verdict_title="资金不足",
            reason=f"商品价格 (${cost}) 超出当前总资金 (${econ.money})",
        )

    projected_money = econ.money - cost
    projected_interest = min(econ.max_interest, max(0, min(projected_money, econ.interest_cap) // 5))
    interest_loss = max(0, econ.current_interest - projected_interest)

    # 1. Emergency Override: If we will die without this purchase, spend everything
    if is_emergency:
        return BudgetVerdict(
            allowed=True,
            budget_tier=BudgetTier.EMERGENCY,
            interest_cost_per_round=interest_loss,
            projected_money=projected_money,
            projected_interest=projected_interest,
            verdict_title="破产求生 (豁免利息)",
            reason="当前局势面临暴毙威胁，启动紧急避险预算全额消费保命",
        )

    # 2. S-Tier / Transcendent Override: God-tier items (Blueprint, Cavendish, etc.)
    if item_tier in ("S+", "S"):
        return BudgetVerdict(
            allowed=True,
            budget_tier=BudgetTier.S_TIER_EXEMPT,
            interest_cost_per_round=interest_loss,
            projected_money=projected_money,
            projected_interest=projected_interest,
            verdict_title="神级小丑 (特权放行)",
            reason=f"顶尖 {item_tier} 级战略小丑，即使损失 ${interest_loss}/轮 利息也是极大正收益",
        )

    # 3. Critical Pillar Deficiency Fix (Fixing a 0-chips or 0-mult crisis)
    if is_critical_synergy_fix and interest_loss <= 1:
        return BudgetVerdict(
            allowed=True,
            budget_tier=BudgetTier.S_TIER_EXEMPT,
            interest_cost_per_round=interest_loss,
            projected_money=projected_money,
            projected_interest=projected_interest,
            verdict_title="短板补强 (允许小幅破阶)",
            reason=f"修复当前阵容极度缺失的核心短板，允许单级利息损失 (-${interest_loss}/轮)",
        )

    # 4. Safe Surplus Spend: Cost does not drop the current interest tier
    if interest_loss == 0:
        return BudgetVerdict(
            allowed=True,
            budget_tier=BudgetTier.SURPLUS_ALLOWED,
            interest_cost_per_round=0,
            projected_money=projected_money,
            projected_interest=projected_interest,
            verdict_title="安全消费 (利息保全)",
            reason=f"消费后保留 ${projected_money}，完美保全每轮 ${econ.current_interest} 利息",
        )

    # 5. Reroll check: Blind rerolls that drop interest tiers are strictly forbidden
    if item_type == "reroll":
        return BudgetVerdict(
            allowed=False,
            budget_tier=BudgetTier.UNEVEN_SPEND,
            interest_cost_per_round=interest_loss,
            projected_money=projected_money,
            projected_interest=projected_interest,
            verdict_title="禁止刷新",
            reason=f"刷新商店将导致跌破利息线 (-${interest_loss}/轮)，本金不足严禁盲刷",
        )

    # 6. Default protection: Breaking an interest tier for a mediocre card is rejected
    return BudgetVerdict(
        allowed=False,
        budget_tier=BudgetTier.BREAKS_INTEREST,
        interest_cost_per_round=interest_loss,
        projected_money=projected_money,
        projected_interest=projected_interest,
        verdict_title="利息保护拦截",
        reason=f"购买该商品将导致利息挡位跌落 (-${interest_loss}/轮 利息)，非核心小丑不值得破产",
    )
