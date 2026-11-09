"""Golden numerical tests for the browser Balatro scoring engine.

The historical build tests only assert that handler source strings exist --
they never verify the NUMBERS. Every scoring-accuracy bug (double-counted
conditional jokers, missing Blueprint copy semantics, suit-joker s_mult)
therefore sailed through CI. This suite executes the real evaluateHandScore
in Node against hand-computed vanilla values, so a scoring change that
drifts from the game's actual math fails loudly here first.
"""

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).parents[1]
APP_JS = REPO / "tools" / "balatro_wasm" / "web" / "app.js"
HARNESS = Path(__file__).parent / "js_scoring_harness.mjs"

node_exe = shutil.which("node")
pytestmark = pytest.mark.skipif(node_exe is None, reason="node not available")


def card(rank, suit, index, **extra):
    return {"rank": rank, "suit": suit, "index": index, **extra}


# Golden cases: expectations hand-computed against vanilla Balatro math
# (RANK_VALUES J/Q/K=10, A=11; level-1 hand table; jokers left-to-right
# with each +mult applied before its xmult).
GOLDEN_CASES = [
    # --- Conditional jokers must NOT double-count via the generic path ---
    {
        "name": "scholar_no_double_count",
        "fn": "evaluateHandScore",
        "args": [[card("A", "S", 0)],
                 [{"name": "Scholar", "extra": {"mult": 4, "chips": 20}}], {}, {}],
        "expect": {"chips": 36, "mult": 5, "totalScore": 180},
    },
    {
        "name": "walkie_no_double_count",
        "fn": "evaluateHandScore",
        "args": [[card("10", "H", 0), card("4", "S", 1)],
                 [{"name": "Walkie Talkie", "extra": {"chips": 10, "mult": 4}}], {}, {}],
        "expect": {"chips": 25, "mult": 5, "totalScore": 125},
    },
    {
        "name": "greedy_joker_uses_s_mult_3",
        "fn": "evaluateHandScore",
        "args": [[card("5", "D", 0), card("9", "D", 1)],
                 [{"name": "Greedy Joker", "extra": {"s_mult": 3, "suit": "Diamonds"}}], {}, {}],
        "expect": {"chips": 14, "mult": 4, "totalScore": 56},
    },
    # --- Blueprint copies the Joker to its RIGHT; Brainstorm the LEFTMOST ---
    {
        "name": "blueprint_copies_right_neighbor",
        "fn": "evaluateHandScore",
        "args": [[card("7", "S", 0), card("7", "H", 1)],
                 [{"name": "Blueprint"}, {"name": "Cavendish", "extra": {"Xmult": 3, "odds": 1000}}], {}, {}],
        "expect": {"chips": 24, "mult": 18, "totalScore": 432},
    },
    {
        "name": "brainstorm_copies_leftmost",
        "fn": "evaluateHandScore",
        "args": [[card("9", "D", 0)],
                 [{"name": "Joker", "mult": 4}, {"name": "Brainstorm"}], {}, {}],
        "expect": {"chips": 14, "mult": 9, "totalScore": 126},
    },
    {
        "name": "blueprint_without_donor_only_edition",
        "fn": "evaluateHandScore",
        "args": [[card("7", "S", 0), card("7", "H", 1)],
                 [{"name": "Cavendish", "extra": {"Xmult": 3, "odds": 1000}}, {"name": "Blueprint"}], {}, {}],
        "expect": {"chips": 24, "mult": 6, "totalScore": 144},
    },
    {
        # Blueprint -> Blueprint -> Cavendish: both copies resolve through the
        # chain (vanilla context.blueprint recursion).
        "name": "blueprint_chain_through_blueprint",
        "fn": "evaluateHandScore",
        "args": [[card("7", "S", 0), card("7", "H", 1)],
                 [{"name": "Blueprint"}, {"name": "Blueprint"}, {"name": "Cavendish", "extra": {"Xmult": 3, "odds": 1000}}], {}, {}],
        "expect": {"chips": 24, "mult": 54, "totalScore": 1296},
    },
    {
        # A leftmost Brainstorm has nothing to copy (vanilla: other_joker == self).
        "name": "brainstorm_leftmost_self_has_no_donor",
        "fn": "evaluateHandScore",
        "args": [[card("7", "S", 0), card("7", "H", 1)], [{"name": "Brainstorm"}], {}, {}],
        "expect": {"chips": 24, "mult": 2, "totalScore": 48},
    },
    {
        # A debuffed donor contributes nothing (vanilla calculate_joker is
        # gated on not self.debuff), and the debuffed joker itself is skipped.
        "name": "blueprint_debuffed_donor_contributes_nothing",
        "fn": "evaluateHandScore",
        "args": [[card("7", "S", 0), card("7", "H", 1)],
                 [{"name": "Cavendish", "extra": {"Xmult": 3, "odds": 1000}, "debuff": True}, {"name": "Blueprint"}], {}, {}],
        "expect": {"chips": 24, "mult": 2, "totalScore": 48},
    },
    {
        # The copy carries the donor's ABILITY only: donor edition (holo +10)
        # applies once to the donor itself, never to the Blueprint copy.
        "name": "donor_edition_does_not_leak_to_copy",
        "fn": "evaluateHandScore",
        "args": [[card("9", "D", 0)],
                 [{"name": "Blueprint"}, {"name": "Joker", "mult": 4, "edition": "holo"}], {}, {}],
        "expect": {"chips": 14, "mult": 19, "totalScore": 266},
    },
    {
        "name": "duo_fires_on_two_pair",
        "fn": "evaluateHandScore",
        "args": [[card("7", "S", 0), card("7", "H", 1), card("3", "D", 2), card("3", "C", 3)],
                 [{"name": "The Duo", "xmult": 2}], {}, {}],
        "expect": {"chips": 40, "mult": 4, "totalScore": 160},
    },
    # --- Held-in-hand pass ---
    {
        "name": "baron_counts_held_kings",
        "fn": "evaluateHandScore",
        "args": [[card("2", "D", 0), card("3", "C", 1)],
                 [{"name": "Baron", "extra": 1.5}],
                 {},
                 {"hand": [card("2", "D", 0), card("3", "C", 1), card("K", "S", 2),
                           card("K", "H", 3), card("4", "S", 4), card("5", "H", 5),
                           card("6", "D", 6), card("7", "C", 7)]}],
        "expect": {"chips": 8, "mult": 2.3, "totalScore": 18},
    },
    # --- New handlers driven by bridge-emitted counters ---
    {
        # Vanilla increments `played` before scoring: the current play counts.
        "name": "supernova_counts_current_play",
        "fn": "evaluateHandScore",
        "args": [[card("7", "S", 0), card("7", "H", 1)], [{"name": "Supernova"}], {},
                 {"hand_played": {"Pair": 3}}],
        "expect": {"chips": 24, "mult": 6, "totalScore": 144},
    },
    {
        "name": "supernova_first_play_counts",
        "fn": "evaluateHandScore",
        "args": [[card("7", "S", 0), card("7", "H", 1)], [{"name": "Supernova"}], {}, {}],
        "expect": {"chips": 24, "mult": 3, "totalScore": 72},
    },
    {
        "name": "card_sharp_needs_repeat_this_round",
        "fn": "evaluateHandScore",
        "args": [[card("7", "S", 0), card("7", "H", 1)],
                 [{"name": "Card Sharp", "extra": {"Xmult": 3}}], {},
                 {"hand_played_round": {"Pair": 2}}],
        "expect": {"chips": 24, "mult": 6, "totalScore": 144},
    },
    {
        # Pre-play count 1 => the current play is the 2nd of the type this
        # round; vanilla's `played_this_round > 1` (post-increment) fires.
        "name": "card_sharp_fires_on_second_play",
        "fn": "evaluateHandScore",
        "args": [[card("7", "S", 0), card("7", "H", 1)],
                 [{"name": "Card Sharp", "extra": {"Xmult": 3}}], {},
                 {"hand_played_round": {"Pair": 1}}],
        "expect": {"chips": 24, "mult": 6, "totalScore": 144},
    },
    {
        "name": "card_sharp_dormant_on_first_play",
        "fn": "evaluateHandScore",
        "args": [[card("7", "S", 0), card("7", "H", 1)],
                 [{"name": "Card Sharp", "extra": {"Xmult": 3}}], {},
                 {"hand_played_round": {"Pair": 0}}],
        "expect": {"chips": 24, "mult": 2, "totalScore": 48},
    },
    {
        "name": "erosion_counts_missing_deck_cards",
        "fn": "evaluateHandScore",
        "args": [[card("7", "S", 0), card("7", "H", 1)], [{"name": "Erosion", "extra": 4}], {},
                 {"starting_deck_size": 52, "deck_cards_total": 44}],
        "expect": {"chips": 24, "mult": 34, "totalScore": 816},
    },
    {
        "name": "fortune_teller_counts_tarots_used",
        "fn": "evaluateHandScore",
        "args": [[card("7", "S", 0), card("7", "H", 1)], [{"name": "Fortune Teller", "extra": 1}], {},
                 {"tarots_used": 5}],
        "expect": {"chips": 24, "mult": 7, "totalScore": 168},
    },
    {
        "name": "idol_hits_selected_rank_suit",
        "fn": "evaluateHandScore",
        "args": [[card("K", "S", 0), card("K", "D", 1)], [{"name": "The Idol"}], {},
                 {"idol_rank": "K", "idol_suit": "S"}],
        "expect": {"chips": 80, "mult": 4, "totalScore": 320},
    },
    {
        # Vanilla triggers PER matching card; DNA-copied duplicates can place
        # more than one copy of the selected rank+suit in a hand.
        "name": "idol_triggers_per_duplicate_card",
        "fn": "evaluateHandScore",
        "args": [[card("K", "S", 0), card("K", "S", 1)], [{"name": "The Idol"}], {},
                 {"idol_rank": "K", "idol_suit": "S"}],
        "expect": {"chips": 130, "mult": 8, "totalScore": 1040},
    },
    # --- Candidate ranking & arbitration ---
    {
        "name": "findBestPlays_prefers_pair_over_high_card",
        "fn": "findBestPlays",
        "args": [{"hand": [card("5", "H", 0), card("7", "S", 1), card("7", "H", 2),
                           card("9", "D", 3), card("4", "C", 4)],
                  "jokers": [], "blind": {"chips": 300, "scored": 0},
                  "hands_left": 4, "discards_left": 3}],
        "expect": {"handName": "Pair", "totalScore": 48},
    },
    {
        "name": "arbitrate_force_plays_when_beating_need",
        "fn": "arbitrateAction",
        "args": [
            {"blind": {"chips": 300, "scored": 0}, "hands_left": 4, "discards_left": 3, "jokers": []},
            {"handName": "Flush", "totalScore": 500, "cards": [card("H", "H", i) for i in range(5)]},
            None,
        ],
        "expect": {"verdict": "PLAY", "confidence": "CERTAIN"},
    },
    {
        "name": "arbitrate_discards_when_doomed",
        "fn": "arbitrateAction",
        "args": [
            {"blind": {"chips": 300, "scored": 0}, "hands_left": 2, "discards_left": 3, "jokers": []},
            {"handName": "High Card", "totalScore": 50, "cards": [card("2", "D", 0)]},
            {"intent": "pair_upgrade", "reason": "x", "cards": [card("2", "D", 0), card("4", "C", 1)]},
        ],
        "expect": {"verdict": "DISCARD"},
    },
    # --- v136 boss-blind gates (previously detected but never enforced) ---
    {
        # The Flint: level base halved exactly (vanilla mod_chips/mod_mult
        # do NOT floor mid-scoring; only the final product floors).
        # High Card K: chips 5/2 + 10 = 12.5, mult 1/2, floor(12.5*0.5) = 6.
        "name": "flint_halves_level_base",
        "fn": "evaluateHandScore",
        "args": [[card("K", "S", 0)], [], {"name": "The Flint", "chips": 300}, {}],
        "expect": {"chips": 12.5, "mult": 0.5, "totalScore": 6},
    },
    {
        # The Eye: Three of a Kind already played this round -> the trips
        # line is banned to 0 and the best LEGAL line (pair of 5s) ranks top.
        "name": "eye_bans_already_played_hand_type",
        "fn": "findBestPlays",
        "args": [{"hand": [card("5", "H", 0), card("5", "D", 1), card("5", "C", 2),
                           card("9", "H", 3), card("2", "D", 4)],
                  "jokers": [], "blind": {"name": "The Eye", "chips": 300, "scored": 0},
                  "hand_played_round": {"Three of a Kind": 1},
                  "hands_left": 3, "discards_left": 2}],
        "expect": {"handName": "Pair", "totalScore": 40},
    },
    {
        # The Mouth: Pair was the round's first (and therefore only legal)
        # type -- the higher-scoring Two Pair line (48*2=96) is banned to 0
        # and the best legal pair (9s: 28*2) tops.
        "name": "mouth_locks_first_played_hand_type",
        "fn": "findBestPlays",
        "args": [{"hand": [card("5", "H", 0), card("5", "D", 1), card("9", "C", 2),
                           card("9", "S", 3), card("2", "H", 4)],
                  "jokers": [], "blind": {"name": "The Mouth", "chips": 300, "scored": 0},
                  "hand_played_round": {"Pair": 1},
                  "hands_left": 3, "discards_left": 2}],
        "expect": {"handName": "Pair", "totalScore": 56},
    },
    {
        # The Ox: playing the most-played type (Pair, 20*2=40) wipes ALL
        # money; while it cannot win the blind outright it is downgraded to
        # 30% (12), so the High Card line (14) tops instead.
        "name": "ox_downgrades_most_played_hand_while_accumulating",
        "fn": "findBestPlays",
        "args": [{"hand": [card("5", "H", 0), card("5", "D", 1), card("9", "C", 2),
                           card("2", "S", 3), card("3", "H", 4)],
                  "jokers": [], "blind": {"name": "The Ox", "chips": 300, "scored": 0},
                  "most_played_hand": "Pair",
                  "hands_left": 4, "discards_left": 3}],
        "expect": {"handName": "High Card", "totalScore": 14},
    },
]


