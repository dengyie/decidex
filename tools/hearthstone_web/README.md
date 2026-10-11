# DecideX · 炉石顾问 优秀操作回放（网页）

只读顾问的可视化前端：把本机 `data/replays/*.log` 的真实 Power.log 回放，逐个决策点重建棋盘，展示
"游戏引擎权威下发的合法动作包络 → 顾问推荐的最优操作（含置信度/保底归因）→ 与人类实操对比"。

**只读红线**：仅枚举合法动作并给出推荐，绝不驱动游戏客户端。

## 运行

```bash
# 在 decidex 仓库根目录
python tools/hearthstone_web/app.py --host 0.0.0.0 --port 8090
```

依赖（可选装，缺了页面会提示 hslog 不可用）：

```bash
pip install "hslog>=1.20.0" "hearthstone>=9.0.0"
```

启动后控制台会打印可用地址。走 EasyTier 时，本机 mesh IP 形如 `10.144.144.10`，局域网为 `192.168.x.y`：

- EasyTier 内任一设备：`http://10.144.144.10:8090/`
- 本机：`http://127.0.0.1:8090/`

无需 SSH。Windows 上若入站被拦，给所用 python.exe 放行 `Public` 配置文件的 TCP 入站即可
（本机已存在 `Python` 规则覆盖 `E:\code\conda\python.exe`）。

## 两个引擎

| 下拉项 | 说明 |
| --- | --- |
| 启发式（离线·秒出） | 纯离线策略，整局 ~1.6s，适合快速浏览 |
| 真实模型（Jev 网关） | 走 `PriorityFailoverProvider.from_default_routes()`（futureppo→nace→mindshub→官方号池），逐点调用真实模型；本机仅有 mindshub 免费 key 时约 5-6s/点 |

## 接口

- `GET /api/health` → hslog 可用性 + 回放清单
- `GET /api/game?log=<name>` → 该局全部决策点（棋盘 + 合法动作 + 人类实操）
- `POST /api/recommend {log,index,provider}` → 单点推荐
- `POST /api/replay {log,provider}` → 整局推荐

数据源：仓库内 `data/replays/*.log`。要换对局，把 Power.log 丢进该目录刷新即可。