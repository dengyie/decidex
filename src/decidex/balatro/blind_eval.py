"""
Blind Selection and Tag Skipping Evaluation Engine for Balatro DecideX.

Models the mathematical expected value (EV) tradeoff between:
  1. Selecting the blind (earning prize money, hands left bonus, interest, and visiting the Shop).
  2. Skipping the blind (earning the Tag reward, but forfeiting round cash and shop visit).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from .state import BalatroState


@dataclass
class TagEvaluation:
    """Canonical evaluation profile for a Balatro Tag."""
    key: str
    name: str
    base_ev: float
    tier: str  # S, A, B, C, D
    category: str  # joker, pack, economy, voucher, utility
    description: str


TAG_CATALOG: Dict[str, TagEvaluation] = {
    "tag_negative": TagEvaluation(
        key="tag_negative",
        name="Negative Tag",
        base_ev=9.5,
        tier="S",
        category="joker",
        description="下一个商店小丑额外提供 +1 小丑槽位 (负片神级效果)",
    ),
    "tag_polychrome": TagEvaluation(
        key="tag_polychrome",
        name="Polychrome Tag",
        base_ev=8.2,
        tier="S",
        category="joker",
        description="下一个商店版本小丑免费且自带多彩属性 (x1.5 Mult 质变)",
    ),
    "tag_investment": TagEvaluation(
        key="tag_investment",
        name="Investment Tag",
        base_ev=8.0,
        tier="S",
        category="economy",
        description="击败下一个 Boss Blind 获得 $25 巨额奖金",
    ),
    "tag_ethereal": TagEvaluation(
        key="tag_ethereal",
        name="Ethereal Tag",
        base_ev=7.5,
        tier="A",
        category="pack",
        description="免费获得 1 个幻灵牌包 (概率产出黑洞、烧牌或复制)",
    ),
    "tag_economy": TagEvaluation(
        key="tag_economy",
        name="Economy Tag",
        base_ev=7.2,
        tier="A",
        category="economy",
        description="当前资金直接翻倍 (上限增加 $40)",
    ),
    "tag_rare": TagEvaluation(
        key="tag_rare",
        name="Rare Tag",
        base_ev=7.0,
        tier="A",
        category="joker",
        description="下一个商店免费获得 1 张 Rare 稀有小丑",
    ),
    "tag_coupon": TagEvaluation(
        key="tag_coupon",
        name="Coupon Tag",
        base_ev=6.8,
        tier="A",
        category="economy",
        description="下一个商店初始卡牌与补充包全部 $0 免费",
    ),
    "tag_holo": TagEvaluation(
        key="tag_holo",
        name="Holographic Tag",
        base_ev=6.8,
        tier="A",
        category="joker",
        description="下一个商店版本小丑免费且自带全息属性 (+10 Mult)",
    ),
    "tag_buffoon": TagEvaluation(
        key="tag_buffoon",
        name="Buffoon Tag",
        base_ev=6.5,
        tier="B",
        category="pack",
        description="免费获得 1 个大型小丑包 (Mega Buffoon Pack)",
    ),
    "tag_voucher": TagEvaluation(
        key="tag_voucher",
        name="Voucher Tag",
        base_ev=6.5,
        tier="B",
        category="voucher",
        description="下一个商店额外增加 1 张优惠券",
    ),
    "tag_foil": TagEvaluation(
        key="tag_foil",
        name="Foil Tag",
        base_ev=6.2,
        tier="B",
        category="joker",
        description="下一个商店版本小丑免费且自带闪箔属性 (+50 Chips)",
    ),
    "tag_charm": TagEvaluation(
        key="tag_charm",
        name="Charm Tag",
        base_ev=6.2,
        tier="B",
        category="pack",
        description="免费获得 1 个大型奥秘包 (Mega Arcana Pack)",
    ),
    "tag_meteor": TagEvaluation(
        key="tag_meteor",
        name="Meteor Tag",
        base_ev=6.0,
        tier="B",
        category="pack",
        description="免费获得 1 个大型天体包 (Mega Celestial Pack)",
    ),
    "tag_uncommon": TagEvaluation(
        key="tag_uncommon",
        name="Uncommon Tag",
        base_ev=5.5,
        tier="B",
        category="joker",
        description="下一个商店免费获得 1 张 Uncommon 罕见小丑",
    ),
    "tag_double": TagEvaluation(
        key="tag_double",
        name="Double Tag",
        base_ev=5.5,
        tier="B",
        category="utility",
        description="复制下一个获取的 Tag 效果",
    ),
    "tag_orbital": TagEvaluation(
        key="tag_orbital",
        name="Orbital Tag",
        base_ev=5.2,
        tier="B",
        category="utility",
        description="使指定牌型等级永久提升 3 级",
    ),
    "tag_standard": TagEvaluation(
        key="tag_standard",
        name="Standard Tag",
        base_ev=5.0,
        tier="C",
        category="pack",
        description="免费获得 1 个大型标准包 (Mega Standard Pack)",
    ),
    "tag_d_six": TagEvaluation(
        key="tag_d_six",
        name="D6 Tag",
        base_ev=5.0,
        tier="C",
        category="utility",
        description="下一个商店初始刷新费用为 $0",
    ),
    "tag_boss": TagEvaluation(
        key="tag_boss",
        name="Boss Tag",
        base_ev=4.5,
        tier="C",
        category="utility",
        description="重置并重新随机下一个 Boss Blind",
    ),
    "tag_speed": TagEvaluation(
        key="tag_speed",
        name="Speed Tag",
        base_ev=3.5,
        tier="D",
        category="economy",
        description="本局每跳过一次盲注获得 $5",
    ),
    "tag_handy": TagEvaluation(
        key="tag_handy",
        name="Handy Tag",
        base_ev=3.5,
        tier="D",
        category="economy",
        description="本局每打出过一次手牌获得 $1",
    ),
    "tag_garbage": TagEvaluation(
        key="tag_garbage",
        name="Garbage Tag",
        base_ev=3.5,
        tier="D",
        category="economy",
        description="本局每次未使用的弃牌获得 $1",
    ),
    "tag_top_up": TagEvaluation(
        key="tag_top_up",
        name="Top-up Tag",
        base_ev=3.2,
        tier="D",
        category="joker",
        description="最多随机生成 2 张 Common 基础小丑 (需空槽位)",
    ),
    "tag_juggle": TagEvaluation(
        key="tag_juggle",
        name="Juggle Tag",
        base_ev=3.0,
        tier="D",
        category="utility",
        description="下一回合手牌上限临时 +3",
    ),
}

# Normalization map for tag keys
TAG_NAME_NORM: Dict[str, str] = {
    "negative": "tag_negative", "negative tag": "tag_negative", "tag_negative": "tag_negative",
    "polychrome": "tag_polychrome", "polychrome tag": "tag_polychrome", "tag_polychrome": "tag_polychrome",
    "investment": "tag_investment", "investment tag": "tag_investment", "tag_investment": "tag_investment",
    "ethereal": "tag_ethereal", "ethereal tag": "tag_ethereal", "tag_ethereal": "tag_ethereal",
    "economy": "tag_economy", "economy tag": "tag_economy", "tag_economy": "tag_economy",
    "rare": "tag_rare", "rare tag": "tag_rare", "tag_rare": "tag_rare",
    "coupon": "tag_coupon", "coupon tag": "tag_coupon", "tag_coupon": "tag_coupon",
    "holographic": "tag_holo", "holo tag": "tag_holo", "tag_holo": "tag_holo",
    "buffoon": "tag_buffoon", "buffoon tag": "tag_buffoon", "tag_buffoon": "tag_buffoon",
    "voucher": "tag_voucher", "voucher tag": "tag_voucher", "tag_voucher": "tag_voucher",
    "foil": "tag_foil", "foil tag": "tag_foil", "tag_foil": "tag_foil",
    "charm": "tag_charm", "charm tag": "tag_charm", "tag_charm": "tag_charm",
    "meteor": "tag_meteor", "meteor tag": "tag_meteor", "tag_meteor": "tag_meteor",
    "uncommon": "tag_uncommon", "uncommon tag": "tag_uncommon", "tag_uncommon": "tag_uncommon",
    "double": "tag_double", "double tag": "tag_double", "tag_double": "tag_double",
    "orbital": "tag_orbital", "orbital tag": "tag_orbital", "tag_orbital": "tag_orbital",
    "standard": "tag_standard", "standard tag": "tag_standard", "tag_standard": "tag_standard",
    "d_six": "tag_d_six", "d6 tag": "tag_d_six", "tag_d_six": "tag_d_six",
    "boss": "tag_boss", "boss tag": "tag_boss", "tag_boss": "tag_boss",
    "speed": "tag_speed", "speed tag": "tag_speed", "tag_speed": "tag_speed",
    "handy": "tag_handy", "handy tag": "tag_handy", "tag_handy": "tag_handy",
    "garbage": "tag_garbage", "garbage tag": "tag_garbage", "tag_garbage": "tag_garbage",
    "top_up": "tag_top_up", "top-up tag": "tag_top_up", "tag_top_up": "tag_top_up",
    "juggle": "tag_juggle", "juggle tag": "tag_juggle", "tag_juggle": "tag_juggle",
}


@dataclass
class BlindChoiceEvaluation:
    """Comprehensive evaluation of whether to select or skip the current blind."""
    blind_name: str
    blind_type: str
    chips_required: int
    select_blind_ev: float
    skip_blind_ev: float
    recommended_action: str  # "select_blind" | "skip_blind"
    can_skip: bool
    tag_key: Optional[str] = None
    tag_name: Optional[str] = None
    tag_tier: Optional[str] = None
    reason: str = ""
    risk_warning: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "blind_name": self.blind_name,
            "blind_type": self.blind_type,
            "chips_required": self.chips_required,
            "select_blind_ev": round(self.select_blind_ev, 4),
            "skip_blind_ev": round(self.skip_blind_ev, 4),
            "recommended_action": self.recommended_action,
            "can_skip": self.can_skip,
            "tag_key": self.tag_key,
            "tag_name": self.tag_name,
            "tag_tier": self.tag_tier,
            "reason": self.reason,
            "risk_warning": self.risk_warning,
        }


def evaluate_blind_choice(state: BalatroState) -> BlindChoiceEvaluation:
    """
    Evaluate whether to Select or Skip the current blind.
    Computes rigorous EV incorporating prize money, interest, shop access value,
    and tag utility balanced against early-game survival constraints.
    """
    b = getattr(state, "blind", None)
    blind_name = getattr(b, "name", "Small Blind") or "Small Blind"
    raw_type = getattr(b, "type", None) or "small"
    blind_type = str(raw_type).lower()
    chips_req = getattr(b, "chips", 300) or 300
    ante = getattr(state, "ante", 1)
    money = getattr(state, "money", 0)
    jokers = getattr(state, "jokers", [])
    j_count = len(jokers)

    # Boss Blinds cannot be skipped in Balatro
    if blind_type == "boss" or "boss" in blind_name.lower():
        return BlindChoiceEvaluation(
            blind_name=blind_name,
            blind_type="boss",
            chips_required=chips_req,
            select_blind_ev=8.0,
            skip_blind_ev=0.0,
            recommended_action="select_blind",
            can_skip=False,
            reason="Boss Blind 无法跳过，必须进入出牌阶段斩杀",
        )

    # 1. Base EV for selecting and playing the blind
    # Rewards: Round payout ($3 small, $4 big), hands left cash (~$1-2), interest (~$0-5),
    # PLUS the immense value of Shop Access (new jokers, boosters, vouchers, rerolls).
    base_select_ev = 6.4
    if blind_type == "big":
        base_select_ev += 0.3
    # Interest component
    interest_units = min(5, money // 5)
    base_select_ev += interest_units * 0.12

    # Shop dependency factor
    if ante == 1:
        base_select_ev += 0.6  # Ante 1 shops are vital to find first jokers
    if j_count < 2:
        base_select_ev += 0.8  # Desperately need shop to buy foundation jokers
    elif j_count >= 4:
        base_select_ev -= 0.2  # Established squad, slightly less dependent on single shop

    select_ev = round(min(8.5, max(5.0, base_select_ev)), 4)

    # 2. Base EV for skipping the blind
    tag_data = getattr(state, "blind_tag", None) or {}
    raw_key = tag_data.get("key") or tag_data.get("name") or ""
    norm_key = TAG_NAME_NORM.get(str(raw_key).lower(), str(raw_key))
    tag_eval = TAG_CATALOG.get(norm_key)

    if not tag_eval:
        # Fallback evaluation for unspecified or generic tag
        tag_key = "tag_unknown"
        tag_name = tag_data.get("name") or "Unknown Tag"
        tag_base_ev = 4.8
        tag_tier = "C"
        tag_desc = "普通奖励 Tag"
    else:
        tag_key = tag_eval.key
        tag_name = tag_eval.name
        tag_base_ev = tag_eval.base_ev
        tag_tier = tag_eval.tier
        tag_desc = tag_eval.description

    skip_ev = tag_base_ev
    risk_warning: Optional[str] = None

    # Dynamic scaling for specific tag mechanics
    if tag_key == "tag_economy":
        if money >= 20:
            skip_ev += 1.5  # Huge payout (+$20 - +$40)
        elif money <= 6:
            skip_ev -= 3.2  # Payout too low (+$4) to justify skipping a shop

    if tag_key == "tag_investment":
        if j_count >= 2:
            skip_ev += 0.8  # Highly likely to clear upcoming boss and claim $25
        elif j_count == 0:
            skip_ev -= 1.8  # Risky to promise boss clear without jokers

    # --- CRITICAL SURVIVAL GATING ---
    # Trap 1: Ante 1 Big Blind with 0 Jokers -> straight to Boss Blind!
    if ante == 1 and blind_type == "big" and j_count == 0:
        skip_ev -= 4.5
        risk_warning = "⚠️ 严禁在 Ante 1 零小丑时跳过大盲注，直面 Boss 极大概率暴毙！"

    # Trap 2: Ante 1 Small Blind with 0 Jokers -> only accept God-tier tags (S-tier)
    elif ante == 1 and blind_type == "small" and j_count == 0:
        if tag_tier != "S":
            skip_ev -= 2.2
            risk_warning = "Ante 1 首轮盲注缺少小丑基础，非 S 级神级 Tag 建议进入商店采购"

    # Trap 3: Zero jokers in later Antes
    elif j_count == 0 and ante > 1:
        skip_ev -= 2.8
        risk_warning = "场上暂无小丑战力，急需进入商店构建阵容"

    # Buff: Strong established squad safe to take high-value tags
    elif j_count >= 3 and tag_tier in ("S", "A"):
        skip_ev += 0.5

    skip_ev = round(max(1.0, skip_ev), 4)

    # Decision
    if skip_ev > select_ev:
        recommended = "skip_blind"
        reason = f"Tag [{tag_name}] ({tag_tier}级) 期望收益 ({skip_ev:.2f}) 优于进入盲注 ({select_ev:.2f}): {tag_desc}"
    else:
        recommended = "select_blind"
        if risk_warning:
            reason = f"优先进入盲注 ({select_ev:.2f}) 以保全战力，避免盲目跳过: {risk_warning}"
        else:
            reason = f"进入盲注 ({select_ev:.2f}) 稳健获取奖金与商店发展机会，Tag [{tag_name}] 价值 ({skip_ev:.2f}) 不足以弥补跳过商店损失"

    return BlindChoiceEvaluation(
        blind_name=blind_name,
        blind_type=blind_type,
        chips_required=chips_req,
        select_blind_ev=select_ev,
        skip_blind_ev=skip_ev,
        recommended_action=recommended,
        can_skip=True,
        tag_key=tag_key,
        tag_name=tag_name,
        tag_tier=tag_tier,
        reason=reason,
        risk_warning=risk_warning,
    )
