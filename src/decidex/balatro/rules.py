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

from .constraints import detect_active_constraints, evaluate_play_safety
from .economy import calculate_economy_status, evaluate_purchase_budget
from .joker_order import compute_optimal_joker_order, estimate_ordering_gain
from .poker import enumerate_selections
from .scoring import simulate_play
from .state import BalatroState
from .synergy import analyze_synergy, score_joker_candidate

MAX_SELECT = 5


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
    constraints = detect_active_constraints(state.jokers, getattr(state.blind, "name", ""))
    synergy = analyze_synergy(state.jokers, getattr(state, "ante", 1))
    econ = calculate_economy_status(getattr(state, "money", 0), getattr(state, "ante", 1))

    # --- SHOP PHASE ACTIONS ---
    if state_name == "SHOP":
        shop = getattr(state, "shop", {}) or {}
        cards = shop.get("cards", [])
        jokers_count = getattr(state, "jokers_count", len(state.jokers))
        joker_slots = getattr(state, "joker_slots", 5)
        has_slot = jokers_count < joker_slots

        # Exit shop candidate (always legal)
        candidates.append(ActionCandidate(
            candidate_id="toggle_shop",
            description="离开商店，进入下一轮盲注选择",
            heuristic_score=3.0,
            metadata={"action_type": "toggle_shop"},
        ))

        for idx, card in enumerate(cards):
            cname = card.get("name", "")
            cost = int(card.get("cost", 5))
            advice = score_joker_candidate(cname, synergy, cost, econ.money, has_slot, getattr(state, "ante", 1))
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

        return candidates

    # --- PLAY / DISCARD / CONSUMABLE PHASE (SELECTING_HAND) ---
    blind = state.blind.remaining

    # Pass 1: Score every play, apply constraint safety verdicts
    scored_plays = []
    best_play_score = 0.0
    for sel in enumerate_selections(state.hand, MAX_SELECT):
        cards = [state.hand[i] for i in sel]
        result = simulate_play(cards, state)
        safety = evaluate_play_safety(cards, result.hand_type, constraints)
        scored_plays.append((sel, cards, result, safety))
        if not safety.is_fatal_violation:
            best_play_score = max(best_play_score, result.score)

    hopeless = best_play_score * max(1, state.hands_left) < blind

    # Pass 2: Build play candidates with constraint-aware tiered priors
    for sel, cards, result, safety in scored_plays:
        clears = result.score >= blind and not safety.is_fatal_violation
        base_h = 2.0 + result.score / max(1.0, blind) + (3.0 if clears else 0.0)

        # Apply safety penalty (e.g. 0.01 for Ride the Bus violation) and growth bonus
        heuristic = base_h * safety.penalty_multiplier * safety.bonus_multiplier
        if safety.is_fatal_violation:
            heuristic = 0.05  # Dropped below all legal moves to protect the run

        desc = f"打出 {result.display}"
        if safety.is_fatal_violation:
            desc += f" (⚠️ {safety.reason})"
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
            },
        ))

    # Pass 3: Discards
    if state.discards_left > 0:
        # Green Joker penalty: Discarding incurs -1 mult penalty
        discard_penalty = 0.6 if constraints.has_green_joker else 1.0

        for sel in enumerate_selections(state.hand, MAX_SELECT):
            cards = [state.hand[i] for i in sel]
            heuristic = (1.0 + 0.03 * _heuristic_keep(cards) + (2.5 if hopeless else 0.0)) * discard_penalty
            candidates.append(ActionCandidate(
                candidate_id=f"discard:{_ids(sel)}",
                description=f"弃掉 {' '.join(c.label for c in cards)} 换牌"
                            + (" (⚠️ Green Joker 惩罚)" if constraints.has_green_joker else ""),
                heuristic_score=round(heuristic, 4),
                metadata={
                    "action_type": "discard",
                    "indices": list(sel),
                    "cards": [c.label for c in cards],
                    "green_joker_penalty": constraints.has_green_joker,
                },
            ))

    # Pass 4: Consumables
    for i, cons in enumerate(state.consumables):
        candidates.append(ActionCandidate(
            candidate_id=f"use:{i}",
            description=f"先使用 {cons.name}({cons.type})",
            heuristic_score=1.9,
            metadata={"action_type": "use_consumable", "index": i, "name": cons.name},
        ))

    # Pass 5: Joker Reorder Optimization (from left to right mathematical law)
    opt_order, needs_reorder, reorder_reason = compute_optimal_joker_order(state.jokers)
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
      1. Guaranteed clearing play that does NOT violate fatal constraints (e.g. no Ride the Bus face cards).
      2. If jokers are out of order and reordering yields substantial gain, reorder first.
      3. Highest scoring safe play.
      4. Strategic discard if all plays are hopeless and discards remain.
      5. Safe non-violating play.
    """
    if not candidates:
        return ActionCandidate(candidate_id="play:0", description="打出第一张牌",
                               metadata={"action_type": "play", "indices": [0]})

    plays = [c for c in candidates if c.metadata.get("action_type") == "play"]
    safe_plays = [c for c in plays if not c.metadata.get("fatal_violation", False)]
    clears = [c for c in safe_plays if c.metadata.get("clears")]

    # 1. Prefer guaranteed clearing safe play
    if clears:
        return max(clears, key=lambda c: c.metadata.get("score", 0))

    # 2. If jokers need reordering to maximize multipliers, prioritize reorder before playing
    reorders = [c for c in candidates if c.metadata.get("action_type") == "reorder_jokers"]
    if reorders:
        return max(reorders, key=lambda c: c.heuristic_score)

    # 3. If safe plays exist, take the highest score
    if safe_plays:
        return max(safe_plays, key=lambda c: c.metadata.get("score", 0))

    # 3. If in shop phase, prefer legal buy with highest score or toggle_shop
    shop_actions = [c for c in candidates if c.metadata.get("action_type") in ("buy_joker", "toggle_shop")]
    if shop_actions:
        return max(shop_actions, key=lambda c: c.heuristic_score)

    return max(candidates, key=lambda c: c.heuristic_score)
