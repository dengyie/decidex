# DecideX: High-Performance System One Decision Foundation

[![Python Version](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Pydantic](https://img.shields.io/badge/Pydantic-v2-e92063.svg)](https://pydantic.dev)
[![Tests](https://img.shields.io/badge/pytest-86%20passed-brightgreen.svg)]()
[![CI/CD](https://img.shields.io/badge/CI%2FCD-GitHub%20Actions%20%2B%20SSH%20Deploy-success.svg)]()

**DecideX** 是专为 **System One（快思考）超低延迟结构化决策模型**（如 TypeSafe Jev、开源 NanoJev 及专用二分类/多选小模型 SLM）量身打造的工业级通用决策基座框架。

传统生成式大语言模型（LLM）因**延迟高（500ms~3s）**、**自由文本易幻觉**、**难以微分**以及**缺乏严格数学置信度**，难以胜任毫秒级即时对抗与复杂状态树博弈。DecideX 彻底将**“意图研判（What）”**与**“物理执行（How）”**解耦，确立了**五阶段形式化决策演算管道**，在确保毫秒级推理速度的同时，提供严格的安全门控、多头隔离、防死循环熔断与确定性降级保障。

---

## 架构总览与五阶段决策演算

DecideX 的核心设计哲学是：**“模型给出直觉，代码守死边界，基座提供容灾”**。

```
                    ┌────────────────────────────────────────┐
                    │       1. Evidence Assembly             │
                    │   (多模态/语义特征提取与上下文装配)      │
                    └───────────────────┬────────────────────┘
                                        │
                                        ▼
                    ┌────────────────────────────────────────┐
                    │     2. Bounded Model Judgment          │
                    │   (System One 极速二分类 / 多选推理)    │
                    └───────────────────┬────────────────────┘
                                        │
                                        ▼
                    ┌────────────────────────────────────────┐
                    │     3. Calibrated Safety Gating        │
                    │   (置信度校准 / 振荡检测 / 动作防呆)    │
                    └───────┬────────────────────────┬───────┘
                            │ [通过]                 │ [拒绝/越界]
                            ▼                        ▼
                    ┌───────────────┐        ┌───────────────┐
                    │ 采用模型决策   │        │ 4. Fallback   │
                    │ (Target Act)  │        │ (确定性启发保底)│
                    └───────┬───────┘        └───────┬───────┘
                            │                        │
                            └───────────┬────────────┘
                                        ▼
                    ┌────────────────────────────────────────┐
                    │ 5. Telemetry Feedback & Memory Update  │
                    │   (时序记忆追加、动作沉降与遥测刷盘)     │
                    └────────────────────────────────────────┘
```

### 1. 五阶段演算定义
1. **Evidence Assembly（证据装配）**：提取游戏核心状态事实，将连续空间与高阶特征压缩为正反向语义准则（Semantic Criteria）。
2. **Bounded Model Judgment（模型研判）**：调用 System One 决策模型，毫秒级输出带校准置信度的离散决策。
3. **Calibrated Safety Gating（安全门控）**：
   - **最大值平移 Softmax**：$p_i = \frac{\exp(z_i - z_{\max})}{\sum_j \exp(z_j - z_{\max})}$，杜绝极端 Logit 或 $T \to 0$ 导致的数值溢出。
   - **多头动作隔离**：按 `question.id` 独立路由候选集，显式空列表 `[]`（如角色被控无合法操作）严格阻断。
   - **周期振荡检测**：滑动窗口识别 $[A, B, A, B]$ 或 $[A, B, C, A, B, C]$ 往复循环死锁。
   - **连续动作安全防呆**：智能区分生产性动作（如 2048 连续向下合并）与无效停滞（`BLOCKED`、`STUCK`）。
4. **Deterministic Fallback（确定性保底）**：模型超时、网络异常、置信度不足或被安全门拦截时，平滑降级至启发式算法。
5. **Telemetry Feedback & Memory Update（遥测回流与记忆）**：时序记忆追加、动作沉降防假结算、`fcntl.flock` 跨进程安全落盘。

### 2. 四大核心决策原语 (Decision Primitives)
| 原语名称 | 类型枚举 | 输出形态 | 典型应用场景 |
| :--- | :--- | :--- | :--- |
| **`BOOLEAN`** | 二值真假 | 布尔真值 + 校准置信度 ($p \in [0, 1]$) | 是否跳跃、是否交法术反制、是否提前交大招 |
| **`CHOICE`** | 单选/多选 | 选定分支 + 全概率后验分布 ($p_i$) | 战术动作选择、手牌出牌/弃牌、走子方向 |
| **`SCORE`** | 标量估值 | 连续分值估算 ($s \in [-1.0, 1.0]$) | 盘面危险度评分、斩杀概率、预期胜率估值 |
| **`MULTI_CHOICE`** | 多标签判定 | 离散子集集合 | 复合招式组合、多目标协同攻击 |

---

## 已接入实战项目矩阵

DecideX 已经过多品类真实场景验证，涵盖即时动作、回合制卡牌、数学推演与大规模号池集群：

### 🎮 1. ARK 2048 实战单步对战 (`examples/play_2048_live.py`)
- **真实环境**：通过 Chrome DevTools Protocol (CDP 9222) 附着真实在线对战页面（`https://game.ark717.com/`）。
- **严格遵循单步实时决策**：**坚决贯彻“每一步均由 Jev 独立研判，拒绝多步打包合并”**，真机毫秒级实时对决。
- **Canvas Hook 盘面感知**：向网页无侵入注入 JS 拦截器，使用 `saveDepth` 嵌套深度计数器拦截 `CanvasRenderingContext2D.prototype.fillText`，精准提取 4x4 实时矩阵。
- **无偏 Expectimax 前瞻估值**：采用均匀网格步长（Uniform Grid Stride）采样消除上方偏置，精准计算蛇形单调性（Snake Pattern）与角锚定得分。
- **实战防抖与终端渲染**：内置 `StateSettlementGuard` 消除幽灵过渡帧；采用 ANSI 原地字符覆盖（`\033[2J\033[H`）取代 `os.system("clear")`，实现控制台 0 闪烁低延迟渲染。

### 🍄 2. 超级马里奥 Mario 实时反应式对战 (`examples/mario_reactive_demo.py`)
- **双轨分频时钟调度**：通过 `PredictiveFrameController` 将 120Hz 微观物理帧与 15Hz 宏观意图推理帧解耦。
- **异步双缓冲前瞻**：在云端网络推理延迟期间维持前帧惯性动作，有效抗网络抖动，高帧率游戏无感掉帧。

### 🃏 3. 小丑牌 Balatro 战术回合决策 (`examples/balatro_turn_demo.py`)
- **组合爆炸空间剪枝**：利用 `CandidatePruner` 启发式粗排，将 50+ 组合空间实时剪枝至 Top-3 候选，防止模型概率过度稀释。
- **多头协同研判**：单步协同决策“出牌 vs 弃牌”、“塔罗牌触发时机”与“筹码乘区期望”。

### ⚔️ 4. 炉石传说 Hearthstone 博弈树决策
- **多头动作隔离路由**：精准实现 `{"play_card": [...], "target_unit": [...]}` 字典级合法动作路由。
- **硬性规则校验**：集成费用水晶刚性约束、嘲讽怪强制吸收攻击与斩杀线预测。

### 🗝️ 5. Fleet KeyPool 账号集群调度中枢 (`decidex/pool.py`)
- **1,171+ 现网账号纳管**：支撑大规模高并发调用场景。
- **$O(1)$ 摊销 Round-Robin**：环形指针避免重复分配推导列表，单步决策开销降低 90%。
- **容灾防封断路器**：智能解析 HTTP 429 的 `Retry-After` 响应头，支持指数退避（$60\text{s} \times 2^k$）；精准 401 隔离；临时文件 `os.replace` 原子防崩落盘。

---

## 快速上手与使用指南

### 1. 环境安装
DecideX 采用现代 Python 标准打包（`pyproject.toml` + `hatchling`）：

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

# 并发抽验号池存活与延迟 (5并发抽查5个Key)
./manage.sh probe --sample 5 --workers 5

# 执行全量单元测试与回归套件 (86 项全绿)
./manage.sh test

# 启动 2048 实时 CDP 实盘对战
./manage.sh play-2048 --mode hybrid --delay 0.05

# 运行各场景演示 Demo
./manage.sh demo mario     # 实时反应式
./manage.sh demo balatro   # 回合制多头
./manage.sh demo keypool   # 号池限频容灾
```

### 3. Python 核心调用示例

#### 回合制多头决策
```python
import asyncio
from decidex import DecisionEngine, DecisionTask, DecisionQuestion, PrimitiveType
from decidex.guards import CalibratedDecisionGuard
from decidex.providers.typesafe import TypeSafeJevProvider

async def main():
    # 1. 初始化引擎（自动加载本地 keys.jsonl 号池）
    provider = TypeSafeJevProvider(temperature=0.3)
    engine = DecisionEngine(
        provider=provider,
        guard=CalibratedDecisionGuard(min_confidence=0.6, enforce_legal=True)
    )

    # 2. 构造多头决策任务
    task = DecisionTask(
        id="combat_turn_1",
        questions=[
            DecisionQuestion(id="skill", primitive=PrimitiveType.CHOICE, prompt="选择技能", options=["Attack", "Heal", "Defend"]),
            DecisionQuestion(id="target", primitive=PrimitiveType.CHOICE, prompt="选择目标", options=["Boss", "MinionA", "Self"])
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
│   ├── types.py            # Pydantic v2 强类型契约 (Task, Question, Verdict, etc.)
│   ├── engine.py           # 核心调度引擎 (五阶段决策闭环、多头路由、flock 遥测)
│   ├── guards.py           # 安全门控 (最大平移 Softmax、振荡检测、防假结算、生产性放行)
│   ├── memory.py           # 环形时序记忆 (MemoryHarness、多通道防抖、动作频率统计)
│   ├── planners.py         # 规划编排器 (PredictiveFrameController 双频预测时钟)
│   ├── pool.py             # Fleet KeyPool 号池 (O(1)轮询、429退避、401隔离、原子写)
│   ├── pruner.py           # 组合爆炸剪枝 (Top-K 粗排截断)
│   ├── providers/          # 推理适配层 (TypeSafeJevProvider, LocalNanoJevProvider)
│   └── tools/              # 实战工具箱 (browser_2048, solver2048, pool_manager, calibrate)
├── examples/               # 实战场景与对战示例 (2048, Mario, Balatro, KeyPool)
├── tests/                  # 86 项全量单元测试与回归套件
├── .github/workflows/      # GitHub Actions CI/CD 流水线
├── manage.sh               # 统一工程运维脚本
└── pyproject.toml          # Hatchling 构建配置
```

---

## 自动化测试与 CI/CD 生产部署

### 1. 单元测试矩阵 (86 Passed)
代码库具备 100% 覆盖关键决策路径的测试套件，执行耗时 $< 0.5\text{s}$：
```bash
./manage.sh test
```

### 2. GitHub Actions 自动化流水线
- **CI 阶段**：覆盖 Python 3.10、3.11 与 3.12 多版本矩阵自动化测试与依赖校验。
- **CD 阶段**：`main` 分支提交触发通过 SSH 自动拉取与热重载，安全同步至 Bohrium `pxed` 生产主机（`/data/decidex`）。
- **零密钥泄漏红线**：严格遵循 `.gitignore` 规则，生产凭据 `keys.jsonl`（1,171+ 现网 Key）与运行遥测 `*.jsonl` 物理脱离版本库。

---

## 许可证
本项目采用 [MIT License](LICENSE) 开源协议。