def run_cases(cases):
    proc = subprocess.run(
        [node_exe, str(HARNESS), str(APP_JS)],
        input=json.dumps(cases).encode(),
        capture_output=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise AssertionError(f"harness failed: {proc.stderr.decode(errors='replace')[:500]}")
    return {entry["name"]: entry for entry in json.loads(proc.stdout.decode())}


def _big_state(hand, **extra):
    state = {
        "state_name": "SELECTING_HAND",
        "hands_left": 2,
        "discards_left": 0,
        "money": 10,
        "jokers": [],
        "blind": {"chips": 200000, "scored": 0, "type": "big", "name": "Big Blind"},
        "hand": hand,
        "deck_count": 52,
        "starting_deck_size": 52,
        "deck_cards_total": 52,
        "tarots_used": 0,
    }
    state.update(extra)
    return state


# Endless runs grow the hand to 40+ cards, where C(n,5) is millions of scorings
# per call and used to freeze the browser main thread for minutes. These cases
# pin the bounded search: it must stay fast and still find the structural hand.
LARGE_HAND_CASES = [
    {
        "name": "large_hand_quad",
        "fn": "findBestPlays",
        "args": [_big_state([
            card("J", "S", 0), card("J", "H", 1), card("J", "D", 2), card("J", "C", 3),
            card("2", "S", 4), card("3", "D", 5), card("4", "H", 6), card("5", "C", 7),
            card("6", "S", 8), card("7", "D", 9), card("8", "H", 10), card("9", "C", 11),
            card("10", "S", 12), card("A", "D", 13), card("K", "H", 14), card("Q", "C", 15),
            card("2", "H", 16), card("3", "C", 17), card("5", "S", 18), card("7", "C", 19),
        ])],
    },
    {
        "name": "large_hand_flush",
        "fn": "findBestPlays",
        "args": [_big_state([
            card("2", "H", 0), card("4", "H", 1), card("6", "H", 2), card("8", "H", 3), card("10", "H", 4),
            card("3", "S", 5), card("5", "S", 6), card("7", "S", 7), card("9", "S", 8), card("J", "S", 9),
            card("2", "D", 10), card("5", "D", 11), card("8", "D", 12), card("J", "D", 13), card("K", "D", 14),
            card("3", "C", 15), card("6", "C", 16), card("9", "C", 17), card("Q", "C", 18), card("A", "C", 19),
        ])],
    },
]


def test_large_hand_search_is_pruned_and_bounded():
    started = time.monotonic()
    got = run_cases(LARGE_HAND_CASES)
    elapsed = time.monotonic() - started
    assert elapsed < 5.0, f"large-hand search took {elapsed:.1f}s; pruning is not bounding it"

    quad = got["large_hand_quad"]
    assert quad["ok"], f"large_hand_quad raised: {quad.get('error')}"
    assert quad["result"][0]["handName"] == "Four of a Kind", quad["result"][0]

    flush = got["large_hand_flush"]
    assert flush["ok"], f"large_hand_flush raised: {flush.get('error')}"
    top = flush["result"][0]
    assert top["handName"] == "Flush", top
    assert len({c["suit"] for c in top["cards"]}) == 1, top["cards"]


@pytest.fixture(scope="module")
def results():
    got = run_cases(GOLDEN_CASES)
    missing = [c["name"] for c in GOLDEN_CASES if c["name"] not in got]
    assert not missing, f"harness dropped cases: {missing}"
    return got


@pytest.mark.parametrize("case", GOLDEN_CASES, ids=lambda c: c["name"])
def test_scoring_golden_values(results, case):
    entry = results[case["name"]]
    assert entry["ok"], f"{case['name']} raised: {entry.get('error')}"
    result = entry["result"]
    assert result, f"{case['name']} returned null"

    # findBestPlays returns a ranked candidate list; the contract under test
    # is that the TOP candidate matches the expectation.
    if isinstance(result, list):
        result = result[0]

    for key, value in case["expect"].items():
        actual = result.get(key)
        if isinstance(value, float):
            assert actual == pytest.approx(value), f"{case['name']}: {key}={actual} != {value}"
        else:
            assert actual == value, f"{case['name']}: {key}={actual} != {value}"
