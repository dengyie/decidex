<!-- markdownlint-disable-file MD033 MD045 -->
<div align="center">

# DecideX

**专为 System One 快思考决策模型量身打造的工业级通用决策基座与安全运行时**

[![License](https://img.shields.io/badge/license-MIT-green.svg?style=flat-square)](LICENSE)
[![Python Version](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-3776AB.svg?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Pydantic](https://img.shields.io/badge/Pydantic-v2-E92063.svg?style=flat-square&logo=pydantic&logoColor=white)](https://pydantic.dev)
[![FastAPI](https://img.shields.io/badge/Framework-FastAPI-009688.svg?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Tests](https://img.shields.io/badge/tests-552%20passed-brightgreen.svg?style=flat-square)]()

[English](./README.md) · [简体中文](./README.zh-CN.md) · [核心特性](#核心特性) · [五阶段架构](#系统架构与五阶段演算) · [决策原语](#四大核心决策原语) · [快速上手](#快速上手与使用指南) · [号池容灾](#模型服务与多路由故障转移)

<br>

<p align="center">
  <b>彻底摆脱通用大语言模型在即时博弈与高频控制场景中的高延迟与自由文本幻觉困境。</b><br>
  为亚百毫秒级 System One 结构化推理模型（TypeSafe Jev、Nace Drex、NanoJev）提供集校准门控、多头隔离、防死循环熔断于一体的工业级基座。<br>
  确立五阶段形式化决策演算管道，内置纳管 1,170+ 现网账号的高性能号池中枢（O(1) 轮询与指数退避），经过 2048、马里奥、小丑牌等实战检验。
</p>

</div>

---

## 痛点与架构对比

传统生成式大语言模型（LLM）因**延迟高（500ms~3s）**、**自由文本易幻觉**、**难以微分**以及**缺乏严格数学置信度**，难以胜任毫秒级即时对抗与复杂状态树博弈。DecideX 彻底将**“意图研判（What）”**与**“物理执行（How）”**解耦：

| 评估维度 | DecideX 决策基座 | 传统生成式大语言模型 (LLM) | 纯启发式 / 硬编码规则引擎 |
| :--- | :--- | :--- | :--- |
| **推理延迟** | 亚百毫秒 (<100ms) 离散决策输出 | 500ms ~ 3000ms 自回归文本流 | < 1ms 静态执行 |
| **输出空间** | 强类型离散原语 (`BOOLEAN`, `CHOICE`, `SCORE`) | 自由文本易出现格式坍缩与幻觉 | 预设硬编码条件分支 |
| **置信度校准** | 带温度缩放与平移的最大值 Softmax 校准 | Token 级别无校准 Logit 概率 | 无不确定度建模，仅有真假判断 |
| **安全控制** | 实时周期振荡识别、防卡死与确定性降级 | 事后正则过滤或外挂 Guardrail | 静态异常捕获，易产生死锁 |
| **出口容灾** | 纳管 1,170+ 账号的多路由智能故障转移 | 单一网关容易 429 限流崩溃 | 本地执行无网络风险 |
| **泛化能力** | 基于预训练决策模型具备跨局面直觉推断 | 知识面广但物理控制过重过慢 | 脆弱，对未定义新局面零适应力 |

---

## 核心特性

| 模块 | 特性亮点 | 技术实现与优势 |
| :--- | :--- | :--- |
| **五阶段决策管道** | 严谨的决策闭环演算 | 明确定义五大演进阶段：证据装配、模型研判、安全门控、确定性保底、遥测记忆回流。 |
| **校准安全门控** | 数值稳定与防死循环 | 最大值平移 Softmax 消除浮点溢出；周期循环振荡检测（$[A,B,A,B]$）；多头动作白名单刚性隔离。 |
| **结构化决策原语** | Pydantic v2 强类型契约 | 原生支持 `BOOLEAN`（二值判断）、`CHOICE`（单选/概率分布）、`SCORE`（标量估值）、`MULTI_CHOICE`（多标签）。 |
| **多源优先降级网关** | 阶梯式自动故障转移 | FuturePPO 免费网关优先 -> Nace Drex -> MindsHub -> 官方 1,170+ 账号池，智能熔断。 |
| **Fleet KeyPool 中枢** | 超大规模号池高并发调度 | 环形指针摊销 $O(1)$ 调度，动态解析 `Retry-After` 指数退避（$60\text{s} \times 2^k$），原子文件安全刷盘。 |
| **多游戏实战底座** | 真实博弈环境无侵入接入 | 深度打通 ARK 2048（CDP 9222 + Canvas Hook）、超级马里奥（双轨分频时钟）、小丑牌与炉石传说。 |

---

## 系统架构与五阶段演算

DecideX 的核心设计哲学是：**“模型给出直觉，代码守死边界，基座提供容灾”**。

```text
                    ┌────────────────────────────────────────┐
                    │       1. Evidence Assembly             │
                    │   (多模态/语义特征提取与上下文装配)    │
                    └───────────────────┬────────────────────┘
                                        │
                                        ▼
                    ┌────────────────────────────────────────┐
                    │     2. Bounded Model Judgment          │
                    │   (System One 极速二分类 / 多选推理)   │
                    └───────────────────┬────────────────────┘
                                        │
                                        ▼
                    ┌────────────────────────────────────────┐
                    │     3. Calibrated Safety Gating        │
                    │   (置信度校准 / 振荡检测 / 动作防呆)   │
                    └───────┬────────────────────────┬───────┘
                            │ [通过]                 │ [拒绝/越界]
                            ▼                        ▼
                    ┌───────────────┐        ┌───────────────┐
                    │ 采用模型决策  │        │ 4. Fallback   │
                    │ (Target Act)  │        │ (确定性启发保底)│
                    └───────┬───────┘        └───────┬───────┘
                            │                        │
                            └───────────┬────────────┘
                                        ▼
                    ┌────────────────────────────────────────┐
                    │ 5. Telemetry Feedback & Memory Update  │
                    │   (时序记忆追加、动作沉降与遥测刷盘)   │
                    └────────────────────────────────────────┘
```

### 五阶段演算定义

1. **Evidence Assembly（证据装配）**：提取游戏核心状态事实，将连续空间与高阶特征压缩为正反向语义准则（Semantic Criteria）。
2. **Bounded Model Judgment（模型研判）**：调用 System One 决策模型，毫秒级输出带校准置信度的离散决策。
3. **Calibrated Safety Gating（安全门控）**：
   - **最大值平移 Softmax**：$p_i = \frac{\exp(z_i - z_{\max})}{\sum_j \exp(z_j - z_{\max})}$，杜绝极端 Logit 或 $T \to 0$ 导致的数值溢出。
   - **多头动作隔离**：按 `question.id` 独立路由候选集，显式空列表 `[]`（如角色被控无合法操作）严格阻断。
   - **周期振荡检测**：滑动窗口识别 $[A, B, A, B]$ 或 $[A, B, C, A, B, C]$ 往复循环死锁。
   - **连续动作安全防呆**：智能区分生产性动作（如 2048 连续向下合并）与无效停滞（`BLOCKED`、`STUCK`）。
4. **Deterministic Fallback（确定性保底）**：模型超时、网络异常、置信度不足或被安全门拦截时，平滑降级至启发式算法。
5. **Telemetry Feedback & Memory Update（遥测回流与记忆）**：时序记忆追加、动作沉降防假结算、`fcntl.flock` 跨进程安全落盘。

---

## 四大核心决策原语

| 原语名称 | 类型枚举 | 输出形态 | 典型应用场景 |
| :--- | :--- | :--- | :--- |
| **`BOOLEAN`** | 二值真假 | 布尔真值 + 校准置信度 ($p \in [0, 1]$) | 是否跳跃、是否交法术反制、是否提前交大招 |
| **`CHOICE`** | 单选/多选 | 选定分支 + 全概率后验分布 ($p_i$) | 战术动作选择、手牌出牌/弃牌、走子方向 |
| **`SCORE`** | 标量估值 | 连续分值估算 ($s \in [-1.0, 1.0]$) | 盘面危险度评分、斩杀概率、预期胜率估值 |
| **`MULTI_CHOICE`** | 多标签判定 | 离散子集集合 | 复合招式组合、多目标协同攻击 |

---

## 实战接入场景矩阵

### 1. ARK 2048 实战单步对战 (`examples/play_2048_live.py`)
- **真实环境**：通过 Chrome DevTools Protocol (CDP 9222) 附着真实在线对战页面。
- **单步实时研判**：坚决贯彻“每一步均由模型独立研判，拒绝多步打包合并”，真机毫秒级实时对决。
- **Canvas Hook 盘面感知**：向网页无侵入注入 JS 拦截器，使用 `saveDepth` 嵌套深度计数器拦截 `CanvasRenderingContext2D.prototype.fillText`，精准提取 4x4 实时矩阵。
- **无偏 Expectimax 前瞻估值**：采用均匀网格步长（Uniform Grid Stride）采样消除上方偏置，精准计算蛇形单调性与角锚定得分。
- **实战防抖与终端渲染**：内置 `StateSettlementGuard` 消除幽灵过渡帧；采用 ANSI 原地字符覆盖（`\033[2J\033[H`）取代低效清屏，实现控制台 0 闪烁低延迟渲染。

### 2. 超级马里奥 Mario 实时反应式对战 (`examples/mario_reactive_demo.py`)
- **双轨分频时钟调度**：通过 `PredictiveFrameController` 将 120Hz 微观物理帧与 15Hz 宏观意图推理帧解耦。
- **异步双缓冲前瞻**：在云端网络推理延迟期间维持前帧惯性动作，有效抗网络抖动，高帧率游戏无感掉帧。

### 3. 小丑牌 Balatro 战术回合决策 (`examples/balatro_turn_demo.py`)
- **组合爆炸空间剪枝**：利用 `CandidatePruner` 启发式粗排，将 50+ 组合空间实时剪枝至 Top-3 候选，防止模型概率过度稀释。
- **多头协同研判**：单步协同决策“出牌 vs 弃牌”、“塔罗牌触发时机”与“筹码乘区期望”。

### 4. 炉石传说 Hearthstone 博弈树决策
- **多头动作隔离路由**：精准实现 `{"play_card": [...], "target_unit": [...]}` 字典级合法动作路由。
- **硬性规则校验**：集成费用水晶刚性约束、嘲讽怪强制吸收攻击与斩杀线预测。

### 5. Fleet KeyPool 账号集群调度中枢 (`decidex/pool.py`)
- **1,170+ 现网账号纳管**：支撑大规模高并发调用场景。
- **O(1) 摊销 Round-Robin**：环形指针避免重复分配推导列表，单步决策开销降低 90%。
- **容灾防封断路器**：智能解析 HTTP 429 的 `Retry-After` 响应头，支持指数退避（$60\text{s} \times 2^k$）；精准 401 隔离；临时文件 `os.replace` 原子防崩落盘。

---

## 模型服务与多路由故障转移

`PriorityFailoverProvider` 按优先级逐路由尝试，构建四层弹性降级链路：

```text
路由 1: FuturePPO 免费网关      ──► [触发 HTTP 429/529 限流冷却]
    │
    ▼
路由 2: Nace Drex (drex-latest) ──► [触发 HTTP 429/529 限流冷却]
    │
    ▼
路由 3: MindsHub 聚合服务       ──► [触发额度或并发耗尽]
    │
    ▼
路由 4: 官方 TypeSafe 1,170+ 号池 (底座容灾)
    │
    ▼
保底降级: 确定性启发保底算法 (安全拦截)
```

### 多路由调用示例

```python
from decidex.providers import PriorityFailoverProvider

# 自动装配默认优先级降级链路
provider = PriorityFailoverProvider.from_default_routes()
verdicts = await provider.infer(payload, questions)

# 实时查询各路由健康遥测指标 (active / cooldown / dead 状态)
print(provider.stats())
```

---

## 快速上手与使用指南

### 1. 环境安装

```bash
# 克隆代码仓库
git clone https://github.com/dengyie/decidex.git
cd decidex

# 创建并激活虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 安装基础与开发依赖
pip install -e ".[dev]"
```

### 2. 统一运维脚手架 (`./manage.sh`)

```bash
# 查看号池容量与可用状态
./manage.sh stats

# 并发抽验号池存活与延迟 (5 并发抽查 5 个 Key)
./manage.sh probe --sample 5 --workers 5

# 执行全量单元测试与回归套件 (552 passed)
./manage.sh test

# 启动 2048 实时 CDP 实盘对战
./manage.sh play-2048 --mode hybrid --delay 0.05

# 运行各场景演示 Demo
./manage.sh demo mario     # 实时反应式
./manage.sh demo balatro   # 回合制多头
./manage.sh demo keypool   # 号池限频容灾
```

### 3. Python 核心调用示例

```python
import asyncio
from decidex import DecisionEngine, DecisionTask, DecisionQuestion, PrimitiveType
from decidex.guards import CalibratedDecisionGuard
from decidex.providers.typesafe import TypeSafeJevProvider

async def main():
    # 1. 初始化引擎与安全门控
    provider = TypeSafeJevProvider()
    engine = DecisionEngine(
        provider=provider,
        guard=CalibratedDecisionGuard(min_confidence=0.6, enforce_legal=True)
    )

    # 2. 构造多头决策任务
    task = DecisionTask(
        id="combat_turn_1",
        questions=[
            DecisionQuestion(
                id="skill",
                primitive=PrimitiveType.CHOICE,
                prompt="选择技能",
                options=["Attack", "Heal", "Defend"]
            ),
            DecisionQuestion(
                id="target",
                primitive=PrimitiveType.CHOICE,
                prompt="选择目标",
                options=["Boss", "MinionA", "Self"]
            )
        ],
        context={"player_hp": 65, "boss_hp": 20, "potions": 1}
    )

    # 3. 传入多头独立动作白名单
    legal_actions = {
        "skill": ["Attack", "Defend"],
        "target": ["Boss"]
    }
    verdicts = await engine.step(task, legal_actions=legal_actions)

    print(f"释放技能: {verdicts['skill'].selected} (置信度: {verdicts['skill'].calibrated_confidence:.2f})")
    print(f"施法目标: {verdicts['target'].selected}")

if __name__ == "__main__":
    asyncio.run(main())
```

---

## 模块结构

```text
decidex/
├── src/decidex/
│   ├── types.py            # Pydantic v2 强类型契约 (Task, Question, Verdict 等)
│   ├── engine.py           # 核心调度引擎 (五阶段决策闭环、多头路由、flock 遥测)
│   ├── guards.py           # 安全门控 (最大平移 Softmax、振荡检测、防假结算、生产性放行)
│   ├── memory.py           # 环形时序记忆 (MemoryHarness、多通道防抖、动作频率统计)
│   ├── planners.py         # 规划编排器 (PredictiveFrameController 双频预测时钟)
│   ├── pool.py             # Fleet KeyPool 号池 (O(1) 轮询、429 退避、401 隔离、原子写)
│   ├── pruner.py           # 组合爆炸剪枝 (Top-K 粗排截断)
│   ├── providers/          # 推理适配层 (TypeSafeJevProvider, LocalNanoJevProvider)
│   └── tools/              # 实战工具箱 (browser_2048, solver2048, pool_manager, calibrate)
├── examples/               # 实战场景与对战示例 (2048, Mario, Balatro, KeyPool)
├── tests/                  # 单元测试与回归套件 (552 passed)
├── .github/workflows/      # GitHub Actions CI/CD 流水线
├── manage.sh               # 统一工程运维脚本
└── pyproject.toml          # Hatchling 构建配置
```

---

## 全栈技术清单

- **核心运行环境**：Python 3.10 / 3.11 / 3.12
- **数据结构与契约**：Pydantic v2
- **服务网关**：FastAPI / Starlette / Uvicorn
- **浏览器与协议驱动**：Playwright / Google Chrome DevTools Protocol (CDP)
- **数值与算法库**：NumPy / SciPy (Softmax, 贝塞尔, Expectimax)
- **并发与系统原语**：`asyncio` / `fcntl` 文件锁 / 线程池

---

## 参与贡献与许可证

欢迎提交 Issue 和 Pull Request！
在提交代码前，请确保全量测试通过：

```bash
uv run pytest tests/ -q
```

本项目采用 [MIT License](LICENSE) 开源协议。
