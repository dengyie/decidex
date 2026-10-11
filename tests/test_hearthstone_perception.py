"""
Tests for perception adapter (real Power.log → BoardState) and replay harness.

These tests require hslog (pip install hslog) and a real Power.log file.
The test data is a small real game from HearthSim/hsreplay-test-data.
"""

import os
import logging

import pytest

from decidex.hearthstone.board import BoardState, Hero, Minion, HandCard

# ---------------------------------------------------------------------------
# Skip guards
# ---------------------------------------------------------------------------

HAS_HSLOG = False
try:
    from decidex.hearthstone.perception import (
        BoardSnapshot,
        HearthstoneGameReader,
        load_game_snapshots,
        RawGameOption,
        RawChoice,
    )
    from decidex.hearthstone.replay import (
        TurnReport,
        game_options_to_candidates,
        replay_game,
    )
    HAS_HSLOG = True
except ImportError:
    pass

REPLAY_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "replays")

pytestmark = pytest.mark.skipif(not HAS_HSLOG, reason="hslog not installed")


def _has_replay(name: str) -> bool:
    return os.path.isfile(os.path.join(REPLAY_DATA_DIR, name))


# ---------------------------------------------------------------------------
# Perception tests
# ---------------------------------------------------------------------------

class TestParseRealLog:
    """Perception adapter on real Power.log."""

    @pytest.mark.skipif(not _has_replay("13619.log"), reason="13619.log not downloaded")
    def test_parse_mage_warrior_log(self):
        """13619.log is a complete Mage vs Warrior game with 24 decision points."""
        path = os.path.join(REPLAY_DATA_DIR, "13619.log")
        snaps = load_game_snapshots(path)

        assert len(snaps) == 24, f"expected 24 decision snapshots, got {len(snaps)}"

        # Verify first snapshot (turn 1, mulligan end → only END_TURN)
        s0 = snaps[0]
        assert s0.turn == 1
        assert s0.mana == 1
        assert len(s0.options) == 1
        assert s0.options[0].option_type == "END_TURN"
        assert s0.chosen is not None and s0.chosen.option_index == 0
        assert s0.board.deck_count == 26

        # Verify that board states are complete
        for s in snaps:
            assert s.board.turn > 0
            assert s.board.my_hero.health > 0
            assert s.board.opp_hero.health > 0
            assert len(s.board.hand) >= 0
            assert s.board.deck_count >= 0
            # Every snapshot must have at least END_TURN
            assert any(o.option_type == "END_TURN" for o in s.options)

    @pytest.mark.skipif(not _has_replay("13619.log"), reason="13619.log not downloaded")
    def test_snapshot_has_play_options(self):
        """Mid-game snapshots (e.g., turn 7) have multiple POWER play options."""
        path = os.path.join(REPLAY_DATA_DIR, "13619.log")
        snaps = load_game_snapshots(path)
        s7 = snaps[5]  # turn 7 with enemy minions
        assert s7.turn == 7
        assert len(s7.options) == 7
        power_opts = [o for o in s7.options if o.option_type == "POWER"]
        assert len(power_opts) == 6

    @pytest.mark.skipif(not _has_replay("13619.log"), reason="13619.log not downloaded")
    def test_board_extracts_minions(self):
        """The board state correctly extracts friendly and enemy minions."""
        path = os.path.join(REPLAY_DATA_DIR, "13619.log")
        snaps = load_game_snapshots(path)

        # Snapshot 5 (turn 7): opponent has OG_314b + CS2_173
        s5 = snaps[5]
        opp_ids = [m.id for m in s5.board.opp_minions]
        assert len(opp_ids) == 2

        # Snapshot 7: friendly has OG_281 on board
        s7 = snaps[7]
        my_ids = [m.id for m in s7.board.my_minions]
        assert len(my_ids) == 1

    @pytest.mark.skipif(not _has_replay("13619.log"), reason="13619.log not downloaded")
    def test_mana_tracking(self):
        """Mana is correctly tracked (spending hero power subtracts 2)."""
        path = os.path.join(REPLAY_DATA_DIR, "13619.log")
        snaps = load_game_snapshots(path)

        # Turn 3: before hero power (2/2) and after (0/2)
        s_before = snaps[1]
        s_after = snaps[2]
        assert s_before.mana == 2 and s_before.max_mana == 2
        assert s_after.mana == 0 and s_after.max_mana == 2

    @pytest.mark.skipif(not _has_replay("13619.log"), reason="13619.log not downloaded")
    def test_chosen_action_recorded(self):
        """Each snapshot records what the human actually chose."""
        path = os.path.join(REPLAY_DATA_DIR, "13619.log")
        snaps = load_game_snapshots(path)

        with_problems = [s for s in snaps if s.chosen is None]
        # Some snapshots (after playing last card) may not have a chosen entry
        # in the log — that's fine as long as most do.
        assert len(snaps) == 24
        assert len(with_problems) < 5, f"too many snapshots missing chosen: {len(with_problems)}"

    def test_parse_nonexistent_log(self):
        """Non-existent log path raises clear error."""
        with pytest.raises(FileNotFoundError):
            load_game_snapshots("/nonexistent/power.log")


