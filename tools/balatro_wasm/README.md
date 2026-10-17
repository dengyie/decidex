# DecideX Balatro WebAssembly (love.js) 纯前端运行与 AI 看板

本模块实现了将《小丑牌》（Balatro v1.0.1）完整编译运行在现代浏览器（WebAssembly / WebGL）中的能力，并内嵌 **DecideX 实时决策 HUD 悬浮看板**。

---

## 一、 核心架构与颠覆性优势

| 维度 | 传统云真机串流架构 (`balatro_web` Xvfb) | 纯前端 WebAssembly 架构 (`balatro_wasm`) |
| :--- | :--- | :--- |
| **服务器带宽 / 流量** | 严重超支风险（300GB 出口限额，需压缩到 560x350 q40） | **0 运行时流量**，一次性加载 ~53MB 资源，CDN/浏览器完全缓存 |
| **服务器 CPU 负载** | 2 vCPU 软件光栅化吃满 170%+，游戏强制 4 FPS | **服务器 0 负载**，全部由玩家浏览器本地算力承载 |
| **画质与帧率** | 低清压缩画质，1~2 FPS 抓取 | **1080P/Retina 视网膜高清，原生 60 FPS** 满血丝滑渲染 |
| **音频表现** | 关闭声音驱动 | **Web Audio 原生立体声音效与 BGM** |
| **操作延迟** | 1.5 ~ 3.0 秒网络往返 | **0 毫秒原生即时响应**，原生支持鼠标拖拽与移动端触屏手势 |
| **AI 决策与交互** | 磁盘文件读写轮询 + xdotool 注入 | **Lua <-> JS 内存级直接通信**，支持一键跟打与实时概率分布 |

---

## 二、 目录结构

```
tools/balatro_wasm/
├── build.py                  # 自动化解包、补丁应用与 balatro.love 打包流水线
├── serve.py                  # 本地开发服务器 (带 WASM/love MIME 与安全头)
├── patches/                  # 浏览器环境适配补丁
│   ├── bit.lua               # PUC-Lua 5.1 纯 Lua 位运算垫片 (band/bor/bxor/lshift...)
│   ├── web_patches.lua       # Mipmaps/伪多线程/平台欺骗/随机数补丁
│   └── decidex_bridge.lua    # WASM 专属 DecideX 状态桥 (调用 JS 回调)
├── runtime/                  # LÖVE 11.5 WebAssembly 预编译运行时
│   ├── love.js               # Emscripten 胶水脚本 (支持 IDBFS 存档与 Canvas 挂载)
│   └── love.wasm             # 11.5 WASM 二进制核心
└── web/                      # 纯静态部署交付目录 (可直发 CDN / S3 / Nginx)
    ├── index.html            # 游戏 Canvas + DecideX 悬浮决策 HUD
    ├── balatro.love          # 打包修补后的游戏资源包 (~53.7 MB)
    ├── style.css             # 响应式布局与 HUD 样式
    ├── app.js                # 前端主逻辑 (WASM 引导、IndexedDB 存档同步、DecideX 通信)
    ├── viz.js / viz.css      # DecideX 统一可视化规范组件
    ├── love.js / love.wasm   # WebAssembly 核心
```

---

## 三、 本地构建与快速预览

### 1. 运行打包流水线
```bash
# 从 Balatro.exe 提取资源、应用 WebGL/WASM 补丁并生成 web/balatro.love
python tools/balatro_wasm/build.py
```

### 2. 启动本地预览服务器
```bash
python tools/balatro_wasm/serve.py --port 8092
```
浏览器打开 `http://localhost:8092` 即可直接游玩并体验 DecideX 悬浮决策看板。

### 3. 运行自动化测试套件
```bash
pytest tests/test_balatro_wasm_build.py -v
```

---

## 四、 静态化部署上线指引

`tools/balatro_wasm/web/` 目录为完全自包含的静态 Web 资产：
1. **直接部署至静态服务器 / CDN**：
   - 支持 Cloudflare Pages、Vercel、Nginx、S3 或腾讯云轻量对象存储。
   - 需确保 Web 服务器对 `.wasm` 扩展名下发 `Content-Type: application/wasm`。
2. **存档持久化**：
   - 玩家的游戏进度、解锁卡牌和最高分自动通过 Emscripten 的 `IDBFS` 实时持久化在浏览器的 IndexedDB 中。
   - 页面右下角提供了「导出存档」功能，支持下载 `.jkr` 格式备份。
