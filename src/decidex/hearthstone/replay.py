"""
Replay harness: runs the HearthstoneAdvisor against real game snapshots from Power.log.

Compares the advisor's recommendation (from either rules-enumerated or game-provided
legal actions) against what the human actually chose.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from decidex.providers.mock import MockReplayProvider
from decidex.types import ActionCandidate, ObservationPayload, QuestionSpec

from .advisor import HearthstoneAdvisor, Recommendation
from .board import BoardState
from .perception import BoardSnapshot, RawGameOption, RawChoice, HearthstoneGameReader


# ---------------------------------------------------------------------------
# Mapping: real game Options → DecideX ActionCandidate list
# ---------------------------------------------------------------------------

def game_options_to_candidates(snapshot: BoardSnapshot) -> List[ActionCandidate]:
    """
    Map the game engine's real legal Options into ActionCandidate objects
    with ids consistent with rules.py conventions (play:, attack:, hero_attack:,
    hero_power, end_turn). This gives us the authoritative legal envelope
    as determined by the real game client.
    """
    board = snapshot.board

    # Build lookup maps for quick zone classification of game entities
    friendly_minion_ids: Dict[int, str] = {}  # entity_id -> candidate_id prefix
    for m in board.my_minions:
        # Minion.id = "e{entity_id}" -> extract numeric entity_id
        eid = int(m.id[1:])
        friendly_minion_ids[eid] = m.id

    hand_card_ids: Dict[int, str] = {}
    for c in board.hand:
        eid = int(c.id[1:])
        hand_card_ids[eid] = c.id

    # Determine friendly hero entity ID
    friendly_hero_eid: Optional[int] = None
    opp_hero_eid: Optional[int] = None
    for o in snapshot.options:
        for t in o.targets:
            name = t.get("name", "")
            if name == board.my_hero.name:
                friendly_hero_eid = t["entity_id"]
            elif name == board.opp_hero.name:
                opp_hero_eid = t["entity_id"]

    candidates: List[ActionCandidate] = []

    for opt in snapshot.options:
        eid = opt.main_entity_id
        tgt_entities = [t["entity_id"] for t in opt.targets]

        if opt.option_type == "END_TURN":
            candidates.append(ActionCandidate(
                candidate_id="end_turn",
                description="End turn",
                heuristic_score=-0.05,
                metadata={"action_type": "end_turn"},
            ))

        elif opt.option_type == "POWER":
            # Classify: hand card play, minion attack, hero power, or hero attack

            if eid in hand_card_ids:
                # --- Play card from hand ---
                card_id = hand_card_ids[eid]
                if tgt_entities:
                    for teid in tgt_entities:
                        tgt_name = _resolve_target_name(teid, snapshot)
                        candidates.append(ActionCandidate(
                            candidate_id=f"play:{card_id}->{tgt_name}",
                            description=f"Play {card_id} targeting {tgt_name}",
                            heuristic_score=0.5,
                            metadata={
                                "action_type": "play",
                                "card": card_id,
                                "target": tgt_name.split("→")[-1].split(" (")[0],
                            },
                        ))
                else:
                    candidates.append(ActionCandidate(
                        candidate_id=f"play:{card_id}",
                        description=f"Play {card_id}",
                        heuristic_score=0.5,
                        metadata={"action_type": "play", "card": card_id},
                    ))

            elif eid in friendly_minion_ids:
                # --- Minion attack ---
                minion_id = friendly_minion_ids[eid]
                for teid in tgt_entities:
                    tgt_name = _resolve_target_name(teid, snapshot)
                    candidates.append(ActionCandidate(
                        candidate_id=f"attack:{minion_id}->{tgt_name}",
                        description=f"Attack {tgt_name} with {minion_id}",
                        heuristic_score=_minion_attack_score(eid, snapshot),
                        metadata={
                            "action_type": "attack",
                            "attacker": minion_id,
                            "target": tgt_name,
                            "target_is_taunt": _is_taunt_target(teid, snapshot),
                        },
                    ))

            elif friendly_hero_eid is not None and eid == friendly_hero_eid:
                # --- Hero attack (weapon swing) ---
                for teid in tgt_entities:
                    tgt_name = _resolve_target_name(teid, snapshot)
                    candidates.append(ActionCandidate(
                        candidate_id=f"hero_attack:->{tgt_name}",
                        description=f"Attack {tgt_name} with hero",
                        heuristic_score=0.3,
                        metadata={
                            "action_type": "hero_attack",
                            "target": tgt_name,
                            "target_is_taunt": _is_taunt_target(teid, snapshot),
                        },
                    ))

            else:
                # --- Hero power (or unknown) ---
                # Hero power entity is typically not in friendly_minion_ids nor hand_card_ids
                hp_card_id = opt.card_id or f"hp{eid}"
                if tgt_entities:
                    for teid in tgt_entities:
                        tgt_name = _resolve_target_name(teid, snapshot)
                        candidates.append(ActionCandidate(
                            candidate_id=f"hero_power->{tgt_name}",
                            description=f"Use hero power on {tgt_name}",
                            heuristic_score=0.2,
                            metadata={
                                "action_type": "hero_power",
                                "target": tgt_name,
                            },
                        ))
                else:
                    candidates.append(ActionCandidate(
                        candidate_id="hero_power",
                        description=f"Use hero power ({hp_card_id})",
                        heuristic_score=0.2,
                        metadata={"action_type": "hero_power"},
                    ))

    return candidates


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_target_name(entity_id: int, snapshot: BoardSnapshot) -> str:
    """Resolve a game entity ID to a human-usable target name."""
    opp = snapshot.board.opp_hero
    if opp.name and str(entity_id) in opp.name:
        return opp.name  # unlikely but safe

    # Check if it matches opponent hero
    for t in snapshot.options:
        for sub in t.targets:
            if sub["entity_id"] == entity_id:
                name = sub.get("name") or sub.get("card_id") or f"e{entity_id}"
                return name

    # Check friendly/opponent minions
    for m in snapshot.board.opp_minions:
        if m.id == f"e{entity_id}":
            return m.name
    for m in snapshot.board.my_minions:
        if m.id == f"e{entity_id}":
            return m.name

    return f"e{entity_id}"


def _is_taunt_target(entity_id: int, snapshot: BoardSnapshot) -> bool:
    for m in snapshot.board.opp_minions:
        if m.id == f"e{entity_id}":
            return m.taunt
    return False


def _minion_attack_score(entity_id: int, snapshot: BoardSnapshot) -> float:
    """Score an attack action by threat value of the target."""
    # Simple heuristic favouring high-attack target removal
    for o in snapshot.options:
        for t in o.targets:
            if t["entity_id"] == entity_id:
                name = t.get("name", "")
                for m in snapshot.board.opp_minions:
                    if m.id == f"e{entity_id}":
                        return 0.2 + 0.3 * min(m.attack, 6) / 6.0 + (0.3 if m.taunt else 0)
                # face
                return 0.1
    return 0.15


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------

@dataclass
class TurnReport:
    """Result of one advisor turn against a real-game snapshot."""
    turn: int
    mana: int
    max_mana: int
    board_summary: str
    game_option_count: int
    advisor_candidate: str
    advisor_action: str
    advisor_confidence: float
    advisor_fallback: bool
    human_candidate: str
    human_action_type: str
    agreed: bool

    @property
    def verdict(self) -> str:
        if self.agreed:
            return "✓ MATCH"
        return "✗ DIFF"


async def replay_game(
    log_path: str,
    friendly_player_id: Optional[int] = None,
    solver_fn: Optional[Callable] = None,
    top_k: int = 12,
    min_confidence: float = 0.30,  # lowered to let more through
    min_margin: float = 0.01,
) -> List[TurnReport]:
    """
    Replay a real game log through the HearthstoneAdvisor.

    Uses the game's own Options (authoritative legal envelope) as the action set
    and compares each advisor recommendation against what the human actually did.

    Args:
        log_path: Path to a Power.log file.
        friendly_player_id: Manual override for the local player (auto-detected if None).
        solver_fn: Optional scoring function for MockReplayProvider. Defaults to
            a simple heuristic that prefers attacks over plays over hero power over
            end turn.
        top_k: Max number of candidates shown to the model.
        min_confidence: Lowered guard threshold (real-game options can be large).
        min_margin: Lowered guard threshold.

    Returns:
        List of TurnReport, one per decision point.
    """
    if solver_fn is None:
        solver_fn = _default_heuristic_solver

    provider = MockReplayProvider(solver_fn=solver_fn, simulated_latency_ms=1)
    advisor = HearthstoneAdvisor(
        provider=provider,
        top_k=top_k,
        min_confidence=min_confidence,
        min_margin=min_margin,
    )

    snapshots = HearthstoneGameReader(
        log_path, friendly_player_id=friendly_player_id
    ).parse()

    reports: List[TurnReport] = []
    for snap in snapshots:
        board = snap.board
        game_candidates = game_options_to_candidates(snap)

        # Pin the advisor to the game engine's own legal envelope.
        rec = await advisor.recommend(board, legal_actions=game_candidates)

        human_cid, human_action = _map_chosen(snap)

        # Determine if the advisor's pick matches the human's action.
        adv_id = rec.candidate_id
        agreed = _same_action(adv_id, human_cid)

        # Board summary
        mine = ", ".join(
            f"{m.name}({m.attack}/{m.health})" for m in board.my_minions
        ) or "none"
        opp = ", ".join(
            f"{m.name}({m.attack}/{m.health}{'T' if m.taunt else ''})"
            for m in board.opp_minions
        ) or "none"

        reports.append(TurnReport(
            turn=board.turn,
            mana=board.mana,
            max_mana=board.max_mana,
            board_summary=f"my=[{mine}] opp=[{opp}] hand={len(board.hand)}",
            game_option_count=len(snap.options),
            advisor_candidate=rec.candidate_id,
            advisor_action=rec.description,
            advisor_confidence=rec.confidence,
            advisor_fallback=rec.is_fallback,
            human_candidate=human_cid,
            human_action_type=human_action,
            agreed=agreed,
        ))

    advisor.close()
    return reports


def _map_chosen(snap: BoardSnapshot) -> Tuple[str, str]:
    """Map the human's SendOption to (candidate_id, action_type)."""
    if snap.chosen is None:
        return "-", "-"

    chosen: RawChoice = snap.chosen

    # Build the authoritative candidate list and match by structure.
    # We match by the chosen option index against rule-smart reconstruction.
    if chosen.option_index < len(snap.options):
        opt = snap.options[chosen.option_index]
        main_eid = opt.main_entity_id
        tgt = chosen.target_entity_id

        # Classify by zone as in game_options_to_candidates
        board = snap.board
        is_hand = any(c.id == f"h{main_eid}" for c in board.hand)
        is_minion = any(m.id == f"e{main_eid}" for m in board.my_minions)

        if opt.option_type == "END_TURN":
            return "end_turn", "end_turn"

        if opt.option_type == "POWER":
            if is_hand:
                card_id = f"h{main_eid}"
                if tgt and tgt != 0:
                    tgt_name = _resolve_target_name(tgt, snap)
                    return f"play:{card_id}->{tgt_name}", "play"
                return f"play:{card_id}", "play"
            if is_minion:
                minion_id = f"e{main_eid}"
                tgt_name = _resolve_target_name(tgt, snap) if tgt else "?"
                return f"attack:{minion_id}->{tgt_name}", "attack"
            # hero power / hero attack fallthrough
            if tgt and tgt != 0:
                tgt_name = _resolve_target_name(tgt, snap)
                return f"hero_power->{tgt_name}", "hero_power"
            return "hero_power", "hero_power"

        if opt.option_type == "ATTACK":
            minion_id = f"e{main_eid}"
            tgt_name = _resolve_target_name(tgt, snap) if tgt else "?"
            return f"attack:{minion_id}->{tgt_name}", "attack"

    return f"opt{chosen.option_index}→e{chosen.target_entity_id}", "?"


