"""
Joker Ordering Optimizer: Left-to-Right Evaluation and Copy Alignment.

Balatro evaluates jokers in strict left-to-right order:
  Chips / +Mult  ──>  Scaling Triggers  ──>  X-Mult
Placing X-Mult to the left of +Mult results in disastrous loss:
  (4 * 2) + 20 = 28 mult   vs   (4 + 20) * 2 = 48 mult (71% higher!)

Copy jokers have directional mechanics:
  - Blueprint: Copies the joker immediately to its RIGHT.
  - Brainstorm: Copies the LEFTMOST joker in the lineup.

This module determines the mathematically optimal order for any joker lineup
and detects when a reorder is needed.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .state import Joker
from .synergy import JOKER_CATALOG, PillarType


def _classify_joker_position_tier(joker: Joker) -> float:
    """
    Assigns an ordering tier (lower numbers go leftmost, higher go rightmost).
    0: Economy / Inactive utility
    1: Flat Chips (+Chips)
    2: Flat Mult (+Mult)
    2.8: Flat Mult with Polychrome (triggers flat mult, then x1.5 mult)
    3: Triggered / Retrigger hooks (e.g. Hanging Chad, Hack)
    4: Copy Jokers (Blueprint / Brainstorm) when copying X-Mult
    5: X-Mult (x2, x3 mult) or non-mult Polychrome - MUST BE RIGIDLY ON THE RIGHTMOST SLOTS!
    """
    meta = JOKER_CATALOG.get(joker.name)
    name = joker.name
    edition = str(getattr(joker, "edition", None) or "").lower()

    # Blueprint and Brainstorm are dynamic
    if name in ("Blueprint", "Brainstorm"):
        return 4.0

    tier = 2.0
    if meta:
        if meta.primary_pillar == PillarType.CHIPS:
            tier = 1.0
        elif meta.primary_pillar == PillarType.FLAT_MULT:
            tier = 2.0
        elif meta.primary_pillar == PillarType.X_MULT:
            tier = 5.0
        elif meta.primary_pillar == PillarType.ECONOMY:
            tier = 0.0
        elif meta.primary_pillar == PillarType.UTILITY:
            tier = 3.0
    else:
        if (joker.xmult or 1.0) > 1.0:
            tier = 5.0
        elif (joker.mult or 0) > 0:
            tier = 2.0
        elif (joker.chips or 0) > 0:
            tier = 1.0

    # Edition weighting adjustments:
    # Polychrome grants x1.5 Mult.
    # If the joker has flat mult, place it right after pure flat mults (tier 2.8)
    # so its flat mult is added, then magnified together with earlier flat mults.
    # If the joker is chips, economy or utility, it has no flat mult, so place it in X-mult block (tier 5.0).
    if edition == "polychrome":
        if tier == 2.0:
            tier = 2.8
        elif tier < 5.0:
            tier = 5.0
    elif edition in ("holo", "holographic"):
        # Holographic grants +10 Flat Mult
        if tier < 2.0:
            tier = 2.0

    return tier


def compute_optimal_joker_order(
    jokers: List[Joker],
    phase: str = "play",  # "play" | "shop" | "cash_out"
) -> Tuple[List[int], bool, str]:
    """
    Computes the optimal left-to-right ordering permutation.

    Returns:
      (optimal_indices, needs_reorder, explanation)
      optimal_indices: list of original indices in the new order, e.g. [2, 0, 1]
    """
    n = len(jokers)
    if n <= 1:
        return list(range(n)), False, "小丑数量 <= 1，无需排序"

    # Identify copy jokers and key targets
    blueprint_idx = next((i for i, j in enumerate(jokers) if j.name == "Blueprint"), None)
    brainstorm_idx = next((i for i, j in enumerate(jokers) if j.name == "Brainstorm"), None)
    econ_indices = [
        i for i, j in enumerate(jokers)
        if j.name in ("Golden Joker", "Mail-In Rebate", "Trading Card", "Rocket", "To the Moon")
    ]
    xmult_indices = [
        i for i, j in enumerate(jokers)
        if (JOKER_CATALOG.get(j.name) and JOKER_CATALOG[j.name].primary_pillar == PillarType.X_MULT)
        or (j.xmult or 1.0) > 1.0
        or str(getattr(j, "edition", None) or "").lower() == "polychrome"
    ]

    # In shop/cash_out phase, prioritize duplicating economy jokers
    if phase in ("shop", "cash_out") and econ_indices and (blueprint_idx is not None or brainstorm_idx is not None):
        target_econ = econ_indices[0]
        # For Brainstorm: target must be at index 0
        # For Blueprint: Blueprint must be immediately left of target
        arranged = []
        used = set()

        if brainstorm_idx is not None and target_econ not in used:
            arranged.append(target_econ)
            used.add(target_econ)

        if blueprint_idx is not None and blueprint_idx not in used:
            arranged.append(blueprint_idx)
            used.add(blueprint_idx)
            if target_econ not in used:
                arranged.append(target_econ)
                used.add(target_econ)

        for i in range(n):
            if i not in used:
                arranged.append(i)
                used.add(i)

        current = list(range(n))
        needs_change = (arranged != current)
        return arranged, needs_change, "经济阶段：将复制小丑移至经济小丑旁吃双倍金币收益"

    # Standard "play" phase ordering:
    # 1. Base items sorted by tier (Chips -> Flat Mult -> Utility)
    # 2. If Blueprint exists and X-Mult exists, place Blueprint right before the strongest X-Mult
    # 3. If Brainstorm exists, position the strongest target at index 0
    # 4. All X-Mult jokers strictly at the end

    tiers = [_classify_joker_position_tier(j) for j in jokers]

    # Check if any X-Mult is currently sitting to the left of a Flat Mult
    has_inverted_xmult = False
    max_flat_pos = max((i for i, t in enumerate(tiers) if 2.0 <= t < 4.0), default=-1)
    min_xmult_pos = min((i for i, t in enumerate(tiers) if t >= 4.8), default=999)
    if min_xmult_pos < max_flat_pos:
        has_inverted_xmult = True

    # Build stable sorted order
    # For Blueprint: anchor next to best X-Mult or best Flat Mult
    indexed = list(range(n))

    def sort_key(i: int) -> float:
        tier = tiers[i]
        # Blueprint should be positioned directly before the X-Mult block (tier 4.9)
        if jokers[i].name == "Blueprint":
            return 4.9
        # Brainstorm can stay near left if copying best card
        if jokers[i].name == "Brainstorm":
            return 0.1
        return float(tier)

    optimal_order = sorted(indexed, key=sort_key)

    # Special handling: If Brainstorm is present, place the best copied card at slot 0
    # Crucial Balatro ordering rule: Slot 0 triggers first. If flat mult/chips exist,
    # Brainstorm should duplicate flat mult/chips first so that subsequent X-Mult jokers
    # on the right multiply the enlarged base mult!
    if brainstorm_idx is not None:
        candidates = [i for i in range(n) if jokers[i].name not in ("Brainstorm", "Blueprint")]
        if candidates:
            flat_or_chips = [i for i in candidates if tiers[i] < 4.0]
            if flat_or_chips:
                best_target = max(flat_or_chips, key=lambda i: (tiers[i], jokers[i].mult or 0, jokers[i].chips or 0))
            else:
                best_target = max(candidates, key=lambda i: (jokers[i].xmult or 1.0))
            optimal_order.remove(best_target)
            optimal_order.insert(0, best_target)

    # Special handling: Ensure Blueprint is directly before the best X-mult
    if blueprint_idx is not None and xmult_indices:
        valid_x = [i for i in xmult_indices if optimal_order.index(i) > 0]
        if valid_x:
            best_x = max(valid_x, key=lambda i: jokers[i].xmult or 1.0)
            optimal_order.remove(blueprint_idx)
            x_pos = optimal_order.index(best_x)
            optimal_order.insert(x_pos, blueprint_idx)

    current_order = list(range(n))
    needs_reorder = (optimal_order != current_order)

    reason = "当前小丑位置已为理论最优"
    if needs_reorder:
        if has_inverted_xmult:
            reason = "严重排位缺陷：乘法倍率 (X-Mult) 位于加法倍率左侧！重排后倍率收益大幅提升"
        elif blueprint_idx is not None or brainstorm_idx is not None:
            reason = "优化复制小丑 (Blueprint/Brainstorm) 锚定位置以实现指数级输出"
        else:
            reason = "小丑重排：严格遵守 [筹码 -> 基础倍率 -> 乘法倍率] 从左到右递增链"

    return optimal_order, needs_reorder, reason


def estimate_ordering_gain(jokers: List[Joker]) -> float:
    """
    Estimates the multiplier improvement ratio (e.g. 1.45 means +45% mult)
    from current layout to optimal layout under a representative hand.
    """
    if len(jokers) <= 1:
        return 1.0

    _, needs, _ = compute_optimal_joker_order(jokers, "play")
    if not needs:
        return 1.0

    # Simulate base score under current order vs optimal order
    def score_mult(order: List[int]) -> float:
        m = 4.0  # base mult
        for idx in order:
            j = jokers[idx]
            meta = JOKER_CATALOG.get(j.name)
            add_m = max(j.mult or 0, meta.base_mult if meta else 0)
            xm = max(j.xmult or 1.0, meta.base_xmult if meta else 1.0)
            edition = str(getattr(j, "edition", None) or "").lower()
            if edition in ("holo", "holographic"):
                add_m += 10.0
            m += add_m
            m *= xm
            if edition == "polychrome":
                m *= 1.5
        return m

    current_score = score_mult(list(range(len(jokers))))
    opt_order, _, _ = compute_optimal_joker_order(jokers, "play")
    optimal_score = score_mult(opt_order)

    if current_score <= 0:
        return 1.0
    return round(optimal_score / current_score, 2)
