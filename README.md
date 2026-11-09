<!-- markdownlint-disable-file MD033 MD045 -->
<div align="center">

# DecideX

**Industrial Decision Foundation Layer & Safe Inference Runtime for System One Fast-Thinking Models**

[![License](https://img.shields.io/badge/license-MIT-green.svg?style=flat-square)](LICENSE)
[![Python Version](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-3776AB.svg?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Pydantic](https://img.shields.io/badge/Pydantic-v2-E92063.svg?style=flat-square&logo=pydantic&logoColor=white)](https://pydantic.dev)
[![FastAPI](https://img.shields.io/badge/Framework-FastAPI-009688.svg?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Tests](https://img.shields.io/badge/tests-552%20passed-brightgreen.svg?style=flat-square)]()

[English](./README.md) · [简体中文](./README.zh-CN.md) · [Core Features](#core-features) · [Architecture](#system-architecture) · [Primitives](#core-decision-primitives) · [Quick Start](#quick-start) · [Model Failover](#model-provider--failover-hierarchy)

<br>

<p align="center">
  <b>Terminates high-latency overhead and hallucinatory text generation pitfalls of general LLMs in real-time gaming and high-frequency control.</b><br>
  Provides an industrial-grade runtime for sub-100ms System One structured decision models (TypeSafe Jev, Nace Drex, NanoJev) with calibrated safety gates, multi-head isolation, and deadlock prevention.<br>
  Features a five-stage formal decision pipeline, 1,170+ account pool management with amortized O(1) round-robin scheduling and exponential backoff, and production adapters for competitive games.
</p>

</div>

---

## Motivation & Architecture Comparison

Traditional generative Large Language Models (LLMs) suffer from high latency (500ms–3s), unpredictable text hallucination, and lack of calibrated mathematical confidence. DecideX strictly decouples intent evaluation ("What") from physical actuation ("How"):

| Evaluation Dimension | DecideX Foundation | Conventional Generative LLM | Pure Rule Engine / Heuristic |
| :--- | :--- | :--- | :--- |
| **Inference Latency** | Sub-100ms discrete decision tokens | 500ms - 3000ms autoregressive generation | < 1ms static execution |
| **Output Space** | Strongly typed discrete actions (`BOOLEAN`, `CHOICE`, `SCORE`) | Free-form unstructured text | Fixed pre-programmed conditional branches |
| **Confidence Calibration** | Temperature-scaled Softmax ($p \in [0, 1]$) with Logit shifts | Uncalibrated token log-probabilities | Binary rule matches without uncertainty modeling |
| **Safety Gating** | Real-time oscillation detection, deadlocks, and fallback switches | Post-hoc regex matching or guardrail models | Static manual exception handling |
| **Egress Resilience** | Multi-provider failover across 1,170+ pooled accounts | Single API endpoint or gateway failover | Local calculation, no external network risk |
| **Generalization** | Adapts to novel state spaces via fast-thinking System One weights | Broad reasoning but too sluggish for real-time control | Brittle, zero adaptability to unforeseen states |

---

## Core Features

| Module | Feature Highlight | Technical Implementation & Advantage |
| :--- | :--- | :--- |
| **Five-Stage Pipeline** | End-to-end decision lifecycle | Formalized five phases: Evidence Assembly, Bounded Inference, Safety Gating, Deterministic Fallback, and Telemetry Update. |
| **Safety Gating Guard** | Numerical stability & anti-oscillation | Implements max-shifted Softmax ($z_i - z_{\max}$) preventing overflow, cyclic deadlock detection ($[A,B,A,B]$), and multi-head action whitelists. |
| **Decision Primitives** | Strongly typed inference contracts | Built on Pydantic v2: `BOOLEAN` (binary truth), `CHOICE` (discrete selection), `SCORE` (scalar utility $[-1.0, 1.0]$), and `MULTI_CHOICE`. |
| **Multi-Provider Failover** | Tiered priority routing engine | Hierarchical fallback: FuturePPO (free tier) -> Nace Drex -> MindsHub -> TypeSafe 1,170+ enterprise pool with circuit breakers. |
| **Fleet KeyPool Hub** | High-concurrency account orchestrator | Amortized $O(1)$ ring-pointer scheduling, dynamic `Retry-After` header extraction, exponential backoff ($60\text{s} \times 2^k$), and atomic file persistence. |
| **Real-Time Game Adapters** | Production-tested game runtimes | Production integrations for ARK 2048 (CDP 9222 + Canvas hook), Super Mario (predictive frame clock), Balatro, and Hearthstone. |

---

## System Architecture

```text
                    ┌────────────────────────────────────────┐
                    │       1. Evidence Assembly             │
                    │ (Multimodal / Feature Context Packing) │
                    └───────────────────┬────────────────────┘
                                        │
                                        ▼
                    ┌────────────────────────────────────────┐
                    │     2. Bounded Model Judgment          │
                    │   (System One Discrete Inference SLM)  │
                    └───────────────────┬────────────────────┘
                                        │
                                        ▼
                    ┌────────────────────────────────────────┐
                    │     3. Calibrated Safety Gating        │
                    │ (Softmax Bounds / Cycle Loop Detection)│
                    └───────┬────────────────────────┬───────┘
                            │ [Pass]                 │ [Reject / Out-of-Bounds]
                            ▼                        ▼
                    ┌───────────────┐        ┌───────────────┐
                    │ Adopt Model   │        │ 4. Fallback   │
                    │ Verdict       │        │ Deterministic │
                    └───────┬───────┘        └───────┬───────┘
                            │                        │
                            └───────────┬────────────┘
                                        ▼
                    ┌────────────────────────────────────────┐
                    │ 5. Telemetry Feedback & Memory Update  │
                    │ (Temporal Ring Buffer / fcntl Logging) │
                    └────────────────────────────────────────┘
```

### Five-Stage Pipeline Details

1. **Evidence Assembly**: Compresses continuous game states and observation matrices into semantic criteria without redundant token waste.
2. **Bounded Model Judgment**: Dispatches structured requests to System One inference endpoints, receiving discrete choices and posterior distributions.
3. **Calibrated Safety Gating**:
   - **Max-Shifted Softmax**: Computes $p_i = \frac{\exp(z_i - z_{\max})}{\sum_j \exp(z_j - z_{\max})}$, eliminating numerical overflow at extreme logits or $T \to 0$.
   - **Multi-Head Action Isolation**: Independently routes question choices and strictly halts if an active question produces an empty legal action candidate set.
   - **Cyclic Oscillation Detection**: Sliding window buffer detects repetitive behavioral loops such as $[A, B, A, B]$ or $[A, B, C, A, B, C]$.
   - **Productive Action Gating**: Distinguishes between intentional repetitive inputs (e.g., merging 2048 downward tiles) versus trapped stall states (`BLOCKED`, `STUCK`).
4. **Deterministic Fallback**: Automatically downgrades to algorithmic heuristics when inference times out, network fails, or confidence drops below target thresholds.
5. **Telemetry Feedback & Memory Update**: Appends transitions to ring-buffer temporal memory, registers state settling guards, and flushes audit records via `fcntl.flock`.

---

## Core Decision Primitives

| Primitive | Value Type | Output Specification | Typical Scenarios |
| :--- | :--- | :--- | :--- |
| **`BOOLEAN`** | Binary True / False | Truth value + calibrated confidence ($p \in [0, 1]$) | Jump timing, spell countering, defensive shield activation |
| **`CHOICE`** | Single / Multi-Option | Selected candidate + posterior probability distribution | Tactical maneuvers, card play vs. discard, chess move selection |
| **`SCORE`** | Continuous Scalar | Normalized utility estimation ($s \in [-1.0, 1.0]$) | Board danger index, lethal damage probability, expected win rate |
| **`MULTI_CHOICE`**| Multi-Label Subset | Discrete set of valid choices | Combo sequence execution, multi-target coordinated focus |

---

## Production Implementations

### 1. ARK 2048 Autonomous Matcher (`examples/play_2048_live.py`)
- **Real-Time Environment**: Connects directly to live browser sessions via Chrome DevTools Protocol (CDP 9222).
- **Single-Step Decision Guarantee**: Strictly executes individual step-by-step model evaluations, rejecting multi-step batching shortcuts.
- **Canvas Memory Hook**: Non-intrusively injects JS hooks intercepting `CanvasRenderingContext2D.prototype.fillText` to capture live 4x4 matrix states.
- **Expectimax Foresight**: Uses uniform grid stride sampling with snake-pattern monotonicity scoring to calculate anchor rewards.
- **Zero-Flicker Rendering**: Replaces disruptive shell clears with ANSI in-place cursor overwrites (`\033[2J\033[H`).

### 2. Super Mario Reactive Controller (`examples/mario_reactive_demo.py`)
- **Dual-Frequency Clock**: `PredictiveFrameController` decouples 120Hz micro-physics simulation ticks from 15Hz macro inference evaluation.
- **Inertial Lookahead**: Maintains previous movement momentum during network inference round-trips, eliminating frame drops.

### 3. Balatro Turn-Based Tactics (`examples/balatro_turn_demo.py`)
- **Combinatorial Pruning**: `CandidatePruner` trims 50+ hand permutations down to Top-3 viable tactical subsets to avoid probability dilution.
- **Multi-Head Evaluation**: Jointly evaluates play vs. discard choices, tarot trigger windows, and chip multiplier expectations.

### 4. Hearthstone Game-Tree Reasoning
- **Multi-Head Routing**: Enforces strict action routing across `{"play_card": [...], "target_unit": [...]}` dictionaries.
- **Rigid Rule Engine**: Incorporates mana crystal constraints, taunt minion targeting requirements, and lethal calculations.

### 5. Fleet KeyPool Account Hub (`decidex/pool.py`)
- **1,170+ Live Accounts**: Manages enterprise token pools under high-throughput production workloads.
- **Amortized O(1) Scheduling**: Ring pointers eliminate repetitive allocation overhead, slashing single-step dispatch latency by 90%.
- **Circuit Breaker & Backoff**: Automatically reads HTTP 429 `Retry-After` headers for exponential backoff ($60\text{s} \times 2^k$) and isolates invalid 401 keys atomically.

---

## Model Provider & Failover Hierarchy

DecideX provides resilient multi-route orchestration across heterogeneous providers through `PriorityFailoverProvider`:

```text
Priority 1: FuturePPO Free Gateway  ──► [HTTP 429/529 / Cooldown]
    │
    ▼
Priority 2: Nace Drex (drex-latest) ──► [HTTP 429/529 / Cooldown]
    │
    ▼
Priority 3: MindsHub Aggregator     ──► [HTTP 429 / Quota Exhaustion]
    │
    ▼
Priority 4: TypeSafe Enterprise Pool (1,170+ Managed Keys)
    │
    ▼
Fallback:   Deterministic Heuristic Safety Engine
```

### Provider Configuration

```python
from decidex.providers import PriorityFailoverProvider

# Automatically configures default priority failover chain
provider = PriorityFailoverProvider.from_default_routes()
verdicts = await provider.infer(payload, questions)

# Inspect per-route telemetry (active / cooldown / dead status)
print(provider.stats())
```

---

## Quick Start

### 1. Installation

```bash
# Clone repository
git clone https://github.com/dengyie/decidex.git
cd decidex

# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install package in editable mode with development dependencies
pip install -e ".[dev]"
```

### 2. Operational CLI (`./manage.sh`)

```bash
# Inspect KeyPool capacity and health statistics
./manage.sh stats

# Probe live account latency (5 workers, 5 random samples)
./manage.sh probe --sample 5 --workers 5

# Run complete test suite (552 passed)
./manage.sh test

# Launch ARK 2048 CDP live match
./manage.sh play-2048 --mode hybrid --delay 0.05

# Run game demonstration pipelines
./manage.sh demo mario     # Reactive real-time gaming
./manage.sh demo balatro   # Turn-based tactical pruning
./manage.sh demo keypool   # Account pool rate-limiting & failover
```

### 3. Python API Integration

```python
import asyncio
from decidex import DecisionEngine, DecisionTask, DecisionQuestion, PrimitiveType
from decidex.guards import CalibratedDecisionGuard
from decidex.providers.typesafe import TypeSafeJevProvider

async def main():
    # 1. Initialize engine with calibrated safety guard
    provider = TypeSafeJevProvider()
    engine = DecisionEngine(
        provider=provider,
        guard=CalibratedDecisionGuard(min_confidence=0.6, enforce_legal=True)
    )

    # 2. Formulate multi-head decision task
    task = DecisionTask(
        id="combat_turn_01",
        questions=[
            DecisionQuestion(
                id="skill",
                primitive=PrimitiveType.CHOICE,
                prompt="Select combat skill",
                options=["Attack", "Heal", "Defend"]
            ),
            DecisionQuestion(
                id="target",
                primitive=PrimitiveType.CHOICE,
                prompt="Select target unit",
                options=["Boss", "MinionA", "Self"]
            )
        ],
        context={"player_hp": 65, "boss_hp": 20, "potions": 1}
    )

    # 3. Supply legal action candidate whitelist
    legal_actions = {
        "skill": ["Attack", "Defend"],
        "target": ["Boss"]
    }
    verdicts = await engine.step(task, legal_actions=legal_actions)

    print(f"Action: {verdicts['skill'].selected} (Confidence: {verdicts['skill'].calibrated_confidence:.2f})")
    print(f"Target: {verdicts['target'].selected}")

if __name__ == "__main__":
    asyncio.run(main())
```

---

## Project Module Topology

```text
decidex/
├── src/decidex/
│   ├── types.py            # Pydantic v2 data models (Task, Question, Verdict)
│   ├── engine.py           # Core dispatch engine (five-stage execution cycle)
│   ├── guards.py           # Safety guards (Softmax, loop detection, settling)
│   ├── memory.py           # Circular temporal memory (MemoryHarness)
│   ├── planners.py         # Frame scheduler (PredictiveFrameController)
│   ├── pool.py             # KeyPool hub (O(1) round-robin, backoff, atomic writes)
│   ├── pruner.py           # Combinatorial space pruner (Top-K heuristic filter)
│   ├── providers/          # Inference adapters (TypeSafe, Nace, MindsHub, NanoJev)
│   └── tools/              # Tooling utilities (CDP 2048, solvers, pool manager)
├── examples/               # Production scenario demos (2048, Mario, Balatro)
├── tests/                  # Complete test suite (552 passing tests)
├── .github/workflows/      # Automated CI/CD pipelines
├── manage.sh               # Unified operations and benchmark harness
└── pyproject.toml          # Hatchling package configuration
```

---

## Technology Stack Inventory

- **Core Runtime**: Python 3.10 / 3.11 / 3.12
- **Data Validation & Typing**: Pydantic v2
- **Web API & Microservice**: FastAPI / Starlette / Uvicorn
- **Browser Automation**: Playwright / Google Chrome DevTools Protocol (CDP)
- **Math & Probabilistic Algorithms**: NumPy / SciPy (Softmax, Bezier, Expectimax)
- **System Concurrency**: `asyncio` / `fcntl` file locks / thread worker pools

---

## Contributing & License

Pull requests and issues are welcome. Ensure that all unit and regression test suites pass before submitting changes:

```bash
uv run pytest tests/ -q
```

Licensed under the [MIT License](LICENSE).
