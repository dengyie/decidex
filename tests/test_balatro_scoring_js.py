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
        "name": "supernova_uses_hand_played",
        "fn": "evaluateHandScore",
        "args": [[card("7", "S", 0), card("7", "H", 1)], [{"name": "Supernova"}], {},
                 {"hand_played": {"Pair": 3}}],
        "expect": {"chips": 24, "mult": 5, "totalScore": 120},
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
        "name": "card_sharp_dormant_on_first_play",
        "fn": "evaluateHandScore",
        "args": [[card("7", "S", 0), card("7", "H", 1)],
                 [{"name": "Card Sharp", "extra": {"Xmult": 3}}], {},
                 {"hand_played_round": {"Pair": 1}}],
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
    # --- Candidate ranking & arbitration ---
    {
        "name": "findBestPlays_prefers_pair_over_high_card",
        "fn": "findBestPlays",
        "args": [{"hand": [card("H", "H", 0), card("7", "S", 1), card("7", "H", 2),
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
