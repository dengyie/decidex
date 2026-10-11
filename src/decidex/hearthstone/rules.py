"""
Hearthstone legal-action enumerator.

Pure rules: given a BoardState, produce the complete set of legal actions for
the acting player, plus per-action structured metadata. This is the layer that
decidex's README calls "hard rule validation" (mana crystal constraints, taunt
absorption, kill-line projection) and is the single biggest gap in the wider
Hearthstone automation ecosystem.

Rule fidelity is deliberately scoped: it models constructed-play turn structure
(mana gating, summoning sickness, taunt, targeting, hero power, end-turn) but
does NOT implement every card mechanic. It is the deterministic backbone behind
the remote decision model, so the model only ever picks among legal actions.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from decidex.types import ActionCandidate

from .board import BoardState, HandCard, Minion

# Action id prefixes keep candidate ids stable and greppable.
PLAY = "play"
ATTACK = "attack"
HERO_ATTACK = "hero_attack"
HERO_POWER = "hero_power"
END_TURN = "end_turn"


def _opp_has_taunt(state: BoardState) -> bool:
    return any(m.taunt for m in state.opp_minions)


def _enemy_targets(state: BoardState) -> List[str]:
    """Targets an attacker may hit, honouring the taunt absorption rule."""
    if _opp_has_taunt(state):
        return [m.id for m in state.opp_minions if m.taunt]
    ids = [m.id for m in state.opp_minions]
    ids.append(state.opp_hero.name)
    return ids


def _resolve_targets(card: HandCard, state: BoardState) -> List[str]:
    """Targets a targeted card may legally select."""
    if not card.target_scopes:
        return []

    scopes = set(card.target_scopes)
    targets: List[str] = []
    enemy_minions = state.opp_minions
    friendly_minions = state.my_minions
    # Taunt does NOT block spells/battlecry targeting.
    if "enemy_minion" in scopes or "any_minion" in scopes:
        targets.extend(m.id for m in enemy_minions)
    if "friendly_minion" in scopes or "any_minion" in scopes:
        targets.extend(m.id for m in friendly_minions)
    if "enemy_hero" in scopes:
        targets.append(state.opp_hero.name)
    if "friendly_hero" in scopes:
        targets.append(state.my_hero.name)
    return targets


def _play_candidates(state: BoardState) -> List[ActionCandidate]:
    """All playable cards in hand, with targeting (or not) resolved."""
    out: List[ActionCandidate] = []
    for c in state.hand:
        if c.cost > state.mana:
            continue
        targets = _resolve_targets(c, state)
        if targets:
            # A targeted play produces one candidate per legal target.
            for t in targets:
                out.append(ActionCandidate(
                    candidate_id=f"{PLAY}:{c.id}->{t}",
                    description=f"Play {c.name} (cost {c.cost}) targeting {t}",
                    heuristic_score=_card_play_heuristic(c, state),
                    metadata={"action_type": "play", "card": c.id, "name": c.name, "target": t},
                ))
        else:
            out.append(ActionCandidate(
                candidate_id=f"{PLAY}:{c.id}",
                description=f"Play {c.name} (cost {c.cost})",
                heuristic_score=_card_play_heuristic(c, state),
                metadata={"action_type": "play", "card": c.id, "name": c.name},
            ))
    return out


def _attack_candidates(state: BoardState) -> List[ActionCandidate]:
    """Minion and hero attacks. Minions with summoning sickness cannot attack."""
    out: List[ActionCandidate] = []
    enemy = _enemy_targets(state)
    taunt_ids = {m.id for m in state.opp_minions if m.taunt}
    for m in state.my_minions:
        if not m.can_attack or m.attack <= 0:
            continue
        for t in enemy:
            out.append(ActionCandidate(
                candidate_id=f"{ATTACK}:{m.id}->{t}",
                description=f"Attack {t} with {m.name} ({m.attack}/{m.health})",
                heuristic_score=_attack_heuristic(m.attack, t, state),
                metadata={
                    "action_type": "attack",
                    "attacker": m.id,
                    "name": m.name,
                    "target": t,
                    "target_is_taunt": t in taunt_ids,
                },
            ))
    if state.my_hero.can_attack and state.my_hero.attack > 0:
        for t in enemy:
            out.append(ActionCandidate(
                candidate_id=f"{HERO_ATTACK}:->{t}",
                description=f"Attack {t} with hero ({state.my_hero.attack} attack)",
                heuristic_score=_attack_heuristic(state.my_hero.attack, t, state),
                metadata={
                    "action_type": "hero_attack",
                    "target": t,
                    "target_is_taunt": t in taunt_ids,
                },
            ))
    return out


def _hero_power_candidate(state: BoardState) -> List[ActionCandidate]:
    if not state.my_hero.hero_power_usable:
        return []
    if state.mana < state.my_hero.hero_power_cost:
        return []
    return [ActionCandidate(
        candidate_id=HERO_POWER,
        description=f"Use hero power ({state.my_hero.name})",
        heuristic_score=0.2,
        metadata={"action_type": "hero_power"},
    )]


def _end_turn_candidate() -> List[ActionCandidate]:
    return [ActionCandidate(
        candidate_id=END_TURN,
        description="End turn",
        heuristic_score=-0.05,  # lowest priority unless nothing useful remains
        metadata={"action_type": "end_turn"},
    )]


def _card_play_heuristic(card: HandCard, state: BoardState) -> float:
    """Rough prior for candidate pre-ranking (model can override it)."""
    score = 0.0
    if card.card_type == "minion":
        score += 0.3 * (card.attack + card.health) / 6.0
        if card.charge:
            score += 0.5
        if card.taunt:
            score += 0.4
    elif card.card_type == "weapon":
        score += 0.6
    else:
        score += 0.3
    # Spending less mana to develop is generally preferred early.
    score += 0.15 * (1.0 - card.cost / max(1, state.max_mana))
    return round(score, 4)


def _attack_heuristic(attack: int, target_id: str, state: BoardState) -> float:
    """Favours removing threats over face, and removing taunt to unlock face."""
    target: Optional[Minion] = next((m for m in state.opp_minions if m.id == target_id), None)
    if target is None:
        return 0.1  # face
    score = 0.2 + 0.3 * min(target.attack, 6) / 6.0
    if target.taunt:
        score += 0.3
    return round(score, 4)


def enumerate_legal_actions(state: BoardState) -> List[ActionCandidate]:
    """
    Deterministic legal-action generation for the acting player.
    Always includes end_turn; return order is stable (plays, attacks, hero power,
    end turn) so candidate ids are reproducible across identical states.
    """
    candidates: List[ActionCandidate] = []
    candidates.extend(_play_candidates(state))
    candidates.extend(_attack_candidates(state))
    candidates.extend(_hero_power_candidate(state))
    candidates.extend(_end_turn_candidate())
    return candidates


def legal_action_ids(candidates: List[ActionCandidate]) -> List[str]:
    return [c.candidate_id for c in candidates]


def candidate_lookup(candidates: List[ActionCandidate]) -> Dict[str, ActionCandidate]:
    return {c.candidate_id: c for c in candidates}


def choose_safe_default(candidates: List[ActionCandidate]) -> ActionCandidate:
    """
    Deterministic fallback when the model returns nothing usable.
    Policy: clear taunt if any, else trade into the highest-attack enemy minion,
    else end turn. Never picks an action the model would be unsafe to guess.
    """
    if not candidates:
        return ActionCandidate(candidate_id=END_TURN, description="End turn", metadata={"action_type": "end_turn"})

    attacks = [c for c in candidates if c.metadata.get("action_type") in ("attack", "hero_attack")]
    # Policy: clear taunt first, else best removal, else best play, else end turn.
    taunt_attacks = [c for c in attacks if c.metadata.get("target_is_taunt")]
    if taunt_attacks:
        return max(taunt_attacks, key=lambda c: c.heuristic_score)
    if attacks:
        return max(attacks, key=lambda c: c.heuristic_score)
    return max(candidates, key=lambda c: c.heuristic_score)
