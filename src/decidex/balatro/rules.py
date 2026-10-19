"""
Balatro legal-action enumeration and deterministic strategy defaults.

Action id conventions (stable, greppable):
  play:<i1>-<i2>-...      play those hand indices (1..5 cards)
  discard:<i1>-<...>      discard those hand indices (1..5 cards)
  use:<consumable_idx>    use the consumable now
  buy:<item_type>:<idx>   buy shop item (jokers, boosters, vouchers)
  toggle_shop             exit shop and proceed to next round
  reorder:<i0>-<i1>-...   reorder jokers for optimal multiplier sequence
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from decidex.types import ActionCandidate

from .blind_eval import evaluate_blind_choice
from .constraints import detect_active_constraints, evaluate_play_safety
from .deck_odds import (
    SUIT_NORM,
    calculate_flush_draw_odds,
    calculate_straight_draw_odds,
    compute_remaining_deck,
)
from .economy import calculate_economy_status, evaluate_purchase_budget
from .joker_order import compute_optimal_joker_order, estimate_ordering_gain
from .poker import enumerate_selections, get_scoring_cards
from .scoring import simulate_play
from .state import BalatroState, Card, Joker, RANK_ORDER
from .synergy import analyze_synergy, score_joker_candidate, score_owned_joker

MAX_SELECT = 5

def select_consumable_targets(
    cname: str,
    hand: List[Card],
    jokers: Optional[List[Joker]] = None,
) -> List[int]:
    """
    Select 0-based indices in hand for targeted Tarot and Spectral cards.
    Intelligently aligns enhancements with their scoring and holding context:
      - Steel / Gold: Target cards kept in hand (low unenhanced junk, or Kings if Baron present).
      - Glass / Lucky / Bonus / Mult: Target high-rank scoring cards (Aces, Kings).
      - Hanged Man / Stone: Target worst unenhanced cards (lowest rank).
      - Death / Cryptid: Target best card to duplicate, replace worst card.
      - Suit Converters (Moon/Sun/World/Star): Target off-suit cards to convert to target suit.
      - Seals (Red/Gold/Blue/Purple): Match trigger location.
    """
    if not hand:
        return []
    indexed_hand = list(enumerate(hand))
    has_baron = any(j.name == "Baron" for j in (jokers or []))

    if cname == "The Hanged Man":
        # Destroy up to 2 worst cards (lowest rank, unenhanced)
        unenhanced = [
            x for x in indexed_hand
            if not getattr(x[1], "enhancement", None) and not getattr(x[1], "seal", None) and not getattr(x[1], "edition", None)
        ]
        pool = unenhanced if unenhanced else indexed_hand
        pool.sort(key=lambda x: (RANK_ORDER.get(x[1].rank, 0), x[1].chips))
        return [x[0] for x in pool[:2]]

    elif cname == "Death":
        # Convert left card into right card: requires 2 cards
        if len(indexed_hand) < 2:
            return []
        sorted_by_val = sorted(indexed_hand, key=lambda x: (
            1 if getattr(x[1], "enhancement", None) or getattr(x[1], "edition", None) or getattr(x[1], "seal", None) else 0,
            RANK_ORDER.get(x[1].rank, 0)
        ))
        worst_idx = sorted_by_val[0][0]
        best_idx = sorted_by_val[-1][0]
        if worst_idx != best_idx:
            return [worst_idx, best_idx]
        return [sorted_by_val[0][0], sorted_by_val[1][0]]

    elif cname == "The Chariot":
        # Steel Card: gives x1.5 Mult while held in hand!
        # If Baron is present, Kings give x1.5 mult in hand -> Target King!
        # Otherwise, target an unenhanced low-rank card (2, 3, 4) kept in hand!
        if has_baron:
            kings = [x for x in indexed_hand if x[1].rank == "K" and getattr(x[1], "enhancement", "") != "steel"]
            if kings:
                return [kings[0][0]]
        unenhanced_low = [
            x for x in indexed_hand
            if not getattr(x[1], "enhancement", None) and not getattr(x[1], "edition", None) and not getattr(x[1], "seal", None)
        ]
        pool = unenhanced_low if unenhanced_low else indexed_hand
        pool.sort(key=lambda x: (RANK_ORDER.get(x[1].rank, 0), x[1].chips))
        return [pool[0][0]]

    elif cname == "The Devil":
        # Gold Card: gives $3 held in hand at end of round
        # Target an unenhanced low-rank card kept in hand
        unenhanced = [
            x for x in indexed_hand
            if not getattr(x[1], "enhancement", None) and not getattr(x[1], "edition", None) and not getattr(x[1], "seal", None)
        ]
        pool = unenhanced if unenhanced else indexed_hand
        pool.sort(key=lambda x: (RANK_ORDER.get(x[1].rank, 0), x[1].chips))
        return [pool[0][0]]

    elif cname == "The Tower":
        # Stone Card: +50 chips, no rank/suit, always scores
        # Target an unenhanced low-rank junk card (2, 3)
        unenhanced = [
            x for x in indexed_hand
            if not getattr(x[1], "enhancement", None) and not getattr(x[1], "edition", None) and not getattr(x[1], "seal", None)
        ]
        pool = unenhanced if unenhanced else indexed_hand
        pool.sort(key=lambda x: (RANK_ORDER.get(x[1].rank, 0), x[1].chips))
        return [pool[0][0]]

    elif cname == "Justice":
        # Glass Card: 2x Mult when scored (1/4 chance to break)
        # Target highest rank scoring card (Ace / King)
        sorted_by_rank = sorted(indexed_hand, key=lambda x: RANK_ORDER.get(x[1].rank, 0), reverse=True)
        return [sorted_by_rank[0][0]]

    elif cname in ("The Empress", "The Hierophant", "The Magician"):
        # Empress (+4 mult), Hierophant (+30 chips), Magician (Lucky): target up to 2 highest rank unenhanced cards
        unenhanced = [x for x in indexed_hand if not getattr(x[1], "enhancement", None)]
        pool = unenhanced if unenhanced else indexed_hand
        pool.sort(key=lambda x: RANK_ORDER.get(x[1].rank, 0), reverse=True)
        return [x[0] for x in pool[:2]]

    elif cname == "The Lovers":
        # Wild Card: counts as any suit
        unenhanced = [x for x in indexed_hand if not getattr(x[1], "enhancement", None)]
        pool = unenhanced if unenhanced else indexed_hand
        pool.sort(key=lambda x: RANK_ORDER.get(x[1].rank, 0), reverse=True)
        return [pool[0][0]]

    elif cname == "Strength":
        # Increase rank by 1: target cards with rank < 'A' (12)
        valid = [x for x in indexed_hand if RANK_ORDER.get(x[1].rank, 0) < RANK_ORDER.get("A", 12)]
        pool = valid if valid else indexed_hand
        pool.sort(key=lambda x: RANK_ORDER.get(x[1].rank, 0), reverse=True)
        return [x[0] for x in pool[:2]]

    # Suit Converters (converts up to 3 cards to target suit)
    elif cname in ("The Moon", "The Sun", "The World", "The Star"):
        target_suit = {
            "The Moon": "C",
            "The Sun": "H",
            "The World": "S",
            "The Star": "D",
        }[cname]
        off_suit = [x for x in indexed_hand if SUIT_NORM.get(x[1].suit, x[1].suit) != target_suit]
        off_suit.sort(key=lambda x: RANK_ORDER.get(x[1].rank, 0), reverse=True)
        return [x[0] for x in off_suit[:3]]

    # Spectral Cards
    elif cname == "Cryptid":
        # Creates 2 copies of 1 card in hand: select the best card
        best = max(indexed_hand, key=lambda x: (
            1 if getattr(x[1], "edition", None) else 0,
            1 if getattr(x[1], "enhancement", None) in ("glass", "steel") else 0,
            1 if getattr(x[1], "seal", None) == "red" else 0,
            RANK_ORDER.get(x[1].rank, 0),
        ))
        return [best[0]]

    elif cname == "Aura":
        # Add Foil/Holo/Poly to 1 card without edition
        no_ed = [x for x in indexed_hand if not getattr(x[1], "edition", None)]
        pool = no_ed if no_ed else indexed_hand
        pool.sort(key=lambda x: RANK_ORDER.get(x[1].rank, 0), reverse=True)
        return [pool[0][0]]

    elif cname == "Deja Vu":
        # Add Red Seal (retrigger) to 1 card
        no_seal = [x for x in indexed_hand if not getattr(x[1], "seal", None)]
        pool = no_seal if no_seal else indexed_hand
        pool.sort(key=lambda x: RANK_ORDER.get(x[1].rank, 0), reverse=True)
        return [pool[0][0]]

    elif cname == "Trance":
        # Add Blue Seal (generates Planet card held in hand) to 1 card
        no_seal = [x for x in indexed_hand if not getattr(x[1], "seal", None)]
        pool = no_seal if no_seal else indexed_hand
        pool.sort(key=lambda x: (RANK_ORDER.get(x[1].rank, 0), x[1].chips))
        return [pool[0][0]]

    elif cname == "Talisman":
        # Add Gold Seal ($3 on score) to 1 card
        no_seal = [x for x in indexed_hand if not getattr(x[1], "seal", None)]
        pool = no_seal if no_seal else indexed_hand
        pool.sort(key=lambda x: RANK_ORDER.get(x[1].rank, 0), reverse=True)
        return [pool[0][0]]

    elif cname == "Medium":
        # Add Purple Seal (creates Tarot when discarded) to 1 card
        no_seal = [x for x in indexed_hand if not getattr(x[1], "seal", None)]
        pool = no_seal if no_seal else indexed_hand
        pool.sort(key=lambda x: (RANK_ORDER.get(x[1].rank, 0), x[1].chips))
        return [pool[0][0]]

    return []


# Maintain backward compatibility alias
select_tarot_targets = select_consumable_targets

VOUCHER_BASE_SCORES: Dict[str, float] = {
    "Antimatter": 95.0,
    "Blank": 70.0,
    "Grabber": 88.0,
    "Nacho Tong": 90.0,
    "Wasteful": 82.0,
    "Recycler": 85.0,
    "Seed Money": 86.0,
    "Money Tree": 88.0,
    "Director's Cut": 84.0,
    "Retcon": 86.0,
    "Overstock": 80.0,
    "Overstock Plus": 82.0,
    "Clearance Sale": 78.0,
    "Liquidation": 80.0,
    "Tarot Merchant": 76.0,
    "Tarot Tycoon": 78.0,
    "Planet Merchant": 74.0,
    "Planet Tycoon": 76.0,
    "Hieroglyph": 75.0,
    "Petroglyph": 75.0,
}

PLANET_CARD_MAP: Dict[str, str] = {
    "Mercury": "Pair",
    "Venus": "Three of a Kind",
    "Earth": "Full House",
    "Mars": "Four of a Kind",
    "Jupiter": "Flush",
    "Saturn": "Straight",
    "Uranus": "Two Pair",
    "Neptune": "Straight Flush",
    "Pluto": "High Card",
    "Planet X": "Five of a Kind",
    "Ceres": "Flush House",
    "Eris": "Flush Five",
}

TARGETED_CONSUMABLES = {
    "The Hanged Man", "Death", "The Chariot", "Justice", "The Devil",
    "The Tower", "The Empress", "The Hierophant", "The Magician",
    "The Lovers", "Strength", "The Moon", "The Sun", "The World",
    "The Star", "Cryptid", "Aura", "Deja Vu", "Trance", "Talisman", "Medium"
}

PACK_STATES = {
    "TAROT_PACK", "PLANET_PACK", "SPECTRAL_PACK", "STANDARD_PACK", "BUFFOON_PACK"
}


def _ids(indices) -> str:
    return "-".join(str(i) for i in indices)


def _heuristic_keep(cards):
    """How much a discard set is 'worth dumping': low-value cards first."""
    return sum(5 - min(5, c.chips // 4) for c in cards)


def enumerate_legal_actions(state: BalatroState) -> List[ActionCandidate]:
    """All legal plays/discards/uses/shop actions, ranked with high-tier synergy and constraint heuristics."""
    candidates: List[ActionCandidate] = []
    state_name = getattr(state, "state_name", "SELECTING_HAND")

    # 1. Active Constraints & Synergy Context
    blind_name = getattr(getattr(state, "blind", None), "name", "")
    most_played = getattr(state, "most_played_hand", None)
    money = getattr(state, "money", 0)
    constraints = detect_active_constraints(
        state.jokers,
        blind_name=blind_name,
        played_hands=set(getattr(state, "played_hands", []) or []),
        most_played_hand=most_played,
        money=money,
    )
    synergy = analyze_synergy(state.jokers, getattr(state, "ante", 1))
    has_to_the_moon = any(j.name == "To the Moon" for j in state.jokers)
    econ = calculate_economy_status(
        getattr(state, "money", 0),
        getattr(state, "ante", 1),
        has_to_the_moon=has_to_the_moon,
    )

    # --- BLIND SELECT PHASE ACTIONS ---
    if state_name == "BLIND_SELECT" or (state_name not in ("SHOP", "SELECTING_HAND", "ROUND_EVAL", "GAME_OVER") and getattr(state, "can_select_blind", False)):
        blind_choice = evaluate_blind_choice(state)
        candidates.append(ActionCandidate(
            candidate_id="select_blind",
            description=f"进入盲注 [{blind_choice.blind_name}] (目标 {blind_choice.chips_required:,} 筹码) - {blind_choice.reason}",
            heuristic_score=round(blind_choice.select_blind_ev, 4),
            metadata={
                "action_type": "select_blind",
                "blind_name": blind_choice.blind_name,
                "chips": blind_choice.chips_required,
                "ev": blind_choice.select_blind_ev,
            },
        ))
        if blind_choice.can_skip:
            candidates.append(ActionCandidate(
                candidate_id="skip_blind",
                description=f"跳过盲注 [{blind_choice.blind_name}] 领取 Tag [{blind_choice.tag_name or '未知'}] - {blind_choice.reason}"
                            + (f" ({blind_choice.risk_warning})" if blind_choice.risk_warning else ""),
                heuristic_score=round(blind_choice.skip_blind_ev, 4),
                metadata={
                    "action_type": "skip_blind",
                    "blind_name": blind_choice.blind_name,
                    "tag_key": blind_choice.tag_key,
                    "tag_name": blind_choice.tag_name,
                    "tag_tier": blind_choice.tag_tier,
                    "ev": blind_choice.skip_blind_ev,
                    "risk_warning": blind_choice.risk_warning,
                },
            ))
        return candidates

    # --- BOOSTER PACK PHASE ACTIONS ---
    pack_cards = getattr(state, "pack_cards", []) or []
    if state_name in PACK_STATES or (state_name and state_name.endswith("_PACK")) or (pack_cards and state_name != "SHOP"):
        for idx, card in enumerate(pack_cards):
            card_idx = card.get("index", idx + 1)
            cname = card.get("name", "")
            cset = card.get("set", "") or card.get("type", "")

            if state_name == "BUFFOON_PACK" or cset == "Joker" or card.get("type") == "Joker":
                advice = score_joker_candidate(
                    cname, synergy, cost=0, current_money=econ.money,
                    has_slot=getattr(state, "jokers_count", len(state.jokers)) < getattr(state, "joker_slots", 5),
                    ante=getattr(state, "ante", 1),
                )
                h_score = 3.5 + (advice.score / 25.0)
                desc = f"选取小丑 [{cname}] - {advice.recommendation}: {advice.reason}"
            elif state_name == "PLANET_PACK" or cset == "Planet" or cname in PLANET_CARD_MAP:
                hand_upgraded = PLANET_CARD_MAP.get(cname, "手牌")
                h_score = 6.0
                if cname in ("Jupiter", "Earth", "Saturn", "Uranus"):
                    h_score += 0.8
                desc = f"选取星球牌 [{cname}] 永久升级 [{hand_upgraded}] 等级"
            elif state_name == "TAROT_PACK" or cset == "Tarot":
                if cname == "The Hermit":
                    h_score = 6.5 if econ.money >= 10 else 5.0
                    desc = f"选取塔罗牌 [{cname}] 金钱翻倍"
                elif cname == "Temperance":
                    h_score = 5.8
                    desc = f"选取塔罗牌 [{cname}] 变现小丑总价值"
                elif cname in ("The Wheel of Fortune", "The Emperor", "The High Priestess", "Judgement"):
                    h_score = 5.5
                    desc = f"选取高价值塔罗牌 [{cname}]"
                elif cname in ("Death", "The Hanged Man"):
                    h_score = 5.4
                    desc = f"选取优化牌库塔罗牌 [{cname}]"
                else:
                    h_score = 4.5
                    desc = f"选取强化塔罗牌 [{cname}]"
            elif state_name == "SPECTRAL_PACK" or cset == "Spectral":
                # Spectral cards evaluation
                j_count = len(state.jokers)
                if cname == "Black Hole":
                    h_score = 7.2
                    desc = "选取神级幻灵牌 [Black Hole] 所有牌型等级永久 +1"
                elif cname == "Immolate":
                    h_score = 6.8
                    desc = "选取幻灵牌 [Immolate] 摧毁5张手牌并获取暴利 $20"
                elif cname == "Cryptid":
                    h_score = 6.5
                    desc = "选取幻灵牌 [Cryptid] 复制手牌核心卡牌2份"
                elif cname == "DejaVu":
                    h_score = 6.4
                    desc = "选取幻灵牌 [DejaVu] 为手牌添加红封印 (触发2次)"
                elif cname == "Trance":
                    h_score = 6.2
                    desc = "选取幻灵牌 [Trance] 为手牌添加蓝封印 (留手生成星球牌)"
                elif cname in ("Talisman", "Medium"):
                    h_score = 6.0
                    desc = f"选取幻灵牌 [{cname}] 为手牌添加金/紫封印"
                elif cname == "Aura":
                    h_score = 5.8
                    desc = "选取幻灵牌 [Aura] 为手牌附加闪箔/全息/多彩特效"
                elif cname == "Ectoplasm":
                    h_score = 6.2 if getattr(state, "hand_size", 8) >= 7 else 3.2
                    desc = "选取幻灵牌 [Ectoplasm] 赋予小丑负片属性 (+1小丑槽位)"
                elif cname in ("Ankh", "Hex"):
                    # Ankh copies 1 joker, destroys all others. Hex adds Poly, destroys others.
                    # ONLY safe and godly if we have 1 high-tier joker!
                    if j_count == 1:
                        h_score = 7.0 if cname == "Ankh" else 6.2
                        desc = f"选取幻灵牌 [{cname}] 单核阵容产生质变升华"
                    elif j_count == 2:
                        h_score = 4.0
                        desc = f"选取幻灵牌 [{cname}] 需权衡摧毁另一个小丑的风险"
                    else:
                        h_score = 1.0  # Suicide for established 3+ joker squad!
                        desc = f"慎选幻灵牌 [{cname}] (⚠️ 会摧毁已有阵容其他所有小丑)"
                elif cname == "Wraith":
                    # Spawns a rare joker, sets money to $0
                    if econ.money <= 8 and getattr(state, "ante", 1) <= 3:
                        h_score = 6.4
                        desc = "选取幻灵牌 [Wraith] 低本金博取高价值 Rare 小丑"
                    else:
                        h_score = 2.8
                        desc = f"选取幻灵牌 [Wraith] (⚠️ 资金将清零，当前持 ${econ.money})"
                elif cname in ("Sigil", "Ouija"):
                    h_score = 5.6
                    desc = f"选取手牌转换幻灵牌 [{cname}]"
                else:
                    h_score = 5.0
                    desc = f"选取幻灵牌 [{cname}]"
            else:
                # Standard pack playing card
                edition = card.get("edition", "")
                seal = card.get("seal", "")
                enh = card.get("enhancement", "")
                crank = card.get("rank", "")
                base = 3.5
                if edition == "poly":
                    base += 3.0
                elif edition == "holo":
                    base += 2.2
                elif edition == "foil":
                    base += 1.6

                if seal == "red":
                    base += 2.8
                elif seal in ("purple", "blue"):
                    base += 2.2
                elif seal == "gold":
                    base += 1.8

                if enh in ("glass", "steel"):
                    base += 2.5
                elif enh == "lucky":
                    base += 1.8
                elif enh in ("bonus", "mult", "wild"):
                    base += 1.2

                if crank in ("A", "K", "Q"):
                    base += 0.5
                elif crank in ("2", "3") and not (edition or seal or enh):
                    base -= 1.2  # Penalize junk polluting deck!

                h_score = max(1.5, base)
                desc = f"选取卡牌 [{cname}]" + (f" ({edition})" if edition else "") + (f" [{enh}]" if enh else "") + (f" <{seal} seal>" if seal else "")

            candidates.append(ActionCandidate(
                candidate_id=f"select_pack_card:{card_idx}",
                description=desc,
                heuristic_score=round(h_score, 4),
                metadata={
                    "action_type": "select_pack_card",
                    "index": card_idx,
                    "name": cname,
                    "set": cset,
                },
            ))

        if getattr(state, "can_skip_booster", False) or not candidates:
            candidates.append(ActionCandidate(
                candidate_id="skip_booster",
                description="跳过补充包",
                heuristic_score=1.5,
                metadata={"action_type": "skip_booster"},
            ))

        return candidates

    # --- SHOP PHASE ACTIONS ---
    if state_name == "SHOP":
        shop = getattr(state, "shop", {}) or {}
        cards = shop.get("cards", [])
        jokers_count = getattr(state, "jokers_count", len(state.jokers))
        joker_slots = getattr(state, "joker_slots", 5)
        has_slot = jokers_count < joker_slots
        upcoming_boss = getattr(getattr(state, "blind", None), "upcoming_boss", None)

        # Exit shop candidate (always legal)
        candidates.append(ActionCandidate(
            candidate_id="toggle_shop",
            description="离开商店，进入下一轮盲注选择",
            heuristic_score=3.0,
            metadata={"action_type": "toggle_shop"},
        ))

        # Shop Jokers
        for idx, card in enumerate(cards):
            cname = card.get("name", "")
            cost = int(card.get("cost", 5))
            advice = score_joker_candidate(
                cname,
                synergy,
                cost,
                econ.money,
                has_slot,
                getattr(state, "ante", 1),
                upcoming_boss=upcoming_boss,
            )
            budget = evaluate_purchase_budget(
                cost=cost,
                item_type="joker",
                item_tier="S" if advice.score >= 85 else "A",
                econ=econ,
                is_critical_synergy_fix=(advice.target_pillar in synergy.missing_pillars),
            )

            # Heuristic score combining synergy score and budget clearance
            h_score = 1.0 + (advice.score / 25.0)
            if not budget.allowed:
                h_score *= 0.4
            if not advice.recommendation == "强烈推荐":
                h_score *= 0.8

            candidates.append(ActionCandidate(
                candidate_id=f"buy:joker:{idx}",
                description=f"购买小丑 [{cname}] (${cost}) - {advice.recommendation}: {advice.reason}",
                heuristic_score=round(h_score, 4),
                metadata={
                    "action_type": "buy_joker",
                    "index": idx,
                    "name": cname,
                    "cost": cost,
                    "score": advice.score,
                    "budget_allowed": budget.allowed,
                    "pillar": advice.target_pillar.value,
                },
            ))

        # Shop Boosters
        boosters = shop.get("boosters", [])
        for b_idx, b in enumerate(boosters):
            bname = b.get("name", "Booster Pack")
            cost = int(b.get("cost", 4))
            b_index = b.get("index", b_idx + 1)
            budget = evaluate_purchase_budget(
                cost=cost,
                item_type="booster",
                item_tier="A",
                econ=econ,
            )
            base_score = 75.0
            if "Buffoon" in bname:
                base_score = 85.0
            elif any(k in bname for k in ("Celestial", "Arcana", "Spectral")):
                base_score = 80.0
            h_score = 1.0 + (base_score / 30.0)
            if not budget.allowed:
                h_score *= 0.35
            candidates.append(ActionCandidate(
                candidate_id=f"buy:booster:{b_index}",
                description=f"购买补充包 [{bname}] (${cost}) - {'预算充足推荐开启' if budget.allowed else '资金紧张暂缓'}",
                heuristic_score=round(h_score, 4),
                metadata={
                    "action_type": "buy_booster",
                    "index": b_index,
                    "name": bname,
                    "cost": cost,
                    "budget_allowed": budget.allowed,
                },
            ))

        # Shop Vouchers
        vouchers = shop.get("vouchers", [])
        for v_idx, v in enumerate(vouchers):
            vname = v.get("name", "Voucher")
            cost = int(v.get("cost", 10))
            v_index = v.get("index", v_idx + 1)
            v_score = VOUCHER_BASE_SCORES.get(vname, 65.0)
            budget = evaluate_purchase_budget(
                cost=cost,
                item_type="voucher",
                item_tier="S" if v_score >= 85 else "A",
                econ=econ,
            )
            h_score = 1.0 + (v_score / 25.0)
            if not budget.allowed:
                h_score *= 0.35
            candidates.append(ActionCandidate(
                candidate_id=f"buy:voucher:{v_index}",
                description=f"购买优惠券 [{vname}] (${cost}) - {'预算充沛强烈推荐' if budget.allowed and v_score >= 80 else ('值得购买' if budget.allowed else '资金紧张暂缓')}",
                heuristic_score=round(h_score, 4),
                metadata={
                    "action_type": "buy_voucher",
                    "index": v_index,
                    "name": vname,
                    "cost": cost,
                    "score": v_score,
                    "budget_allowed": budget.allowed,
                },
            ))

        # Full Joker Replacement / Sell Evaluation
        if not has_slot and state.jokers and cards:
            best_adv = None
            best_card = None
            for card in cards:
                cname = card.get("name", "")
                cost = int(card.get("cost", 5))
                adv = score_joker_candidate(
                    cname,
                    synergy,
                    cost,
                    econ.money,
                    has_slot=True,
                    ante=getattr(state, "ante", 1),
                    upcoming_boss=upcoming_boss,
                )
                if adv.score >= 80 and econ.money >= cost:
                    if not best_adv or adv.score > best_adv.score:
                        best_adv = adv
                        best_card = card

            if best_adv and best_card:
                scored_owned = [(idx, j, score_owned_joker(j, synergy)) for idx, j in enumerate(state.jokers)]
                scored_owned.sort(key=lambda x: x[2])
                weakest_idx, weakest_j, weakest_score = scored_owned[0]
                if weakest_score <= 48 and (best_adv.score - weakest_score) >= 30:
                    sell_heuristic = 5.2
                    candidates.append(ActionCandidate(
                        candidate_id=f"sell:joker:{weakest_idx + 1}",
                        description=f"出售小丑 [{weakest_j.name}] (价值 {weakest_score}) 腾出槽位，迎进神级小丑 [{best_adv.joker_name}] (评级 {best_adv.score})",
                        heuristic_score=round(sell_heuristic, 4),
                        metadata={
                            "action_type": "sell_joker",
                            "index": weakest_idx + 1,
                            "sold_joker": weakest_j.name,
                            "target_joker": best_adv.joker_name,
                            "score_gain": round(best_adv.score - weakest_score, 1),
                        },
                    ))

        # Reroll Shop Evaluation
        reroll_cost = int(shop.get("reroll_cost", 5))
        has_strong_shop_card = any(
            c.heuristic_score >= 4.5
            for c in candidates
            if c.metadata.get("action_type") in ("buy_joker", "buy_booster", "buy_voucher")
        )
        if econ.money >= 25 + reroll_cost and not has_strong_shop_card:
            candidates.append(ActionCandidate(
                candidate_id="reroll_shop",
                description=f"刷新商店 (${reroll_cost}) - 资金充裕 (${econ.money} > $25) 寻找更高战力商品",
                heuristic_score=3.8,
                metadata={"action_type": "reroll_shop", "cost": reroll_cost},
            ))
        elif getattr(state, "ante", 1) <= 2 and jokers_count == 0 and econ.money >= reroll_cost and not has_strong_shop_card:
            candidates.append(ActionCandidate(
                candidate_id="reroll_shop",
                description=f"紧急刷新商店 (${reroll_cost}) - 暂无小丑且亟需战力通关",
                heuristic_score=4.2,
                metadata={"action_type": "reroll_shop", "cost": reroll_cost, "emergency": True},
            ))

        return candidates

    # --- PLAY / DISCARD / CONSUMABLE PHASE (SELECTING_HAND) ---
    blind = state.blind.remaining

    # Pass 1: Score every play, apply constraint safety verdicts
    scored_plays = []
    best_play_score = 0.0
    for sel in enumerate_selections(state.hand, MAX_SELECT):
        cards = [state.hand[i] for i in sel]
        result = simulate_play(cards, state)
        scoring = get_scoring_cards(cards, result.hand_type)
        junk = [c for c in cards if c not in scoring]
        safety = evaluate_play_safety(cards, result.hand_type, constraints, scoring_cards=scoring)
        scored_plays.append((sel, cards, result, scoring, junk, safety))
        if not safety.is_fatal_violation:
            best_play_score = max(best_play_score, result.score)

    hopeless = best_play_score * max(1, state.hands_left) < blind

    # Pass 2: Build play candidates with constraint-aware tiered priors & filtering bonus
    for sel, cards, result, scoring, junk, safety in scored_plays:
        clears = result.score >= blind and not safety.is_fatal_violation
        base_h = 2.0 + result.score / max(1.0, blind) + (3.0 if clears else 0.0)

        # Filtering heuristics: dumping junk cards alongside the main hand
        is_filtering = len(junk) > 0
        filtering_mod = 1.0
        if is_filtering:
            # The Psychic: 5-card play with junk attached is the essential survival technique
            if constraints.is_the_psychic and len(cards) == 5:
                filtering_mod *= 1.45
            # Zero discards / The Water / Green Joker: recycling junk cards via plays is optimal
            if constraints.is_the_water or state.discards_left == 0:
                filtering_mod += 0.08 * len(junk)
            if constraints.has_green_joker:
                filtering_mod += 0.12 * len(junk)
            # The Tooth: each card costs $1, so dumping extra junk is heavily penalized
            if constraints.is_the_tooth:
                filtering_mod *= 0.70 ** len(junk)
            # Ditching debuffed cards gives strong filtering bonus
            has_debuffed_junk = any(
                getattr(c, "debuff", False)
                or (constraints.debuffed_suit and constraints.debuffed_suit in c.suits())
                or (constraints.is_the_plant and getattr(c, "rank", "") in ("J", "Q", "K"))
                for c in junk
            )
            if has_debuffed_junk:
                filtering_mod += 0.15 * len(junk)
            elif not clears and not constraints.is_the_tooth:
                filtering_mod += 0.03 * len(junk)

            # In-hand card protection: Avoid dumping high-value held-in-hand cards (Steel, Gold, Blue Seal, Baron Kings, Enhancements, Editions, Seals, High cards)
            # If hand already clears the blind without filtering, NEVER dump extra cards unless required by The Psychic or Square Joker!
            if clears and not constraints.is_the_psychic and not getattr(constraints, "has_square_joker", False):
                filtering_mod *= 0.05

            if state.discards_left > 0 and not constraints.is_the_psychic and not getattr(constraints, "has_square_joker", False):
                if not has_debuffed_junk:
                    filtering_mod *= 0.85 ** len(junk)  # Prefer pure hand when discards are still available

            # Tally ranks and suits across full hand to protect pairs and flush outs
            hand_rank_counts = {}
            hand_suit_counts = {}
            for hc in state.hand:
                hrank = getattr(hc, "rank", "")
                hsuit = getattr(hc, "suit", "")
                hand_rank_counts[hrank] = hand_rank_counts.get(hrank, 0) + 1
                hand_suit_counts[hsuit] = hand_suit_counts.get(hsuit, 0) + 1

            has_baron = any(j.name == "Baron" for j in state.jokers)
            has_shoot_moon = any(j.name == "Shoot the Moon" for j in state.jokers)
            for c in junk:
                enh = getattr(c, "enhancement", "") or ""
                seal = getattr(c, "seal", "") or ""
                crank = getattr(c, "rank", "") or ""
                csuit = getattr(c, "suit", "")
                ed = getattr(c, "edition", None)
                is_c_debuffed = (
                    getattr(c, "debuff", False)
                    or (constraints.debuffed_suit and constraints.debuffed_suit in c.suits())
                    or (constraints.is_the_plant and crank in ("J", "Q", "K"))
                )

                if is_c_debuffed:
                    # Debuffed cards are top-priority targets to safely dump
                    filtering_mod += 0.15
                    continue

                # Hard Veto: Enhancements, Editions, Seals
                if enh in ("steel", "gold", "glass", "lucky", "mult", "bonus", "stone") or ed or seal:
                    filtering_mod *= 0.005
                # Hard Veto: High ranks (A, K, Q, J) are prime assets, never unscored junk!
                elif crank in ("A", "K", "Q", "J"):
                    filtering_mod *= 0.005
                # Hard Veto: Protect pairs / multiples in hand
                elif hand_rank_counts.get(crank, 0) >= 2:
                    filtering_mod *= 0.02
                # Hard Veto: Protect flush outs (3+ of this suit)
                elif hand_suit_counts.get(csuit, 0) >= 3:
                    filtering_mod *= 0.05
                elif has_baron and crank == "K":
                    filtering_mod *= 0.001
                elif has_shoot_moon and crank == "Q":
                    filtering_mod *= 0.001
                elif crank in ("2", "3", "4", "5", "6"):
                    filtering_mod += 0.05  # Valid isolated low-rank junk
                else:
                    filtering_mod *= 0.20  # 7-10 mid cards avoided unless necessary

        heuristic = base_h * safety.penalty_multiplier * safety.bonus_multiplier * filtering_mod
        if safety.is_fatal_violation:
            heuristic = 0.05  # Dropped below all legal moves to protect the run

        desc = f"打出 {result.display}"
        if is_filtering:
            dumped_str = " ".join(c.label for c in junk)
            desc += f" (附带弃牌: {dumped_str})"

        if safety.is_fatal_violation:
            desc += f" (⚠️ {safety.reason})"
        elif safety.reason and safety.reason != "动作完全符合所有安全与小丑成长准则":
            desc += f" ({safety.reason})"
        elif clears:
            desc += "(足够过关)"
        else:
            desc += f"(距 {blind - result.score:.0f})"

        candidates.append(ActionCandidate(
            candidate_id=f"play:{_ids(sel)}",
            description=desc,
            heuristic_score=round(heuristic, 4),
            metadata={
                "action_type": "play",
                "indices": list(sel),
                "cards": [c.label for c in cards],
                "hand_type": result.hand_type,
                "score": result.score,
                "chips": result.chips,
                "mult": result.mult,
                "clears": clears,
                "fatal_violation": safety.is_fatal_violation,
                "safety_reason": safety.reason,
                "is_filtering_play": is_filtering,
                "scoring_cards": [c.label for c in scoring],
                "dumped_cards": [c.label for c in junk],
            },
        ))

    # Pass 3: Discards
    # The Water forbids discarding completely (0 discards)
    if state.discards_left > 0 and not constraints.is_the_water:
        # Green Joker penalty: Discarding incurs -1 mult penalty
        discard_penalty = 0.6 if constraints.has_green_joker else 1.0

        # The Needle: Only 1 hand allowed. If hand doesn't clear, burning discards is imperative!
        if constraints.is_the_needle and not any(p[5].is_fatal_violation is False and p[2].score >= blind for p in scored_plays):
            discard_penalty *= 2.0

        deck_profile = compute_remaining_deck(
            state.hand,
            discards=getattr(state, "discards", None),
            deck_cards=getattr(state, "deck_cards", None),
            total_deck_size=getattr(state, "deck_count", 52),
        )
        has_baron = any(j.name == "Baron" for j in state.jokers)
        has_shoot_moon = any(j.name == "Shoot the Moon" for j in state.jokers)

        for sel in enumerate_selections(state.hand, MAX_SELECT):
            cards = [state.hand[i] for i in sel]
            kept = [state.hand[i] for i in range(len(state.hand)) if i not in sel]

            # In-hand held card protection: severely penalize discarding high-value passive cards
            held_retention_mod = 1.0
            for c in cards:
                enh = getattr(c, "enhancement", "") or ""
                seal = getattr(c, "seal", "") or ""
                crank = getattr(c, "rank", "") or ""
                if enh == "steel":
                    held_retention_mod *= 0.25  # Steel provides x1.5 Mult while held in hand
                elif enh == "gold":
                    held_retention_mod *= 0.45  # Gold provides $3 at round end
                elif seal == "blue":
                    held_retention_mod *= 0.35  # Blue seal generates Planet card
                elif has_baron and crank == "K":
                    held_retention_mod *= 0.20  # Baron Kings provide x1.5 Mult
                elif has_shoot_moon and crank == "Q":
                    held_retention_mod *= 0.30  # Shoot the Moon Queens provide +13 Mult

            # Deck odds draw bonus: evaluate if keeping cards sets up a high-probability Flush or Straight
            draw_bonus = 0.0
            draw_desc = ""
            odds_weight = 0.8 if hopeless else 0.4
            if len(kept) >= 4 and len(cards) >= 1:
                # Check for 4-card flush draw
                suit_counts: Dict[str, int] = {}
                for kc in kept:
                    s = SUIT_NORM.get(kc.suit, kc.suit)
                    suit_counts[s] = suit_counts.get(s, 0) + 1
                for s, count in suit_counts.items():
                    if count >= 4:
                        prob = calculate_flush_draw_odds(count, s, deck_profile, len(cards))
                        if prob > 0.3:
                            draw_bonus = max(draw_bonus, prob * odds_weight)
                            draw_desc = f" (追同花 {prob*100:.0f}%)"
                        break

                # Check for 4-card straight draw
                if not draw_desc and len(kept) >= 4:
                    kept_ranks = {kc.rank for kc in kept}
                    sprob = calculate_straight_draw_odds(kept_ranks, deck_profile, len(cards))
                    if sprob > 0.25:
                        draw_bonus = max(draw_bonus, sprob * (odds_weight * 0.85))
                        draw_desc = f" (追顺子 {sprob*100:.0f}%)"

            base_heuristic = 1.0 + 0.03 * _heuristic_keep(cards) + (2.5 if hopeless else 0.0)
            heuristic = (base_heuristic + draw_bonus) * discard_penalty * held_retention_mod

            desc = f"弃掉 {' '.join(c.label for c in cards)} 换牌" + draw_desc
            if constraints.has_green_joker:
                desc += " (⚠️ Green Joker 惩罚)"

            candidates.append(ActionCandidate(
                candidate_id=f"discard:{_ids(sel)}",
                description=desc,
                heuristic_score=round(heuristic, 4),
                metadata={
                    "action_type": "discard",
                    "indices": list(sel),
                    "cards": [c.label for c in cards],
                    "green_joker_penalty": constraints.has_green_joker,
                    "draw_bonus": round(draw_bonus, 4),
                    "held_retention_mod": round(held_retention_mod, 4),
                },
            ))

    # Pass 4: Consumables
    for i, cons in enumerate(state.consumables):
        cname = cons.name
        ctype = cons.type.lower()

        # Resolve targets if targeted consumable
        if cname in TARGETED_CONSUMABLES:
            targets = select_consumable_targets(cname, state.hand, state.jokers)
            target_labels = [state.hand[idx].label for idx in targets]
        else:
            targets = []
            target_labels = []

        # Check whether the engine allows using this card right now
        card_can_use = getattr(cons, "can_use", True)
        if card_can_use is False:
            h_score = 0.5
            desc = f"使用消耗牌 [{cname}] (引擎限制当前不可用)"
        elif cname in PLANET_CARD_MAP or ctype == "planet":
            h_score = 5.8
            hand_type = PLANET_CARD_MAP.get(cname, "指定手牌")
            desc = f"使用星球牌 [{cname}] 永久升级 [{hand_type}] 等级"
        elif cname == "Black Hole":
            h_score = 6.2
            desc = "使用幻灵牌 [Black Hole] 全牌型等级统一 +1 级！"
        elif cname == "The Hermit":
            if money >= 10:
                h_score = 5.6
                desc = f"使用塔罗牌 [The Hermit] 金钱翻倍 (当前 ${money})"
            elif money >= 5:
                h_score = 4.2
                desc = f"使用塔罗牌 [The Hermit] 金钱翻倍 (当前 ${money})"
            else:
                h_score = 2.5
                desc = f"使用塔罗牌 [The Hermit] (建议留存至 $10+ 翻倍)"
        elif cname == "Temperance":
            sell_val = sum(getattr(j, "sell_value", 3) or 3 for j in state.jokers)
            if sell_val >= 10:
                h_score = 5.6
            elif sell_val >= 5:
                h_score = 5.0
            else:
                h_score = 3.2
            desc = f"使用塔罗牌 [Temperance] 变现小丑价值 (+${sell_val})"
        elif cname == "The Wheel of Fortune":
            has_editionless = any(not getattr(j, "edition", None) for j in state.jokers)
            h_score = 4.8 if has_editionless else 2.0
            desc = f"使用塔罗牌 [The Wheel of Fortune] 尝试为小丑获取闪箔/全息/多彩特效"
        elif cname in ("The Emperor", "The High Priestess"):
            h_score = 4.8
            item_type = "塔罗牌" if cname == "The Emperor" else "星球牌"
            desc = f"使用塔罗牌 [{cname}] 生成额外 2 张{item_type}"
        elif cname == "The Fool":
            h_score = 5.2
            desc = "使用塔罗牌 [The Fool] 复制上一张使用的塔罗/星球牌"
        elif cname == "Judgement":
            jokers_count = getattr(state, "jokers_count", len(state.jokers))
            joker_slots = getattr(state, "joker_slots", 5)
            h_score = 5.2 if jokers_count < joker_slots else 2.0
            desc = f"使用塔罗牌 [Judgement] 随机创造新小丑"
        elif cname == "The Hanged Man":
            h_score = 5.5 if len(targets) > 0 else 2.0
            desc = f"使用塔罗牌 [The Hanged Man] 净化牌库：永久销毁废牌 [{' '.join(target_labels)}]"
        elif cname == "Death":
            if len(targets) >= 2:
                left_lbl = state.hand[targets[0]].label
                right_lbl = state.hand[targets[1]].label
                h_score = 5.6
                desc = f"使用塔罗牌 [Death] 复制强力卡牌：将废牌 [{left_lbl}] 转化为核心牌 [{right_lbl}]"
            else:
                h_score = 2.0
                desc = "使用塔罗牌 [Death] 转换卡牌 (手牌数量不足)"
        elif cname == "The Chariot":
            if targets:
                target_lbl = state.hand[targets[0]].label
                h_score = 5.3
                desc = f"使用塔罗牌 [The Chariot] 将卡牌 [{target_lbl}] 改造为钢铁牌 (+1.5x Mult 留手倍率)"
            else:
                h_score = 2.0
                desc = "使用塔罗牌 [The Chariot] (无合适目标)"
        elif cname == "Justice":
            if targets:
                target_lbl = state.hand[targets[0]].label
                h_score = 5.1
                desc = f"使用塔罗牌 [Justice] 将卡牌 [{target_lbl}] 改造为玻璃牌 (2x Mult)"
            else:
                h_score = 2.0
                desc = "使用塔罗牌 [Justice] (无合适目标)"
        elif cname == "The Devil":
            if targets:
                h_score = 5.0
                desc = f"使用塔罗牌 [The Devil] 将卡牌 [{target_labels[0]}] 改造为黄金牌 (留手回合结算 $3)"
            else:
                h_score = 2.0
                desc = "使用塔罗牌 [The Devil] (无合适目标)"
        elif cname == "The Tower":
            if targets:
                h_score = 4.6
                desc = f"使用塔罗牌 [The Tower] 将废牌 [{target_labels[0]}] 改造为石头牌 (+50 筹码消杂)"
            else:
                h_score = 2.0
                desc = "使用塔罗牌 [The Tower] (无合适目标)"
        elif cname in ("The Empress", "The Hierophant", "The Magician"):
            if targets:
                if cname == "The Empress":
                    enh_name, h_score = "+4 Mult", 4.7
                elif cname == "The Hierophant":
                    enh_name, h_score = "+30 Chips", 4.7
                else:
                    enh_name, h_score = "幸运牌 (+20 Mult / $20 机率)", 4.6
                desc = f"使用塔罗牌 [{cname}] 附魔卡牌 [{' '.join(target_labels)}] 为{enh_name}"
            else:
                h_score = 2.0
                desc = f"使用塔罗牌 [{cname}] (无合适目标)"
        elif cname == "The Lovers":
            if targets:
                h_score = 4.3
                desc = f"使用塔罗牌 [The Lovers] 附魔万能牌 [{target_labels[0]}] (充当任意花色)"
            else:
                h_score = 2.0
                desc = "使用塔罗牌 [The Lovers] (无合适目标)"
        elif cname == "Strength":
            if targets:
                h_score = 4.5
                desc = f"使用塔罗牌 [Strength] 将卡牌 [{' '.join(target_labels)}] 点数升级 (+1 Rank)"
            else:
                h_score = 2.0
                desc = "使用塔罗牌 [Strength] (无合适目标)"
        elif cname in ("The Moon", "The Sun", "The World", "The Star"):
            suit_names = {
                "The Moon": "梅花 (Clubs)",
                "The Sun": "红桃 (Hearts)",
                "The World": "黑桃 (Spades)",
                "The Star": "方片 (Diamonds)",
            }
            target_suit_name = suit_names.get(cname, "目标花色")
            if targets:
                h_score = 5.0
                desc = f"使用塔罗牌 [{cname}] 将杂牌 [{' '.join(target_labels)}] 转化为 {target_suit_name}"
            else:
                h_score = 2.0
                desc = f"使用塔罗牌 [{cname}] (手牌已全为该花色)"
        elif cname == "Cryptid":
            if targets:
                h_score = 6.0
                desc = f"使用幻灵牌 [Cryptid] 双倍复制核心牌 [{target_labels[0]}]"
            else:
                h_score = 2.0
                desc = "使用幻灵牌 [Cryptid] (手牌无目标)"
        elif cname == "Aura":
            if targets:
                h_score = 5.2
                desc = f"使用幻灵牌 [Aura] 为 [{target_labels[0]}] 赋予特殊版本词缀 (闪箔/全息/多彩)"
            else:
                h_score = 2.0
                desc = "使用幻灵牌 [Aura] (无合适卡牌)"
        elif cname == "Deja Vu":
            if targets:
                h_score = 5.4
                desc = f"使用幻灵牌 [Deja Vu] 为核心牌 [{target_labels[0]}] 刻印红色封印 (重复触发)"
            else:
                h_score = 2.0
                desc = "使用幻灵牌 [Deja Vu] (无合适卡牌)"
        elif cname == "Trance":
            if targets:
                h_score = 5.3
                desc = f"使用幻灵牌 [Trance] 为留手牌 [{target_labels[0]}] 刻印蓝色封印 (回合结束生成星球牌)"
            else:
                h_score = 2.0
                desc = "使用幻灵牌 [Trance] (无合适卡牌)"
        elif cname == "Talisman":
            if targets:
                h_score = 5.0
                desc = f"使用幻灵牌 [Talisman] 为核心得分牌 [{target_labels[0]}] 刻印金色封印 (计分时奖励 $3)"
            else:
                h_score = 2.0
                desc = "使用幻灵牌 [Talisman] (无合适卡牌)"
        elif cname == "Medium":
            if targets:
                h_score = 5.1
                desc = f"使用幻灵牌 [Medium] 为弃牌目标 [{target_labels[0]}] 刻印紫色封印 (弃牌生成塔罗牌)"
            else:
                h_score = 2.0
                desc = "使用幻灵牌 [Medium] (无合适卡牌)"
        elif cname == "Immolate":
            h_score = 5.8
            desc = "使用幻灵牌 [Immolate] 销毁 5 张手牌并立即获得 $20 现金"
        elif cname == "Sigil":
            h_score = 4.8
            desc = "使用幻灵牌 [Sigil] 将全手牌转换为单一随机花色"
        elif cname == "Ouija":
            h_score = 4.5
            desc = "使用幻灵牌 [Ouija] 将全手牌转换为单一同一点数 (-1 手牌上限)"
        elif cname == "Ectoplasm":
            h_score = 5.5 if len(state.jokers) >= 3 else 3.0
            desc = "使用幻灵牌 [Ectoplasm] 赋予小丑负片属性 (+1 小丑槽位, -1 手牌上限)"
        elif cname == "Hex":
            h_score = 5.8 if len(state.jokers) == 1 else 1.5
            desc = "使用幻灵牌 [Hex] 为小丑赋予多彩 (x1.5 Mult) (⚠️ 摧毁其余所有小丑)"
        elif cname == "Ankh":
            h_score = 5.8 if len(state.jokers) == 1 else 1.5
            desc = "使用幻灵牌 [Ankh] 复制随机小丑 (⚠️ 摧毁其余所有小丑)"
        elif cname == "Wraith":
            h_score = 5.2 if money <= 4 else 2.0
            desc = f"使用幻灵牌 [Wraith] 随机创造稀有小丑 (资金归零，当前 ${money})"
        else:
            h_score = 3.2
            desc = f"先使用 {cons.name}({cons.type})"

        candidates.append(ActionCandidate(
            candidate_id=f"use:{i}",
            description=desc,
            heuristic_score=round(h_score, 4),
            metadata={
                "action_type": "use_consumable",
                "index": i,
                "name": cons.name,
                "target_indices": targets,
            },
        ))

    # Pass 5: Joker Reorder Optimization (from left to right mathematical law)
    order_phase = "shop" if state_name == "SHOP" else ("cash_out" if state_name == "ROUND_EVAL" else "play")
    opt_order, needs_reorder, reorder_reason = compute_optimal_joker_order(state.jokers, phase=order_phase)
    if needs_reorder:
        gain = estimate_ordering_gain(state.jokers)
        reorder_heuristic = 5.5 + gain * 1.5
        order_str = "-".join(str(i) for i in opt_order)
        candidates.append(ActionCandidate(
            candidate_id=f"reorder:{order_str}",
            description=f"调整小丑排布顺序 [{order_str}] - {reorder_reason} (增益 ~{gain:.1f}x)",
            heuristic_score=round(reorder_heuristic, 4),
            metadata={
                "action_type": "reorder_jokers",
                "optimal_order": opt_order,
                "ordering_gain": gain,
                "reason": reorder_reason,
            },
        ))

    return candidates


def choose_safe_default(candidates: List[ActionCandidate], state: BalatroState) -> ActionCandidate:
    """
    Deterministic fallback policy enhanced with safety constraints and clear priority:
      1. Booster pack actions (select_pack_card / skip_booster).
      2. High-value consumables (Planets, Hermit >= $10, Temperance) that permanently boost power or economy.
      3. Guaranteed clearing play that does NOT violate fatal constraints (e.g. no Ride the Bus face cards, meets The Psychic 5-card rule).
      4. If jokers need reordering to maximize multipliers, prioritize reorder before playing.
      5. Highest heuristic-scoring safe play (prefers filtering plays when 0 discards/The Water/Green Joker).
      6. If in shop phase, prefer legal buy with highest score or toggle_shop.
      7. Strategic discard if all plays are hopeless and discards remain.
      8. Safe non-violating play / highest candidate.
    """
    if not candidates:
        return ActionCandidate(candidate_id="play:0", description="打出第一张牌",
                               metadata={"action_type": "play", "indices": [0]})

    # 1. Booster pack actions
    pack_actions = [c for c in candidates if c.metadata.get("action_type") in ("select_pack_card", "skip_booster")]
    if pack_actions:
        return max(pack_actions, key=lambda c: c.heuristic_score)

    # 1b. Blind selection actions (select_blind / skip_blind)
    blind_actions = [c for c in candidates if c.metadata.get("action_type") in ("select_blind", "skip_blind")]
    if blind_actions:
        return max(blind_actions, key=lambda c: c.heuristic_score)

    # 2. High-value consumables (score >= 4.5, e.g. Planet, Hermit, Temperance)
    high_consumables = [
        c for c in candidates
        if c.metadata.get("action_type") == "use_consumable" and c.heuristic_score >= 4.5
    ]
    if high_consumables:
        return max(high_consumables, key=lambda c: c.heuristic_score)

    # 3. Prefer guaranteed clearing safe play (highest heuristic score: prioritizes pure hands over unnecessary filtering)
    plays = [c for c in candidates if c.metadata.get("action_type") == "play"]
    safe_plays = [c for c in plays if not c.metadata.get("fatal_violation", False)]
    clears = [c for c in safe_plays if c.metadata.get("clears")]
    if clears:
        return max(clears, key=lambda c: c.heuristic_score)

    # 4. If jokers need reordering to maximize multipliers, prioritize reorder before playing
    reorders = [c for c in candidates if c.metadata.get("action_type") == "reorder_jokers"]
    if reorders:
        return max(reorders, key=lambda c: c.heuristic_score)

    # 5. If safe plays exist, take the highest heuristic score (factors in filtering utility & growth)
    if safe_plays:
        return max(safe_plays, key=lambda c: c.heuristic_score)

    # 5b. If all plays have fatal violations (e.g. <5 cards on The Psychic), prioritize strategic discard to survive
    discards = [c for c in candidates if c.metadata.get("action_type") == "discard"]
    if discards:
        return max(discards, key=lambda c: c.heuristic_score)

    # 6. If in shop phase, prefer legal buy/sell/voucher/reroll or toggle_shop
    shop_actions = [
        c for c in candidates
        if c.metadata.get("action_type") in (
            "buy_joker",
            "buy_booster",
            "buy_voucher",
            "sell_joker",
            "reroll_shop",
            "toggle_shop",
        )
    ]
    if shop_actions:
        return max(shop_actions, key=lambda c: c.heuristic_score)

    return max(candidates, key=lambda c: c.heuristic_score)
