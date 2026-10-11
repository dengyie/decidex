"""
Perception adapter: read real Hearthstone Power.log → BoardState snapshots.

Uses hslog (HearthSim Log Parser) to replay game log and capture board state
at each decision point (Options packet). The game's own legal action enumeration
(Options) is preserved alongside the reconstructed BoardState for comparison.

This is read-only: it parses a game log file; it does not interact with the client.
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from hslog import LogParser
from hslog.export import EntityTreeExporter
from hslog.packets import Options as OptionsPacket, Option, SendOption, PacketTree
from hearthstone.enums import GameTag, Zone, CardType, OptionType
from hearthstone.entities import Game as HSGame, Player as HSPlayer

from decidex.hearthstone.board import BoardState, Hero, Minion, HandCard

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Snapshot data structures
# ---------------------------------------------------------------------------

@dataclass
class RawGameOption:
    """A single legal option as reported by the real game engine."""
    option_index: int
    option_type: str          # "POWER", "END_TURN", "ATTACK", ...
    main_entity_id: int       # entity id of the card / hero power being used
    card_id: str = ""
    main_entity_name: str = ""
    targets: List[Dict[str, Any]] = field(default_factory=list)
    # each target dict: {"entity_id": int, "card_id": str, "name": str}


@dataclass
class RawChoice:
    """What the player actually chose (SendOption)."""
    option_index: int
    target_entity_id: int
    position: int


@dataclass
class BoardSnapshot:
    """A complete board state at one decision point, from real game data."""
    ts_str: str                     # raw timestamp string from log
    turn: int
    friendly_player_id: int
    mana: int                       # available mana
    max_mana: int
    board: BoardState               # reconstructed DecideX BoardState
    options: List[RawGameOption]    # game's own legal action list
    chosen: Optional[RawChoice] = None  # what the player chose (None if not yet)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TAG = GameTag  # shorthand

def _entity_to_dict(entity, include_tags: bool = False) -> Dict[str, Any]:
    """Extract essential fields from a hslog Card/Player entity."""
    d = {
        "entity_id": entity.id,
        "card_id": getattr(entity, "card_id", None) or "",
        "tags": {},
    }
    if include_tags and hasattr(entity, "tags"):
        for tag, value in entity.tags.items():
            try:
                tag_name = _TAG(tag).name
            except (ValueError, TypeError):
                tag_name = str(tag)
            d["tags"][tag_name] = value
    return d


def _safe_int(val) -> int:
    if val is None:
        return 0
    return int(val)


def _safe_str(val) -> str:
    if val is None:
        return ""
    return str(val)


# ---------------------------------------------------------------------------
# Core reader
# ---------------------------------------------------------------------------

class HearthstoneGameReader:
    """
    Reads a real Power.log file and produces a list of BoardSnapshots,
    one per decision point in the game (friendly player turns).
    """

    def __init__(
        self,
        path: str,
        friendly_player_id: Optional[int] = None,
    ):
        self.path = path
        self.friendly_player_id = friendly_player_id
        self._parser = LogParser()
        self._packet_tree: Optional[PacketTree] = None
        self._hs_game: Optional[HSGame] = None
        self._friendly_player: Optional[HSPlayer] = None
        self.snapshots: List[BoardSnapshot] = []
        self._pending_options: Optional[List[RawGameOption]] = None

    # ---- parsing ----

    def parse(self) -> List[BoardSnapshot]:
        """Parse the log and return all snapshots across every game in the log."""
        raw = self._load_and_normalize()
        self._feed_raw(raw)
        self._parse_stream()
        return self.snapshots

    def _load_and_normalize(self) -> str:
        with open(self.path, encoding="utf-8") as f:
            raw = f.read()

        # Fix stray leading "r " from log corruption
        raw = re.sub(r"^(?=r [A-Z])r ", "", raw, flags=re.MULTILINE)

        # Normalise old "PowerTaskList.DebugPrint*" → "GameState.DebugPrint*"
        raw = raw.replace(
            "PowerTaskList.DebugPrintPower", "GameState.DebugPrintPower"
        )
        raw = raw.replace(
            "PowerTaskList.DebugPrintOptions", "GameState.DebugPrintOptions"
        )
        raw = raw.replace(
            "PowerTaskList.DebugPrintGame", "GameState.DebugPrintGame"
        )
        raw = raw.replace(
            "PowerTaskList.DebugPrintEntityChoices",
            "GameState.DebugPrintEntityChoices",
        )
        raw = raw.replace(
            "PowerTaskList.DebugPrintEntitiesChosen",
            "GameState.DebugPrintEntitiesChosen",
        )
        raw = raw.replace(
            "PowerTaskList.DebugPrintPowerList",
            "GameState.DebugPrintPowerList",
        )

        return raw

    def _feed_raw(self, raw: str) -> None:
        for line in raw.split("\n"):
            if not line.strip():
                continue
            try:
                self._parser.read_line(line)
            except Exception:
                # skip unparseable lines (non-fatal metadata noise etc.)
                pass
        self._parser.flush()

    # ---- stream replay ----

    def _parse_stream(self) -> None:
        if not self._parser.games:
            logger.warning("No games found in %s", self.path)
            return

        # A single Power.log can hold multiple PacketTrees (reconnect / quest
        # sub-games / mixed PowerTaskList+GameState logging). Process each one;
        # within a tree the entity state is rebuilt from CREATE_GAME onward.
        for packet_tree in self._parser.games:
            self._packet_tree = packet_tree
            exporter = _SnapshotExporter(
                packet_tree, self._parser._parsing_state.manager, self
            )
            exporter.export()

    # ---- entity-state extraction ----

    def _extract_hero(
        self, player: HSPlayer
    ) -> Hero:
        """Extract Hero dataclass from a HSPlayer (by finding hero entity)."""
        hero_entity = None
        for e in self._hs_game.entities:
            ctrl = e.tags.get(_TAG.CONTROLLER)
            ct = e.tags.get(_TAG.CARDTYPE)
            if ctrl == player.player_id and ct == CardType.HERO:
                hero_entity = e
                # Prefer current hero (not just the first)
                # Actually there's typically only 1 hero in PLAY
                if e.tags.get(_TAG.ZONE) == Zone.PLAY:
                    hero_entity = e
                    break

        if hero_entity is None:
            return Hero(name=getattr(player, "name", "") or "Hero", health=30, max_health=30)

        name = getattr(player, "name", None) or _safe_str(
            getattr(hero_entity, "card_id", "")
        )
        health_raw = hero_entity.tags.get(_TAG.HEALTH, 30)
        damage = hero_entity.tags.get(_TAG.DAMAGE, 0)
        health = _safe_int(health_raw) - _safe_int(damage)
        max_health = _safe_int(health_raw)
        armor = _safe_int(hero_entity.tags.get(_TAG.ARMOR, 0))
        atk = _safe_int(hero_entity.tags.get(_TAG.ATK, 0))
        exhausted = hero_entity.tags.get(_TAG.EXHAUSTED, 0)
        cant_attack = hero_entity.tags.get(_TAG.CANT_ATTACK, 0)
        num_attacks = hero_entity.tags.get(_TAG.NUM_ATTACKS_THIS_TURN, 0)

        can_attack = bool(atk > 0 and not exhausted and not cant_attack)

        return Hero(
            name=name,
            health=max(0, health),
            max_health=max(0, max_health),
            armor=armor,
            attack=atk,
            can_attack=can_attack,
            hero_power_cost=2,
            hero_power_usable=True,  # simplistic; real logic checks EXHAUSTED on hero power entity
        )

    def _extract_minions(self, player_id: int) -> List[Minion]:
        minions: List[Tuple[int, Minion]] = []  # (zone_pos, minion)
        for e in self._hs_game.entities:
            ctrl = e.tags.get(_TAG.CONTROLLER)
            ct = e.tags.get(_TAG.CARDTYPE)
            zone = e.tags.get(_TAG.ZONE)
            if ctrl != player_id:
                continue
            if ct != CardType.MINION:
                continue
            if zone != Zone.PLAY:
                continue

            health_raw = e.tags.get(_TAG.HEALTH, 0)
            damage = e.tags.get(_TAG.DAMAGE, 0)
            atk = _safe_int(e.tags.get(_TAG.ATK, 0))
            exhausted = bool(e.tags.get(_TAG.EXHAUSTED, 0))
            cant_attack = bool(e.tags.get(_TAG.CANT_ATTACK, 0))
            taunt = bool(e.tags.get(_TAG.TAUNT, 0))
            ds = bool(e.tags.get(_TAG.DIVINE_SHIELD, 0))
            zpos = _safe_int(e.tags.get(_TAG.ZONE_POSITION, 0))

            # frozen minions can't attack
            frozen = bool(e.tags.get(_TAG.FROZEN, 0))
            can_attack = not exhausted and not cant_attack and atk > 0 and not frozen

            m = Minion(
                id=f"e{e.id}",
                name=_safe_str(getattr(e, "card_id", "")) or f"minion_{e.id}",
                attack=atk,
                health=max(0, _safe_int(health_raw) - _safe_int(damage)),
                max_health=max(0, _safe_int(health_raw)),
                taunt=taunt,
                divine_shield=ds,
                can_attack=can_attack,
            )
            minions.append((zpos, m))

        minions.sort(key=lambda t: t[0])
        return [m for _, m in minions]

    def _extract_hand(self, player_id: int) -> List[HandCard]:
        cards: List[Tuple[int, HandCard]] = []  # (zone_pos, card)
        for e in self._hs_game.entities:
            ctrl = e.tags.get(_TAG.CONTROLLER)
            zone = e.tags.get(_TAG.ZONE)
            if ctrl != player_id or zone != Zone.HAND:
                continue
            ct = e.tags.get(_TAG.CARDTYPE)
            cid = _safe_str(getattr(e, "card_id", ""))
            cost = _safe_int(e.tags.get(_TAG.COST))
            atk = _safe_int(e.tags.get(_TAG.ATK))
            health_raw = e.tags.get(_TAG.HEALTH, 0)
            damage = e.tags.get(_TAG.DAMAGE, 0)
            hp = max(0, _safe_int(health_raw) - _safe_int(damage))
            taunt = bool(e.tags.get(_TAG.TAUNT, 0))
            charge = bool(e.tags.get(_TAG.CHARGE, 0))
            zpos = _safe_int(e.tags.get(_TAG.ZONE_POSITION, 0))

            hc = HandCard(
                id=f"h{e.id}",
                name=cid or f"card_{e.id}",
                cost=cost,
                card_type="spell" if ct == CardType.SPELL else "minion",
                attack=atk,
                health=hp,
                taunt=taunt,
                charge=charge,
            )
            cards.append((zpos, hc))

        cards.sort(key=lambda t: t[0])
        return [c for _, c in cards]

    def _extract_deck_count(self, player_id: int) -> int:
        count = 0
        for e in self._hs_game.entities:
            ctrl = e.tags.get(_TAG.CONTROLLER)
            zone = e.tags.get(_TAG.ZONE)
            if ctrl == player_id and zone == Zone.DECK:
                count += 1
        return count

    def _extract_hero_power_usable(self, player_id: int) -> bool:
        """Check if the hero power entity is usable."""
        for e in self._hs_game.entities:
            ctrl = e.tags.get(_TAG.CONTROLLER)
            ct = e.tags.get(_TAG.CARDTYPE)
            zone = e.tags.get(_TAG.ZONE)
            if ctrl != player_id or ct != CardType.HERO_POWER or zone != Zone.PLAY:
                continue
            exhausted = e.tags.get(_TAG.EXHAUSTED, 0)
            return not exhausted
        return True  # assume usable if not found

    def _build_board_state(self, friendly_player: HSPlayer, opp_player: HSPlayer) -> BoardState:
        turn = _safe_int(self._hs_game.tags.get(_TAG.TURN, 1))
        resources = _safe_int(friendly_player.tags.get(_TAG.RESOURCES, 0))
        resources_used = _safe_int(friendly_player.tags.get(_TAG.RESOURCES_USED, 0))
        temp_resources = _safe_int(friendly_player.tags.get(_TAG.TEMP_RESOURCES, 0))
        mana = max(0, resources - resources_used + temp_resources)
        max_mana = resources

        my_hero = self._extract_hero(friendly_player)
        my_hero.hero_power_usable = self._extract_hero_power_usable(
            friendly_player.player_id
        )
        opp_hero = self._extract_hero(opp_player)

        return BoardState(
            turn=turn,
            mana=mana,
            max_mana=max_mana,
            my_hero=my_hero,
            opp_hero=opp_hero,
            my_minions=self._extract_minions(friendly_player.player_id),
            opp_minions=self._extract_minions(opp_player.player_id),
            hand=self._extract_hand(friendly_player.player_id),
            deck_count=self._extract_deck_count(friendly_player.player_id),
            fatigue=_safe_int(friendly_player.tags.get(_TAG.FATIGUE, 0)),
        )

    # ---- snapshot building ----

    def _take_snapshot(self, options: OptionsPacket) -> None:
        if self._hs_game is None:
            return

        friendly = self._friendly_player
        if friendly is None:
            return

        # Find opponent player
        opp: Optional[HSPlayer] = None
        for e in self._hs_game.entities:
            if isinstance(e, HSPlayer) and e.player_id != friendly.player_id:
                opp = e
                break
        if opp is None:
            return

        board = self._build_board_state(friendly, opp)

        raw_options: List[RawGameOption] = []
        for opt in options.options:
            main_eid = opt.entity if hasattr(opt, "entity") and opt.entity else 0
            main_cid = ""
            main_name = ""
            # Try to look up the entity
            if main_eid and self._hs_game:
                try:
                    ent = self._hs_game.find_entity_by_id(main_eid)
                    if ent:
                        main_cid = getattr(ent, "card_id", "") or ""
                        main_name = getattr(ent, "name", "") or ""
                except Exception:
                    pass

            targets = []
            for sub in opt.options:
                t_eid = sub.entity if hasattr(sub, "entity") and sub.entity else 0
                t_cid = ""
                t_name = ""
                if t_eid and self._hs_game:
                    try:
                        ent = self._hs_game.find_entity_by_id(t_eid)
                        if ent:
                            t_cid = getattr(ent, "card_id", "") or ""
                            t_name = getattr(ent, "name", "") or ""
                    except Exception:
                        pass
                targets.append({
                    "entity_id": t_eid,
                    "card_id": t_cid,
                    "name": t_name,
                })

            # Map numeric option.type to string
            opt_type_str = str(opt.type) if opt.type is not None else "?"
            if hasattr(opt, "type") and isinstance(opt.type, int):
                try:
                    opt_type_str = OptionType(opt.type).name
                except (ValueError, TypeError):
                    opt_type_str = str(opt.type)

            raw_options.append(RawGameOption(
                option_index=opt.id,
                option_type=opt_type_str,
                main_entity_id=main_eid,
                card_id=main_cid,
                main_entity_name=main_name,
                targets=targets,
            ))

        ts_str = str(options.ts) if hasattr(options, "ts") else ""
        snap = BoardSnapshot(
            ts_str=ts_str,
            turn=board.turn,
            friendly_player_id=friendly.player_id,
            mana=board.mana,
            max_mana=board.max_mana,
            board=board,
            options=raw_options,
        )
        self.snapshots.append(snap)
        self._pending_options = raw_options

    def _record_send_option(self, send: SendOption) -> None:
        if self._pending_options and self.snapshots:
            self.snapshots[-1].chosen = RawChoice(
                option_index=send.option,
                target_entity_id=send.target,
                position=send.position,
            )
        # After recording, pending options consumed
        self._pending_options = None

    # ---- reader is the back-link for _take_snapshot ----
    pass  # self reference is in __init__


# ---------------------------------------------------------------------------
# Snapshot exporter – hooks into EntityTreeExporter to capture state
# ---------------------------------------------------------------------------

class _SnapshotExporter(EntityTreeExporter):
    """
    Extends EntityTreeExporter to snapshot board state at each Options packet
    and record the chosen action at each SendOption.
    """

    def __init__(self, packet_tree, player_manager, reader: HearthstoneGameReader):
        super().__init__(packet_tree, player_manager, tolerate_missing_entities=True)
        self._reader = reader
        self._reader._hs_game = None
        self._reader._friendly_player = None

    def handle_create_game(self, packet):
        result = super().handle_create_game(packet)
        self._reader._hs_game = self.game
        return result

    def handle_player(self, packet):
        result = super().handle_player(packet)
        # Try to identify friendly player (the human)
        if self._reader.friendly_player_id is not None:
            for e in self.game.entities:
                if isinstance(e, HSPlayer) and e.player_id == self._reader.friendly_player_id:
                    self._reader._friendly_player = e
                    break
        else:
            # Auto-detect: first player with known hand cards (from original log)
            # Use the players from game.entities
            players = [e for e in self.game.entities if isinstance(e, HSPlayer)]
            if players:
                self._reader._friendly_player = players[0]
        return result

    def handle_options(self, packet: OptionsPacket):
        self._reader._take_snapshot(packet)

    def handle_send_option(self, packet: SendOption):
        self._reader._record_send_option(packet)


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------

def load_game_snapshots(
    path: str, friendly_player_id: Optional[int] = None
) -> List[BoardSnapshot]:
    """
    Convenience function: parse a Power.log and return all decision-point snapshots.

    Args:
        path: Path to a Power.log file.
        friendly_player_id: Player ID (1 or 2) for the local/friendly player.
            If None, auto-detected as the first player.

    Returns:
        List of BoardSnapshot, one per friendly-player decision point.
    """
    reader = HearthstoneGameReader(path, friendly_player_id=friendly_player_id)
    return reader.parse()