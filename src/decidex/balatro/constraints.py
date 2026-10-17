"""
Constraints and Boss Defense Engine.

Certain jokers and boss blinds impose lethal conditions that can immediately ruin a run:
  1. Ride the Bus: Playing a scoring face card (J, Q, K) resets mult to 0!
  2. Green Joker: Discarding loses -1 mult, playing gains +1 mult. Discards are toxic.
  3. Square Joker: Playing exactly 4 cards permanently gains +4 chips.
  4. Banner: Retaining discards yields +30 chips each; squandering discards costs score.
  5. Boss Blinds Matrix:
     - The Psychic: Must play exactly 5 cards; < 5 cards scores 0! (Requires junk card attachment)
     - The Eye: Cannot repeat the same poker hand type this round.
     - The Mouth: Only 1 poker hand type permitted all round.
     - The Water: 0 discards allowed. (Requires dumping junk with played hands)
     - The Needle: Exactly 1 hand allowed. Must burn all discards to build a 1-shot kill.
     - The Ox: Playing most played hand sets money to $0!
     - The Flint: Base chips and mult are halved.
     - The Plant: All face cards (J, Q, K) are debuffed.
     - Suit Debuffers (The Club, The Goad, The Window, The Head): Debuffed suits score 0 and trigger nothing.
     - The Tooth: Lose $1 per card played. (Discourages dumping junk cards)
     - The Arm: Decrease level of played poker hand by 1.
     - The Pillar: Cards played previously this Ante are debuffed.
     - Finisher Bosses: Verdant Leaf, Amber Acorn, Violet Vessel, Crimson Heart, Cerulean Bell.

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
class BossRule:
    """Explicit domain rules and strategic guidance for a specific Balatro Boss Blind."""

    name: str
    category: str  # "hand_constraint", "hand_type_restriction", "debuff", "economic_penalty", "resource_cap", "finisher"
    description: str
    strategic_directive: str
    requires_exact_5_cards: bool = False  # The Psychic
    no_repeat_hand_type: bool = False  # The Eye
    single_hand_type_only: bool = False  # The Mouth
    zero_discards: bool = False  # The Water
    single_hand_only: bool = False  # The Needle
    debuffed_suit: Optional[str] = None  # The Club ("C"), The Goad ("S"), etc.
    debuff_face_cards: bool = False  # The Plant
    wipes_money_on_most_played: bool = False  # The Ox
    halves_base_chips_mult: bool = False  # The Flint
    costs_money_per_card: int = 0  # The Tooth ($1 per card)
    decreases_hand_level: bool = False  # The Arm
    cards_played_debuffed: bool = False  # The Pillar
    hand_size_penalty: int = 0  # The Manacle (-1 hand size)
    draw_fixed_count: int = 0  # The Serpent (always draw 3)
    finisher_boss: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "category": self.category,
            "description": self.description,
            "strategic_directive": self.strategic_directive,
            "requires_exact_5_cards": self.requires_exact_5_cards,
            "no_repeat_hand_type": self.no_repeat_hand_type,
            "single_hand_type_only": self.single_hand_type_only,
            "zero_discards": self.zero_discards,
            "single_hand_only": self.single_hand_only,
            "debuffed_suit": self.debuffed_suit,
            "debuff_face_cards": self.debuff_face_cards,
            "wipes_money_on_most_played": self.wipes_money_on_most_played,
            "halves_base_chips_mult": self.halves_base_chips_mult,
            "costs_money_per_card": self.costs_money_per_card,
            "decreases_hand_level": self.decreases_hand_level,
            "finisher_boss": self.finisher_boss,
        }


# Comprehensive Balatro Boss Blind Catalog
BOSS_CATALOG: Dict[str, BossRule] = {
    "The Psychic": BossRule(
        name="The Psychic",
        category="hand_constraint",
        description="必须打出 5 张牌 (少于 5 张牌打出不计分/0分)",
        strategic_directive="打出任何牌型（包括对子、高牌、三条等）都必须附带低价值杂牌补足 5 张牌！严禁打出少于 5 张牌！",
        requires_exact_5_cards=True,
    ),
    "The Eye": BossRule(
        name="The Eye",
        category="hand_type_restriction",
        description="本回合不可重复打出相同牌型",
        strategic_directive="本回合严禁打出已打过的牌型！首发打出主力牌型后，后续出牌必须规划不同牌型（如两对、同花、顺子）补充伤害。",
        no_repeat_hand_type=True,
    ),
    "The Mouth": BossRule(
        name="The Mouth",
        category="hand_type_restriction",
        description="本回合只能打出单一一种牌型",
        strategic_directive="第一手打出的牌型将锁定为本局唯一允许牌型！必须确保第一手出牌就是你牌库最强、最容易连续凑齐的核心牌型。",
        single_hand_type_only=True,
    ),
    "The Water": BossRule(
        name="The Water",
        category="resource_cap",
        description="开局 0 次弃牌",
        strategic_directive="全回合无弃牌机会！出牌时务必附带 1~3 张不需要的废牌进行手牌过滤（伪弃牌），快速重置手牌。",
        zero_discards=True,
    ),
    "The Needle": BossRule(
        name="The Needle",
        category="resource_cap",
        description="本回合仅有 1 次出牌机会",
        strategic_directive="仅有 1 次出牌机会！在打出之前必须用尽所有弃牌次数，全力凑出能一击必杀的最大爆发牌型！",
        single_hand_only=True,
    ),
    "The Flint": BossRule(
        name="The Flint",
        category="debuff",
        description="基础筹码与基础倍率减半",
        strategic_directive="基础点数减半，过度依赖牌型基础分的打法受重挫；必须依靠小丑的 +Chips、+Mult 与 X-Mult 强力补正。",
        halves_base_chips_mult=True,
    ),
    "The Ox": BossRule(
        name="The Ox",
        category="economic_penalty",
        description="打出本局最常用的牌型将使金币清零 ($0)",
        strategic_directive="严禁打出本局最常打的主力牌型！除非能一击通关且盲注奖励足以弥补，否则应改打次选牌型以保住利息本金。",
        wipes_money_on_most_played=True,
    ),
    "The Plant": BossRule(
        name="The Plant",
        category="debuff",
        description="所有人头牌 (J, Q, K) 被封印削弱",
        strategic_directive="人头牌失效且不触发点数与人头小丑加成（如 Scary Face、Photographer），应转为打数字牌（A, 10, 9等）。",
        debuff_face_cards=True,
    ),
    "The Club": BossRule(
        name="The Club",
        category="debuff",
        description="所有梅花 (♣) 牌被封印削弱",
        strategic_directive="梅花牌不计筹码且不触发同花加成，避免组梅花同花；可将无用梅花牌作为出牌杂牌垫掉。",
        debuffed_suit="C",
    ),
    "The Goad": BossRule(
        name="The Goad",
        category="debuff",
        description="所有黑桃 (♠) 牌被封印削弱",
        strategic_directive="黑桃牌不计筹码且不触发同花加成，避免组黑桃同花；可将黑桃牌作为弃牌或杂牌垫掉。",
        debuffed_suit="S",
    ),
    "The Window": BossRule(
        name="The Window",
        category="debuff",
        description="所有方块 (♦) 牌被封印削弱",
        strategic_directive="方块牌不计筹码且不触发同花加成，避免组方块同花；可将方块牌作为弃牌或杂牌垫掉。",
        debuffed_suit="D",
    ),
    "The Head": BossRule(
        name="The Head",
        category="debuff",
        description="所有红桃 (♥) 牌被封印削弱",
        strategic_directive="红桃牌不计筹码且不触发同花加成，避免组红桃同花；可将红桃牌作为弃牌或杂牌垫掉。",
        debuffed_suit="H",
    ),
    "The Tooth": BossRule(
        name="The Tooth",
        category="economic_penalty",
        description="每打出一张牌损失 $1",
        strategic_directive="严禁出牌带无用杂牌！尽量以 1~2 张高爆发卡牌极简击杀，保护经济资金。",
        costs_money_per_card=1,
    ),
    "The Arm": BossRule(
        name="The Arm",
        category="debuff",
        description="打出的牌型等级降低 1 级",
        strategic_directive="打出牌型会掉级！优先使用未升级过或不依赖等级的牌型，避免高等级主力牌型被连续降级。",
        decreases_hand_level=True,
    ),
    "The Pillar": BossRule(
        name="The Pillar",
        category="debuff",
        description="本 Ante 之前打过的牌被封印削弱",
        strategic_directive="避开前几局已打出的旧牌，优先使用未曾出过的新牌。",
        cards_played_debuffed=True,
    ),
    "The Manacle": BossRule(
        name="The Manacle",
        category="hand_constraint",
        description="手牌上限减少 1 张",
        strategic_directive="手牌池变小，组大牌型难度上升，优先依赖小丑战力打简单牌型。",
        hand_size_penalty=1,
    ),
    "The Serpent": BossRule(
        name="The Serpent",
        category="hand_constraint",
        description="出牌或弃牌后总是固定抽取 3 张牌",
        strategic_directive="不受手牌上限限制！可以通过积极出牌和弃牌让手牌数量滚雪球，积累大量手牌后一波爆发。",
        draw_fixed_count=3,
    ),
    "Verdant Leaf": BossRule(
        name="Verdant Leaf",
        category="finisher",
        description="所有卡牌被削弱，直到卖出一张小丑",
        strategic_directive="决战 Boss：若战力足够可强行斩杀，否则必须卖掉贡献最低或最容易替代的小丑解除封印。",
        finisher_boss=True,
    ),
    "Violet Vessel": BossRule(
        name="Violet Vessel",
        category="finisher",
        description="超巨型盲注 (目标筹码变为通常的 3 倍)",
        strategic_directive="纯粹的极端数值检验！必须调整小丑最佳从左到右排序（Flat Mult 垫底，X-Mult 连续翻倍），打出最大乘算。",
        finisher_boss=True,
    ),
    "Crimson Heart": BossRule(
        name="Crimson Heart",
        category="finisher",
        description="每手牌随机失效一张小丑",
        strategic_directive="每回合核心小丑可能轮流熄火，优先确保备用倍率小丑与筹码小丑协同，提高容错率。",
        finisher_boss=True,
    ),
    "Amber Acorn": BossRule(
        name="Amber Acorn",
        category="finisher",
        description="打乱小丑顺序并全部翻面面朝下",
        strategic_directive="根据进战前记忆的小丑效果与静态加成重新梳理位置，尽量将 X-Mult 挪回右侧。",
        finisher_boss=True,
    ),
    "Cerulean Bell": BossRule(
        name="Cerulean Bell",
        category="finisher",
        description="强制固定选中手牌中 1 张特定卡牌",
        strategic_directive="必须以被强制锁定的卡牌为基础构建牌型，或将其作为 5 张牌中的杂牌带出消耗掉。",
        finisher_boss=True,
    ),
}


@dataclass
class ActiveConstraints:
    # Joker constraints
    has_ride_the_bus: bool = False
    has_green_joker: bool = False
    has_square_joker: bool = False
    has_banner: bool = False

    # Boss Blind constraints
    boss_name: str = ""
    boss_rule: Optional[BossRule] = None
    is_the_psychic: bool = False
    is_the_needle: bool = False
    is_the_water: bool = False
    is_the_eye: bool = False
    is_the_mouth: bool = False
    is_the_flint: bool = False
    is_the_ox: bool = False
    is_the_plant: bool = False
    is_the_tooth: bool = False
    debuffed_suit: Optional[str] = None

    # Contextual fields
    most_played_hand: Optional[str] = None
    money: int = 0
    played_hand_types_this_round: Set[str] = field(default_factory=set)
    active_warnings: List[str] = field(default_factory=list)
    strategic_directive: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "has_ride_the_bus": self.has_ride_the_bus,
            "has_green_joker": self.has_green_joker,
            "has_square_joker": self.has_square_joker,
            "has_banner": self.has_banner,
            "boss_name": self.boss_name,
            "boss_rule": self.boss_rule.to_dict() if self.boss_rule else None,
            "is_the_psychic": self.is_the_psychic,
            "is_the_needle": self.is_the_needle,
            "is_the_water": self.is_the_water,
            "is_the_eye": self.is_the_eye,
            "is_the_mouth": self.is_the_mouth,
            "is_the_flint": self.is_the_flint,
            "is_the_ox": self.is_the_ox,
            "is_the_plant": self.is_the_plant,
            "is_the_tooth": self.is_the_tooth,
            "debuffed_suit": self.debuffed_suit,
            "most_played_hand": self.most_played_hand,
            "money": self.money,
            "played_hands": list(self.played_hand_types_this_round),
            "warnings": self.active_warnings,
            "strategic_directive": self.strategic_directive,
        }


def detect_active_constraints(
    jokers: List[Joker],
    blind_name: str = "",
    played_hands: Optional[Set[str]] = None,
    most_played_hand: Optional[str] = None,
    money: int = 0,
) -> ActiveConstraints:
    """Scans jokers and current boss blind to identify active operational hazards and directives."""
    jnames = {j.name for j in (jokers or [])}

    has_bus = "Ride the Bus" in jnames
    has_green = "Green Joker" in jnames
    has_square = "Square Joker" in jnames
    has_banner = "Banner" in jnames

    boss_rule = BOSS_CATALOG.get(blind_name)
    is_psychic = boss_rule.requires_exact_5_cards if boss_rule else False
    is_needle = boss_rule.single_hand_only if boss_rule else (blind_name == "The Needle")
    is_water = boss_rule.zero_discards if boss_rule else (blind_name == "The Water")
    is_eye = boss_rule.no_repeat_hand_type if boss_rule else (blind_name == "The Eye")
    is_mouth = boss_rule.single_hand_type_only if boss_rule else (blind_name == "The Mouth")
    is_flint = boss_rule.halves_base_chips_mult if boss_rule else (blind_name == "The Flint")
    is_ox = boss_rule.wipes_money_on_most_played if boss_rule else (blind_name == "The Ox")
    is_plant = boss_rule.debuff_face_cards if boss_rule else (blind_name == "The Plant")
    is_tooth = (boss_rule.costs_money_per_card > 0) if boss_rule else False
    debuff_suit = boss_rule.debuffed_suit if boss_rule else DEBUFFED_SUIT_BOSSES.get(blind_name)

    warnings = []
    if has_bus:
        warnings.append("🚨 [Ride the Bus] 严禁打出计分的人头牌 (J/Q/K)，否则倍率归零！")
    if has_green:
        warnings.append("⚠️ [Green Joker] 每次弃牌 -1 Mult；优先出杂牌单打充当伪弃牌。")
    if is_psychic:
        warnings.append("🔮 [The Psychic] 必须打满 5 张牌！少于 5 张判定为 0 分，务必附带杂牌打出。")
    if is_needle:
        warnings.append("☠️ [The Needle] 全局仅剩 1 次出牌机会！必须在出牌前用光弃牌凑出斩杀爆发。")
    if is_eye:
        warnings.append("👁️ [The Eye] 本回合不可重复打出相同牌型！打出重复牌型直接判 0 分。")
    if is_mouth:
        warnings.append("👄 [The Mouth] 全回合只能打出单一牌型！打其他牌型直接判 0 分。")
    if is_ox:
        warnings.append(f"🐂 [The Ox] 严禁打出最常用牌型 '{most_played_hand or '未知'}'，否则当前 ${money} 全部归零！")
    if is_plant:
        warnings.append("🌿 [The Plant] 所有人头牌 (J/Q/K) 全部被封印削弱，无法得分！")
    if is_tooth:
        warnings.append("🦷 [The Tooth] 打出的每张牌扣除 $1！严禁带无用杂牌！")
    if debuff_suit:
        warnings.append(f"🛡️ [{blind_name}] 封印花色 '{debuff_suit}'，该花色牌不计分且无加成！")

    directive = boss_rule.strategic_directive if boss_rule else ""

    return ActiveConstraints(
        has_ride_the_bus=has_bus,
        has_green_joker=has_green,
        has_square_joker=has_square,
        has_banner=has_banner,
        boss_name=blind_name,
        boss_rule=boss_rule,
        is_the_psychic=is_psychic,
        is_the_needle=is_needle,
        is_the_water=is_water,
        is_the_eye=is_eye,
        is_the_mouth=is_mouth,
        is_the_flint=is_flint,
        is_the_ox=is_ox,
        is_the_plant=is_plant,
        is_the_tooth=is_tooth,
        debuffed_suit=debuff_suit,
        most_played_hand=most_played_hand,
        money=money,
        played_hand_types_this_round=played_hands or set(),
        active_warnings=warnings,
        strategic_directive=directive,
    )


@dataclass
class PlayConstraintVerdict:
    is_fatal_violation: bool
    penalty_multiplier: float  # Multiplier on candidate heuristic score (0.0 to 1.0)
    bonus_multiplier: float  # Growth bonus (e.g. 1.25 for Square Joker 4-card play)
    warning_title: Optional[str]
    reason: str


def evaluate_play_safety(
    cards: List[Card],
    hand_type: str,
    constraints: ActiveConstraints,
    scoring_cards: Optional[List[Card]] = None,
) -> PlayConstraintVerdict:
    """
    Checks whether a candidate play violates active rules or triggers joker growth.
    Enforces strict Boss rules (The Psychic, The Eye, The Mouth, The Ox, etc.)
    and Joker constraints (Ride the Bus).
    """
    scoring = scoring_cards if scoring_cards is not None else cards

    # 1. The Psychic: Fatal if fewer than 5 cards played
    if constraints.is_the_psychic and len(cards) < 5:
        return PlayConstraintVerdict(
            is_fatal_violation=True,
            penalty_multiplier=0.01,
            bonus_multiplier=1.0,
            warning_title="The Psychic 致命违规",
            reason=f"The Psychic 强制要求必须打满 5 张牌！当前仅选 {len(cards)} 张牌，打出将得 0 分！必须附带杂牌补足 5 张！",
        )

    # 2. Ride the Bus: Fatal if any SCORING card is J, Q, or K
    if constraints.has_ride_the_bus:
        scoring_faces = [c for c in scoring if c.rank in FACE_RANKS]
        if scoring_faces:
            return PlayConstraintVerdict(
                is_fatal_violation=True,
                penalty_multiplier=0.01,
                bonus_multiplier=1.0,
                warning_title="Ride the Bus 破戒清零",
                reason=f"打出计分人头牌 ({', '.join(c.label for c in scoring_faces)}) 将导致 Ride the Bus 倍率清零！",
            )

    # 3. The Eye: Boss blind forbids repeating the same poker hand type
    if constraints.is_the_eye and hand_type in constraints.played_hand_types_this_round:
        return PlayConstraintVerdict(
            is_fatal_violation=True,
            penalty_multiplier=0.01,
            bonus_multiplier=1.0,
            warning_title="The Eye 规则违规",
            reason=f"本回合已打过 '{hand_type}'，再次打出将判定为 0 分！",
        )

    # 4. The Mouth: Only 1 poker hand type permitted all round
    if constraints.is_the_mouth and constraints.played_hand_types_this_round:
        first_played = next(iter(constraints.played_hand_types_this_round))
        if hand_type != first_played:
            return PlayConstraintVerdict(
                is_fatal_violation=True,
                penalty_multiplier=0.01,
                bonus_multiplier=1.0,
                warning_title="The Mouth 牌型锁定违规",
                reason=f"The Mouth 限制本回合只能打出 '{first_played}'！打出 '{hand_type}' 判定为 0 分！",
            )

    # 5. The Ox: Wipes money if playing most played hand
    penalty = 1.0
    reason_parts = []
    if (
        constraints.is_the_ox
        and constraints.most_played_hand
        and hand_type == constraints.most_played_hand
        and constraints.money > 0
    ):
        penalty *= 0.15
        reason_parts.append(f"⚠️ 打出主力牌型 '{hand_type}' 将导致 ${constraints.money} 全部清零！")

    # 6. Debuffed Suit Bosses: Heavy penalty if majority of scoring cards belong to debuffed suit
    if constraints.debuffed_suit:
        debuffed_count = sum(1 for c in scoring if constraints.debuffed_suit in c.suits())
        if debuffed_count > 0:
            ratio = debuffed_count / max(1, len(scoring))
            penalty *= max(0.2, 1.0 - ratio * 0.8)
            reason_parts.append(f"计分牌包含 {debuffed_count} 张被封印的 {constraints.debuffed_suit} 花色")

    # 7. The Plant: Face cards are debuffed
    if constraints.is_the_plant:
        face_count = sum(1 for c in scoring if c.rank in FACE_RANKS)
        if face_count > 0:
            penalty *= max(0.3, 1.0 - (face_count / max(1, len(scoring))) * 0.7)
            reason_parts.append(f"计分牌包含 {face_count} 张被 The Plant 封印的人头牌")

    # 8. The Tooth: Lose $1 per card played -> penalize larger card plays
    if constraints.is_the_tooth:
        penalty *= 0.95 ** len(cards)
        reason_parts.append(f"The Tooth: 出 {len(cards)} 张牌将扣除 ${len(cards)}")

    # 9. Square Joker Growth Bonus: Playing exactly 4 cards grants permanent +4 chips
    growth_bonus = 1.0
    if constraints.has_square_joker and len(cards) == 4:
        growth_bonus = 1.25
        reason_parts.append("触发 Square Joker (+4 永久筹码成长)")

    reason_str = " | ".join(reason_parts) if reason_parts else "动作完全符合所有安全与小丑成长准则"
    return PlayConstraintVerdict(
        is_fatal_violation=False,
        penalty_multiplier=round(penalty, 4),
        bonus_multiplier=round(growth_bonus, 4),
        warning_title=None,
        reason=reason_str,
    )
