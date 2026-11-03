/* Mango Arcade — Balatro PWA offline layer (2026-11-04, v2)
 *
 * 目标：/play 在断网后仍能重新打开并进入游戏。
 * 分工：
 *   - 本 SW 只负责「应用外壳」（HTML/JS/CSS/manifest）的离线缓存；
 *   - balatro.love / love.wasm 这两个大资产由 app.js 自带的
 *     IndexedDB + CacheStorage 双引擎负责，SW 绝不重复缓存（避免 112MB 双份占用）。
 *
 * v2（2026-11-04 事故根修）：v1 的非导航分支是 `caches.match(req,
 * {ignoreSearch:true})` 缓存优先 —— 安装日（2026-11-01）预缓存的
 * `/app.js`（v108 时代字节）会以「忽略 query」命中一切后续的
 * `app.js?v=N` 新版本 URL，导致 v110 之后的全部决策引擎修复从未到达
 * 安装了 PWA 的浏览器（用户侧「决策比以前傻了」的真正根源）。
 * v2 改为**网络优先、缓存兜底**：在线时外壳永远取最新字节（并回填缓存），
 * 仅断网时回落缓存 —— 离线可玩的目标不变，发版即时生效。
 *
 * 铁律（见 Mango Arcade 手册 §七 ServiceWorker 四连带故障复盘）：
 *   - /api/、/stream、/login、/download 一律旁路，动态响应永不入缓存；
 *   - Range 请求旁路，绝不缓存 206 分片；
 *   - 发版递增 CACHE 版本，activate 时清理旧前缀缓存；
 *   - 外壳（HTML/JS/CSS）永不缓存优先 —— 缓存只作断网兜底。
 */
const CACHE = 'mango-balatro-v2';
const OLD_PREFIX = 'mango-balatro-';

const SHELL = [
  '/',
  '/play',
  '/manifest.json',
  '/style.css',
  '/viz.css',
  '/viz.js',
  '/love.js',
  '/app.js'
];

const BYPASS = [/^\/api\//, /^\/stream/, /^\/login/, /^\/download/];
const BIG_ASSET = /\.(love|wasm)$|\.love\.zip$|\.wasm\.bin$/i;

self.addEventListener('install', (e) => {
  self.skipWaiting();
  e.waitUntil((async () => {
    const cache = await caches.open(CACHE);
    await Promise.all(SHELL.map(async (url) => {
      try {
        const res = await fetch(url, { cache: 'reload' });
        if (res && res.ok) await cache.put(url, res);
      } catch (err) {
        /* 单个外壳资源失败不应让整个 install 失败 */
      }
    }));
  })());
});

self.addEventListener('activate', (e) => {
  e.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(
      keys.filter((k) => k.startsWith(OLD_PREFIX) && k !== CACHE).map((k) => caches.delete(k))
    );
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (BYPASS.some((re) => re.test(url.pathname))) return;
  if (BIG_ASSET.test(url.pathname)) return;
  if (req.headers.get('range')) return;

  if (req.mode === 'navigate') {
    e.respondWith((async () => {
      try {
        const fresh = await fetch(req);
        if (fresh && fresh.ok) {
          const cache = await caches.open(CACHE);
          cache.put(req, fresh.clone()).catch(() => {});
        }
        return fresh;
      } catch (err) {
        return (await caches.match(req, { ignoreSearch: true }))
          || (await caches.match('/play'))
          || (await caches.match('/'))
          || offlinePage();
      }
    })());
    return;
  }

  // v2: shell assets (app.js?v=N etc.) are version-busted -- serving them
  // cache-first would freeze the app at install-day bytes (the v1 incident
  // above). Network-first keeps releases immediate; the cache entry is the
  // offline fallback only.
  e.respondWith((async () => {
    try {
      const fresh = await fetch(req);
      if (fresh && fresh.ok) {
        const cache = await caches.open(CACHE);
        cache.put(req, fresh.clone()).catch(() => {});
      }
      return fresh;
    } catch (err) {
      return (await caches.match(req, { ignoreSearch: true })) || offlinePage();
    }
  })());
});

function offlinePage() {
  return new Response(
    '<!doctype html><meta charset="utf-8"><title>离线</title>'
    + '<body style="margin:0;height:100vh;display:flex;align-items:center;justify-content:center;'
    + 'background:#0f0f12;color:#e6e6e6;font-family:system-ui">'
    + '<div style="text-align:center"><h2>尚未缓存该资源</h2>'
    + '<p style="color:#888">请联网打开一次，之后即可断网游玩。</p></div>',
    { headers: { 'Content-Type': 'text/html; charset=utf-8' } }
  );
}