# ---------------------------------------------------------------------------
# Replay tests
# ---------------------------------------------------------------------------

class TestGameOptionsToCandidates:
    """Mapping game Options → ActionCandidate list."""

    @pytest.mark.skipif(not _has_replay("13619.log"), reason="13619.log not downloaded")
    def test_end_turn_only_snapshot(self):
        """Turn 1 snapshot: only END_TURN, maps to single end_turn candidate."""
        path = os.path.join(REPLAY_DATA_DIR, "13619.log")
        snaps = load_game_snapshots(path)
        cands = game_options_to_candidates(snaps[0])
        assert len(cands) == 1
        assert cands[0].candidate_id == "end_turn"
        assert cands[0].metadata["action_type"] == "end_turn"

    @pytest.mark.skipif(not _has_replay("13619.log"), reason="13619.log not downloaded")
    def test_has_attack_candidates(self):
        """Snapshot 12 (turn 11): minion attacks appear in candidates."""
        path = os.path.join(REPLAY_DATA_DIR, "13619.log")
        snaps = load_game_snapshots(path)
        cands = game_options_to_candidates(snaps[12])
        attacks = [c for c in cands if c.metadata.get("action_type") == "attack"]
        assert len(attacks) > 0, "turn 11 should have minion attacks"

    @pytest.mark.skipif(not _has_replay("13619.log"), reason="13619.log not downloaded")
    def test_attack_taunt_enforced(self):
        """When taunts exist, attack targets are only taunts."""
        path = os.path.join(REPLAY_DATA_DIR, "13619.log")
        snaps = load_game_snapshots(path)
        cands = game_options_to_candidates(snaps[12])
        attacks = [c for c in cands if c.metadata.get("action_type") == "attack"]

        # All attack targets should be taunt minions (EX1_390, CS2_187)
        for a in attacks:
            meta = a.metadata
            tgt = meta.get("target", "")
            assert "EX1_390" in tgt or "CS2_187" in tgt, f"attack target {tgt} not a taunt"


class TestReplayGame:
    """End-to-end replay harness."""

    @pytest.mark.skipif(not _has_replay("13619.log"), reason="13619.log not downloaded")
    @pytest.mark.asyncio
    async def test_replay_produces_reports(self):
        """Replay returns a TurnReport per decision point."""
        path = os.path.join(REPLAY_DATA_DIR, "13619.log")
        reports = await replay_game(path)
        assert len(reports) == 24
        for r in reports:
            assert r.turn > 0
            assert r.mana >= 0
            assert r.board_summary
            assert r.advisor_candidate
            assert r.advisor_confidence > 0
            assert r.human_candidate

    @pytest.mark.skipif(not _has_replay("13619.log"), reason="13619.log not downloaded")
    @pytest.mark.asyncio
    async def test_replay_end_turn_agreements(self):
        """End-turn decisions are reliably matched (advisor agrees when human ends)."""
        path = os.path.join(REPLAY_DATA_DIR, "13619.log")
        reports = await replay_game(path)
        end_turns = [r for r in reports if r.human_candidate.startswith("end_turn")]
        assert len(end_turns) > 4, "game should have multiple end-turn decisions"
        agreements = [r for r in end_turns if r.agreed]
        # The heuristic solver picks end_turn when nothing better (mana=0).
        # Most end-turn decisions should match.
        assert len(agreements) >= len(end_turns) - 2