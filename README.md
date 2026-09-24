# DecideX: High-Performance System One Decision Foundation

[![Python Version](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Pydantic](https://img.shields.io/badge/Pydantic-v2-e92063.svg)](https://pydantic.dev)
[![Tests](https://img.shields.io/badge/pytest-48%20passed-brightgreen.svg)]()

**DecideX** 是专为 **System One（快思考）结构化决策模型**（如 TypeSafe Jev、开源 NanoJev 及轻量分类小模型 SLM）量身定制的工业级通用决策基座框架。

传统生成式大语言模型（LLM）由于延迟高（1~5秒）、输出自由文本易幻觉、不可微以及无法提供严格数学置信度，难以直接用于即时操控或复杂的策略博弈。DecideX 彻底将决策与执行解耦，提供毫秒级、强类型、带校准门控的五阶决策闭环。

---

## 核心设计哲学

### 1. "What vs. How" 意图与执行彻底解耦
- **模型只负责意图判断（What）**：通过强类型算子（`Choice` / `Noul` / `Score`）输出具有精确概率分布的离散决策；
- **确定性代码负责物理执行（How）**：A* 寻路、底层手柄宏连击、物理引擎碰撞与游戏规则校验全部由宿主系统保证，杜绝模型幻觉。

### 2. 五阶形式化决策闭环 (Formal Decision Calculus)
```text
State Evidence ──> Bounded Judgment ──> Explicit Policy ──> Checked Action ──> Observed Outcome
 (环境/记忆融合)       (Jev 概率推理)       (温度平滑/门控)      (确定性执行/降级)     (数据飞轮/重放)
```

### 3. 三大决策原语 (Decision Primitives)
| 原语名称 | 数学本质 | 输出形态 | 典型应用场景 |
| :--- | :--- | :--- | :--- |
| **`Choice`** | 多分类后验分布 | 选定分支 + 全概率分布 ($p_i$) | 战术动作选择、手牌出牌/弃牌、走子方向 |
| **`Noul`** | 二值分类真值概率 | 布尔判定 + 校准真值置信度 ($p \in [0, 1]$) | 是否跳跃、是否提前结束回合、是否交解牌 |
| **`Score`** | 连续实数区间估值 | 标量评分 ($s \in [min, max]$) | 盘面危险度评分、斩杀概率、预期胜率估算 |

---

## 模块架构与能力矩阵

```text
decidex/
├── src/decidex/
│   ├── types.py            # Pydantic v2 强类型契约 (QuestionSpec, DecisionVerdict, etc.)
│   ├── guards.py           # 双重复合门控 (StateSettlementGuard 状态防抖 + CalibratedDecisionGuard)
│   ├── memory.py           # 外挂短期记忆 (MemoryHarness: 环形队列 + 反震荡周期死循环阻断)
│   ├── pruner.py           # 组合爆炸剪枝 (CandidatePruner: 启发式粗排 Top-K 截断)
│   ├── lookahead.py        # 1-Step 状态推演评估器 (LookaheadEvaluator: 盘面几何特征投影)
│   ├── planners.py         # 执行规划器 (SequentialTurnPlanner 连击 + DualRateScheduler 双轨分频)
│   ├── pool.py             # 工业级 Jev KeyPool 号池管理 (轮询、429 自动冷却、401 熔断隔离)
│   ├── engine.py           # 核心协调引擎 (DecisionEngine: 并发装配、门控核验、数据飞轮)
│   ├── providers/          # 推理适配层 (TypeSafeJevProvider 号池集成, LocalNanoJevProvider, MockReplayProvider)
│   └── tools/              # 离线工具箱 (calibrate 调优工具 + pool_manager 号池运维 CLI)
├── tests/                  # 48 项全覆盖自动化单元测试
└── examples/               # 四大典型实战场景 Demo (即时动作、小丑牌、2048、号池轮询)
```

### 核心亮点特性：
1. **状态过渡防抖 (`StateSettlementGuard`)**：哈希比对连续物理/动画帧，消除状态采样抖动；
2. **温度平滑与差值显著性 (`CalibratedDecisionGuard`)**：
   - 抑制模型过度自信：$p_i^{\text{cal}} = \frac{\exp(\ln(p_i) / T)}{\sum_j \exp(\ln(p_j) / T)}$
   - 显著性差值校验：要求 $\text{Top}_1 - \text{Top}_2 \ge \Delta$，杜绝模糊分支引发的不可靠行动；
   - 反死震荡阻断（Anti-Oscillation）：识别 $A \leftrightarrow B$ 或 $A \to B \to C \to A$ 往复震荡死循环并强制熔断；
3. **组合动作空间剪枝 (`CandidatePruner`)**：针对小丑牌、炉石等手牌组合爆炸场景，自动将数十种组合剪枝至 $\le 5$ 个候选，彻底防止概率稀释；
4. **非阻塞前瞻双缓冲 (`PredictiveFrameController`)**：异步双缓冲调度，在模型网络推理中维持上一帧惯性宏，实现 60 FPS 无感掉帧；
5. **离线校准飞轮 (`decidex.tools.calibrate`)**：扫描决策日志，计算 Brier Score 与 ECE，求解最优温度参数与门控阈值。

---

## 快速上手

### 1. 环境安装
DecideX 采用现代 Python 打包标准（`pyproject.toml`），可使用 `pip` 或高性能 `uv` 进行安装：

```bash
cd /Users/mango/project/decidex

# 创建并激活虚拟环境 (可选)
uv venv .venv
source .venv/bin/activate

# 可编辑安装基础与开发依赖
uv pip install -e ".[dev]"
```

### 2. 最小运行示例
```python
import asyncio
from decidex import (
    DecisionEngine,
    QuestionSpec,
    PrimitiveType,
    CalibratedDecisionGuard
)
from decidex.providers import MockReplayProvider

async def main():
    # 1. 初始化推理提供方与复合门控
    provider = MockReplayProvider(
        preset_verdicts={
            "action": {"selected": "ATTACK", "confidence": 0.92, "distribution": {"ATTACK": 0.92, "DEFEND": 0.08}},
            "is_lethal": {"selected": True, "confidence": 0.89}
        }
    )
    guard = CalibratedDecisionGuard(temperature=1.25, min_confidence=0.60, min_margin=0.15)
    engine = DecisionEngine(provider=provider, guard=guard)

    # 2. 声明决策头
    questions = [
        QuestionSpec(
            id="action",
            primitive=PrimitiveType.CHOICE,
            description="Select optimal combat action",
            options=["ATTACK", "DEFEND", "FLEE"]
        ),
        QuestionSpec(
            id="is_lethal",
            primitive=PrimitiveType.NOUL,
            description="Can we defeat enemy this round?"
        )
    ]

    # 3. 执行五阶闭环单步决策
    state = {"player_hp": 85, "enemy_hp": 20, "mana": 4}
    verdicts = await engine.step(state, questions, legal_actions=["ATTACK", "DEFEND", "FLEE"])

    print("Chosen Action:", verdicts["action"].selected)
    print("Calibrated Conf:", verdicts["action"].calibrated_confidence)
    print("Is Lethal:", verdicts["is_lethal"].selected)

if __name__ == "__main__":
    asyncio.run(main())
```

---

## 三大实战游戏 Demo 验证

仓库内置了三个覆盖工业界主流游戏品类的端到端实战示例：

### 1. 即时动作类 (Mario Reactive Platformer)
展示非阻塞预测控制器、宏观意图与微观操作的双轨分频调度（0.5Hz vs 10Hz）：
```bash
python examples/mario_reactive_demo.py
```

### 2. 卡牌构筑类 (Balatro Tactical Turn-based)
展示 50+ 组合空间通过 `CandidatePruner` 启发式剪枝至 Top-3、多头决策（出牌/弃牌、塔罗牌使用时机、胜率评估）：
```bash
python examples/balatro_turn_demo.py
```

### 3. 棋盘推演类 (2048 1-Step Lookahead)
展示推演估值器 `LookaheadEvaluator` 预计算上下左右四向移动后的单调性与平滑度指标，并引导模型选定全局最优解：
```bash
python examples/game2048_lookahead_demo.py
```

### 4. Jev 号池高并发轮询与自愈类 (KeyPool Rotation & Fault Tolerance)
展示从知识库一键装载 1171+ Jev 账号、动态负载轮询、429 速率限制自动冷却与 401 死号永久隔离：
```bash
python examples/keypool_rotation_demo.py
```

---

## Jev KeyPool 号池与并发运维

DecideX 原生支持企业级与多账号场景下的 API Key 弹性号池管理（`decidex.pool.KeyPool`），支持无缝对接 Obsidian 知识库中集中维护的 1000+ Jev 账号。

### 1. 核心特性
- **多种轮询策略**：`Round-Robin`（平滑轮询）、`Least-Used`（最少使用优先）、`Random`（随机分流）；
- **429 自动冷却退避**：遭遇速率限制时自动将 Key 移入冷却队列（默认 60s），并在本轮请求中立即热换至下一可用 Key；
- **401 永久隔离熔断**：遭遇凭证失效或撤销时标记为 `DEAD` 状态，彻底剔除出调度环；
- **高并发线程/协程安全**：内置 `asyncio.Lock` 保证高 QPS 跨 Tick 抢占安全；
- **双向数据恢复**：支持全量导出/导入号池运行时运行指标（请求数、成功率、最后报错与冷却状态）。

### 2. 命令行运维工具 (CLI)
```bash
# 查看号池统计摘要
python -m decidex.tools.pool_manager stats

# 将 Obsidian 号池导出为独立运行文件 (JSONL / TXT)
python -m decidex.tools.pool_manager export --out keys.jsonl --format jsonl

# 抽样对号池执行线上最小 noul 存活探测
python -m decidex.tools.pool_manager probe --sample 5 --workers 5
```

---

## 自动化测试与离线自适应校准

### 运行单元测试
DecideX 拥有 100% 通过的完整单元测试集（覆盖数据类型、防抖、校准、剪枝、规划器、协调引擎与校准工具）：
```bash
pytest -v
```

### 运行自适应校准工具
DecideX 在决策过程中会自动持久化结构化轨迹到 JSONL 日志中，使用内置校准工具即可拟合最优参数：
```bash
python -m decidex.tools.calibrate examples/mario_journal.jsonl
```

输出示例：
```text
=======================================================
           DecideX Calibration Analysis Report         
=======================================================
 Samples Evaluated       : 24
 Raw Brier Score         : 0.0414
 Raw ECE                 : 0.0217
-------------------------------------------------------
 Optimal Temperature (T) : 0.80
 Calibrated Brier Score  : 0.0406
 Calibrated ECE          : 0.0073
-------------------------------------------------------
 Recommended CalibratedDecisionGuard Parameters:
  - temperature          = 0.80
  - min_confidence       = 0.85
  - min_margin           = 0.30
=======================================================
```

---

## 许可证
本项目采用 [MIT License](LICENSE) 开源协议。