def _same_action(advisor_id: str, human_id: str) -> bool:
    """Compare advisor vs human action, tolerant of hero-power target mismatch (common)."""
    if advisor_id == human_id:
        return True
    if human_id == "-" or advisor_id == "-":
        return False
    # Normalize: match the action prefix and target entity
    def norm(aid: str) -> Tuple[str, str]:
        if aid.startswith("attack:") or aid.startswith("play:") or aid.startswith("hero_attack:"):
            body = aid.split(":", 1)[1]
            if "->" in body:
                left, right = body.split("->", 1)
                return (aid.split(":", 1)[0], right)
            return (aid.split(":", 1)[0], body)
        if aid.startswith("hero_power"):
            return ("hero_power", aid.split("->")[-1] if "->" in aid else "")
        if aid.startswith("end_turn"):
            return ("end_turn", "")
        return (aid, "")

    a_type, a_tgt = norm(advisor_id)
    h_type, h_tgt = norm(human_id)
    if a_type != h_type:
        return False
    if a_tgt and h_tgt and a_tgt != h_tgt:
        return False
    return True


def _default_heuristic_solver(
    payload: ObservationPayload, question: QuestionSpec
) -> Tuple[Any, float, Dict[str, float]]:
    """Simple heuristic: prefer attack > play > hero_power > end_turn."""
    opts = question.options or []
    if not opts:
        return "end_turn", 0.50, {}
    for prefix in ("attack:", "play:", "hero_attack:", "hero_power", "end_turn"):
        matches = [o for o in opts if o.startswith(prefix)]
        if matches:
            selected = matches[0]
            conf = 0.75
            dist = {o: (conf if o == selected else (1 - conf) / max(1, len(opts) - 1)) for o in opts}
            return selected, conf, dist
    selected = opts[0]
    return selected, 0.60, {o: 1.0 / len(opts) for o in opts}