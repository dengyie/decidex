# DecideX Balatro 状态桥(只读)

把真机 Balatro 的对局状态 dump 成 JSON 快照,供 `decidex.balatro` 顾问离线/近实时推荐。
**红线:只观察,不自动出牌、不模拟点击、不改任何游戏数值。**

## 安装(游戏所在机器)

1. 安装 [Steamodded](https://github.com/Steamodded/smods)(需要 Lovely 注入器,见其 README)。
2. 把本目录整个拷到 Balatro 的 `mods/` 目录(与游戏同盘):
   - Windows: `%APPDATA%/Balatro/mods/decidex-bridge/decidex-bridge.lua`
3. 启动游戏进入任意盲注,桥会自动在存档目录落快照:
   - Windows: `%APPDATA%/Balatro/decidex_bridge/state-NNNN.json`
   - 每个决策点(发牌完成/出牌瞬间/弃牌瞬间)一张;`actual` 字段记录玩家当步真实操作。

## 快照格式(与 `decidex.balatro.state.BalatroState` 一一对应)

```json
{
  "hand": [{"rank":"A","suit":"S","enhancement":null,"edition":null,"seal":null}],
  "blind": {"name":"The Pillar","type":"big","chips":600,"scored":0},
  "jokers": [{"name":"Joker","chips":0,"mult":4,"xmult":1,"extra":{}}],
  "consumables": [{"name":"The Hermit","type":"tarot"}],
  "hands_left": 4, "discards_left": 3, "hand_size": 8,
  "money": 8, "ante": 2, "round": 5,
  "actual": null
}
```

- `pre` 快照(`actual: null`)是决策点;紧随的 `actual` 快照是玩家真实操作,
  网页/回放会把两者配对成"顾问 vs 玩家"。
- 卡牌索引 `cards` 是**手牌显示顺序(从 0 起)**。

## 消费快照

```bash
# 离线回放(顾问 vs 玩家一致率)
python examples/balatro_real_replay.py "%APPDATA%/Balatro/decidex_bridge"

# 网页查看器
python tools/balatro_web/app.py --port 8091        # BALATRO_BRIDGE_DIR 可改快照目录
```

## 模拟覆盖范围(v1)

- **牌型**:14 种全部(含 Flush Five / Flush House / Five of a Kind,A 高低顺均可,万能牌算任意花色)。
- **计分**:基础筹码×倍率表、牌面筹码、bonus/mult/wild/glass/steel/lucky 强化、
  foil/holo/poly 版本、红印(重触发,含基础筹码)、钢铁留手 ×2。
- **小丑**:约 25 个常见白卡小丑按名字内置(逐牌/整手钩子);不在表内的小丑只计
  桥上报的静态 chips/mult/x_mult。玻璃/幸运按期望值(确定性模拟,不吃 RNG)。
- **未模拟**: planet 等级、部分模组小丑、商店/BI 阶段决策。

## 排障

- 快照没生成:`sendDebugMessage` 里有 `DecideX bridge write failed: ...`;确认
  Steamodded 已加载(主菜单左下版本号)、目录可写。
- 字段对不上:桥是按原版 `Card.base/ability/edition/seal` 读的;模组自定义卡牌
  可能缺字段,桥会以 `null` 兜底。
