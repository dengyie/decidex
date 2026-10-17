"""
A/B Benchmark test suite for Balatro decision policies.
Compares Baseline Greedy vs DecideX Heuristic v2 vs DecideX Model-Augmented v2.
"""

import pytest

from decidex.balatro.benchmark import BalatroABBenchmark, PolicyVariant


@pytest.mark.asyncio
async def test_benchmark_scenario_generation():
    bench = BalatroABBenchmark()
    assert len(bench.scenarios) == 6
    categories = {s.category for s in bench.scenarios}
    assert "constraints" in categories
    assert "economy" in categories
    assert "joker_order" in categories
    assert "synergy" in categories


@pytest.mark.asyncio
async def test_ab_benchmark_ride_the_bus_constraint():
    bench = BalatroABBenchmark()
    s1 = next(s for s in bench.scenarios if s.id == "scen_01_ride_bus_constraint")

    # Baseline naively plays the King because it gives highest raw chips
    base_res = await bench.run_scenario(s1, PolicyVariant.BASELINE_GREEDY)
    assert base_res.violated_constraint is True, "Baseline should violate Ride the Bus by playing King"

    # DecideX v2 incorporates active constraints, avoiding face cards
    decidex_res = await bench.run_scenario(s1, PolicyVariant.DECIDEX_HEURISTIC_V2)
    assert decidex_res.violated_constraint is False, "DecideX v2 must not violate Ride the Bus"
    assert decidex_res.action_type == "play"


@pytest.mark.asyncio
async def test_ab_benchmark_interest_guard():
    bench = BalatroABBenchmark()
    s2 = next(s for s in bench.scenarios if s.id == "scen_02_interest_line_defense")

    # Baseline buys the first item ($6) without checking interest, falling from $14 to $8 (<$10)
    base_res = await bench.run_scenario(s2, PolicyVariant.BASELINE_GREEDY)
    assert base_res.interest_preserved is False, "Baseline should break the $10 interest bracket"

    # DecideX v2 checks budget verdict: buying the $4 Blue Joker preserves the $10 interest threshold
    decidex_res = await bench.run_scenario(s2, PolicyVariant.DECIDEX_HEURISTIC_V2)
    assert decidex_res.interest_preserved is True, "DecideX v2 must protect the interest bracket"
    assert "Blue Joker" in decidex_res.description or decidex_res.action_type == "leave_shop"


@pytest.mark.asyncio
async def test_ab_benchmark_joker_ordering():
    bench = BalatroABBenchmark()
    s3 = next(s for s in bench.scenarios if s.id == "scen_03_joker_ordering_gain")

    # In s3, jokers are [Cavendish (x3), Half Joker (+20)].
    # Baseline cannot reorder jokers.
    base_res = await bench.run_scenario(s3, PolicyVariant.BASELINE_GREEDY)
    assert base_res.joker_ordered_correctly is False, "Baseline does not recognize bad joker order"

    # DecideX v2 recognizes the >15% ordering gain and issues a reorder action
    decidex_res = await bench.run_scenario(s3, PolicyVariant.DECIDEX_HEURISTIC_V2)
    assert decidex_res.action_type == "reorder_jokers" or decidex_res.joker_ordered_correctly is True
    assert "reorder" in decidex_res.chosen_candidate_id


@pytest.mark.asyncio
async def test_ab_benchmark_the_psychic_and_junk_filtering():
    bench = BalatroABBenchmark()
    s6 = next(s for s in bench.scenarios if s.id == "scen_06_the_psychic_filtering_play")

    # Baseline plays only 2 cards (bare pair of Aces), which violates The Psychic's 5-card rule
    base_res = await bench.run_scenario(s6, PolicyVariant.BASELINE_GREEDY)
    assert base_res.violated_constraint is True
    assert base_res.cleared_target is False

    # DecideX v2 recognizes The Psychic and attaches 3 junk cards, fulfilling the 5-card rule and clearing!
    decidex_res = await bench.run_scenario(s6, PolicyVariant.DECIDEX_HEURISTIC_V2)
    assert decidex_res.violated_constraint is False
    assert decidex_res.cleared_target is True
    assert "play" in decidex_res.chosen_candidate_id
    assert len(decidex_res.chosen_candidate_id.split(":")[1].split("-")) == 5


@pytest.mark.asyncio
async def test_ab_benchmark_full_suite_and_report():
    bench = BalatroABBenchmark()
    results = await bench.run_all()

    # Verify each variant has 6 scenario outcomes
    assert len(results[PolicyVariant.BASELINE_GREEDY]) == 6
    assert len(results[PolicyVariant.DECIDEX_HEURISTIC_V2]) == 6
    assert len(results[PolicyVariant.DECIDEX_MODEL_V2]) == 6

    # Check constraint violation rates: Baseline has violations; DecideX has 0%
    base_violations = sum(1 for o in results[PolicyVariant.BASELINE_GREEDY] if o.violated_constraint)
    decidex_violations = sum(1 for o in results[PolicyVariant.DECIDEX_HEURISTIC_V2] if o.violated_constraint)
    model_violations = sum(1 for o in results[PolicyVariant.DECIDEX_MODEL_V2] if o.violated_constraint)

    assert base_violations > 0
    assert decidex_violations == 0
    assert model_violations == 0

    # Generate Markdown report
    report = bench.generate_markdown_report(results)
    assert "DecideX Balatro 策略体系 A/B 测试对比与量化基准报告" in report
    assert "被动破戒发生率" in report
    assert "利息与安全预算守则合规率" in report

    # Print report for review
    print("\n" + report)
