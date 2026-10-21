/**
 * DecideX Balatro WebAssembly Host & Advisor Application
 * Runs Balatro via love.js (LÖVE 11.5) with bi-directional Lua/JS bridge,
 * IndexedDB savegame persistence, and real-time floating AI decision HUD.
 */

(function () {
  "use strict";

  // Prevent any browser alert from freezing the single-threaded WASM runtime
  window.alert = function (msg) {
    console.warn("[Alert Suppressed]:", msg);
  };

  // Globally block browser full-screen requests to ensure DecideX HUD is always visible
  // and canvas coordinates remain strictly 1:1 aligned with the page layout
  try {
    const rejectFs = function() { return Promise.reject(new Error("Fullscreen disabled")); };
    if (typeof HTMLCanvasElement !== "undefined") {
      HTMLCanvasElement.prototype.requestFullscreen = rejectFs;
      HTMLCanvasElement.prototype.webkitRequestFullscreen = function() {};
      HTMLCanvasElement.prototype.mozRequestFullScreen = function() {};
      HTMLCanvasElement.prototype.msRequestFullscreen = function() {};
    }
    if (typeof Element !== "undefined") {
      Element.prototype.requestFullscreen = rejectFs;
      Element.prototype.webkitRequestFullscreen = function() {};
      Element.prototype.mozRequestFullScreen = function() {};
      Element.prototype.msRequestFullscreen = function() {};
    }
    if (document.fullscreenElement && document.exitFullscreen) {
      document.exitFullscreen().catch(() => {});
    }
  } catch (e) {
    console.warn("[DecideX] Could not patch fullscreen prototype:", e);
  }

  // Resilient Background-Execution Guard:
  // When the tab is backgrounded/minimized (document.hidden), Chrome suspends requestAnimationFrame
  // and throttles standard window.setTimeout to 1000ms.
  // We use a lightweight Web Worker clock (which Chrome does NOT throttle) to drive
  // requestAnimationFrame callbacks strictly one-at-a-time at 60 FPS, with zero duplicate executions
  // or re-entrant runner explosions.
  try {
    if (typeof window !== "undefined") {
      const origRAF = window.requestAnimationFrame ? window.requestAnimationFrame.bind(window) : null;
      let pendingHiddenCallbacks = [];
      let hiddenRunning = false;
      let workerInstance = null;

      if (typeof Worker !== "undefined" && typeof Blob !== "undefined" && typeof URL !== "undefined") {
        const workerCode = "let t = null; self.onmessage = function(e) { if (e.data === 'start') { if (!t) t = setInterval(function() { self.postMessage('tick'); }, 16); } else if (e.data === 'stop') { if (t) { clearInterval(t); t = null; } } };";
        const blob = new Blob([workerCode], { type: "application/javascript" });
        workerInstance = new Worker(URL.createObjectURL(blob));
        workerInstance.onmessage = function () {
          if (typeof document !== "undefined" && document.hidden && pendingHiddenCallbacks.length > 0 && !hiddenRunning) {
            hiddenRunning = true;
            const cbs = pendingHiddenCallbacks;
            pendingHiddenCallbacks = [];
            const now = (typeof performance !== "undefined" && performance.now) ? performance.now() : Date.now();
            for (let i = 0; i < cbs.length; i++) {
              try { cbs[i](now); } catch (_) {}
            }
            hiddenRunning = false;
          }
        };
        workerInstance.postMessage("start");
        window.__decidexHeartbeatWorker = workerInstance;
        console.log("[DecideX] Background Web Worker Heartbeat active (queue-driven rAF)");
      }

      if (origRAF) {
        window.requestAnimationFrame = function (cb) {
          if (typeof document !== "undefined" && document.hidden) {
            if (workerInstance) {
              pendingHiddenCallbacks.push(cb);
              return pendingHiddenCallbacks.length;
            }
            return setTimeout(() => cb((typeof performance !== "undefined" && performance.now) ? performance.now() : Date.now()), 16);
          }
          return origRAF(cb);
        };
      }
    }
  } catch (e) {
    console.warn("[DecideX] Could not initialize background heartbeat worker:", e);
  }

  // State globals
  let currentGameState = null;
  let currentRecommendation = null;
  let isLoveRunning = false;
  let lovePackageBuffer = null;

  const SUIT_SYMBOLS = { S: "♠", H: "♥", D: "♦", C: "♣" };
  const SUIT_NAMES = { S: "黑桃", H: "红桃", D: "方片", C: "梅花" };
  const RANK_VALUES = {
    "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9, "10": 10,
    "J": 10, "Q": 10, "K": 10, "A": 11
  };

  // Base poker hand definitions (chips, mult)
  const POKER_HANDS = {
    "Flush Five": { chips: 160, mult: 16 },
    "Flush House": { chips: 140, mult: 14 },
    "Five of a Kind": { chips: 120, mult: 12 },
    "Straight Flush": { chips: 100, mult: 8 },
    "Four of a Kind": { chips: 60, mult: 7 },
    "Full House": { chips: 40, mult: 4 },
    "Flush": { chips: 35, mult: 4 },
    "Straight": { chips: 30, mult: 4 },
    "Three of a Kind": { chips: 30, mult: 3 },
    "Two Pair": { chips: 20, mult: 2 },
    "Pair": { chips: 10, mult: 2 },
    "High Card": { chips: 5, mult: 1 }
  };

  // DOM Elements
  const canvas = document.getElementById("canvas");
  const splashOverlay = document.getElementById("splash-overlay");
  const progressBar = document.getElementById("progress-bar");
  const statusText = document.getElementById("status-text");
  const btnLaunch = document.getElementById("btn-launch");
  const decidexHud = document.getElementById("decidex-hud");
  const hudToggleBtn = document.getElementById("hud-toggle-btn");
  const hudHeader = document.querySelector(".hud-header");

  // Toggle HUD minimized state
  hudHeader.addEventListener("click", () => {
    decidexHud.classList.toggle("minimized");
    hudToggleBtn.textContent = decidexHud.classList.contains("minimized") ? "◀" : "▼";
  });

  let initialSyncState = "pending";
  let initialSyncStarted = false;
  function onInitSuccess() {
    if (isLoveRunning || initialSyncState !== "ready") return;
    isLoveRunning = true;
    splashOverlay.classList.add("hidden");
    console.log("[DecideX] Balatro initialized successfully in love.js WASM.");
    syncSaveFileSystem();
  }

  // Emscripten Module definition
  window.Module = {
    canvas: canvas,
    arguments: ["balatro.love"],
    args: ["balatro.love"],
    INITIAL_MEMORY: 268435456, // 256MB
    print: function (text) {
      if (text.startsWith("[DECIDEX_STATE] ")) {
        const jsonStr = text.substring("[DECIDEX_STATE] ".length);
        console.log("[DECIDEX_STATE]", jsonStr);
        try {
          const state = JSON.parse(jsonStr);
          window._lastDecidexState = state;
          window._decidexStateCount = (window._decidexStateCount || 0) + 1;
          window.onBalatroStateUpdate(state);
        } catch (e) {
          console.error("[DecideX] Failed to parse state JSON:", e);
        }
      } else if (text.startsWith("[DECIDEX_ACTION_RESULT] ")) {
        const resStr = text.substring("[DECIDEX_ACTION_RESULT] ".length);
        console.log("[DECIDEX_ACTION_RESULT]", resStr);
        if (window.AutoPilot && typeof window.AutoPilot.onActionResult === "function") {
          window.AutoPilot.onActionResult(resStr);
        }
      } else {
        console.log("[LÖVE stdout]", text);
      }
    },
    printErr: function (text) {
      console.warn("[LÖVE stderr]", text);
    },
    preRun: [setupFileSystem],
    postRun: [onInitSuccess]
  };

  const NORMALIZE1_LUA = `-- normalize1.lua for love.js
love.js = {}
local cache = {}
function love.js.eval(cmd)
  love.system = love.system or require('love.system')
  love.system.openURL('javascript:'..cmd)
  for i = 1, 2^32 do
    local line = io.read()
    if not line then break end
    cache[i] = line
  end
  local output = table.concat(cache, '\\n')
  for i = #cache, 1, -1 do cache[i] = nil end
  return output
end

local lfs = love.filesystem
local _lfs_getInfo = lfs.getInfo
local _lfs_read = lfs.read
function lfs.read(fn, ...)
  if not _lfs_getInfo(fn, 'file') then
    return nil, 'File does not exist: '..fn
  end
  return _lfs_read(fn, ...)
end

local _lfs_newFile = lfs.newFileData
function lfs.newFileData(fn, arg, ...)
  if not arg and not _lfs_getInfo(fn, 'file') then
    return nil, 'File does not exist: '..fn
  end
  return _lfs_newFile(fn, arg, ...)
end

local reg = debug.getregistry()
if reg and reg.File then
  local _File_open = reg.File.open
  reg.File.open = function(file, ...)
    local fn = file:getFilename()
    if not _lfs_getInfo(fn) then
      return nil, 'File does not exist: '..fn
    end
    return _File_open(file, ...)
  end
end
`;

  const NORMALIZE2_LUA = `-- normalize2.lua for love.js
if love.event then
  local _love_event_push = love.event.push
  function love.event.push(event, action, ...)
    if event == 'quit' and action == 'reload' then
      love.js.eval('window.location.reload();')
      return
    end
    return _love_event_push(event, action, ...)
  end
end

if love.audio then
  local playing = {}
  local function _cleanup_playing()
    for s in pairs(playing) do
      local ok, busy = pcall(s.isPlaying, s)
      if not ok or not busy then playing[s] = nil end
    end
  end
  local _love_audio_play = love.audio.play
  function love.audio.play(...)
    for i = 1, select("#", ...) do
      local source = select(i, ...)
      if source ~= nil then playing[source] = true end
    end
    return _love_audio_play(...)
  end

  local _love_audio_stop = love.audio.stop
  function love.audio.stop(source, ...)
    if source then return _love_audio_stop(source, ...) end
    _cleanup_playing()
    for s in pairs(playing) do s:stop(); playing[s] = nil end
  end

  local reg = debug.getregistry()
  if reg and reg.Source then
    local _Source_play = reg.Source.play
    reg.Source.play = function(source, ...)
      local res = _Source_play(source, ...)
      if res then playing[source] = true end
      return res
    end
  end
end

function love.system.getClipboardText()
  return love.js.eval([[(function(){return navigator.clipboard && navigator.clipboard.text ? navigator.clipboard.text : '';})();]])
end

function love.system.setClipboardText(text)
  local cmd = string.format([[(function(){if(navigator.clipboard){navigator.clipboard.writeText(%q).catch(function(){});}})();]], text)
  return love.js.eval(cmd)
end
`;

  function setupFileSystem() {
    const FS = (window.Module && window.Module.FS) || (typeof FS !== "undefined" ? FS : null);
    if (!FS) {
      console.warn("[DecideX] FS not yet available in setupFileSystem.");
      return;
    }

    // Set up window.open/window.prompt bridge for love.js.eval
    if (!window._decidex_open_hooked) {
      window._decidex_open_hooked = true;
      const originalOpen = window.open ? window.open.bind(window) : null;
      window._output = null;
      window.open = function (url) {
        if (typeof url === "string" && url.startsWith("javascript:")) {
          const code = url.substring(11);
          try {
            window._output = eval(code);
          } catch (e) {
            console.warn("[DecideX eval error]", e);
          }
          return null;
        }
        return originalOpen ? originalOpen(url) : null;
      };
      window.prompt = function () {
        const tmp = window._output;
        window._output = null;
        return tmp;
      };
    }

    // Install normalize1 and normalize2 shims for love.wasm bootloader
    try {
      FS.mkdirTree("/usr/local/share/lua/5.1");
      FS.createDataFile("/usr/local/share/lua/5.1", "normalize1.lua", NORMALIZE1_LUA, true, true, true);
      FS.createDataFile("/usr/local/share/lua/5.1", "normalize2.lua", NORMALIZE2_LUA, true, true, true);
      FS.createDataFile("/", "normalize1.lua", NORMALIZE1_LUA, true, true, true);
      FS.createDataFile("/", "normalize2.lua", NORMALIZE2_LUA, true, true, true);
      console.log("[DecideX] Installed normalize1.lua and normalize2.lua runtime shims.");
    } catch (e) {
      console.warn("[DecideX] Warning creating normalize shims:", e);
    }

    // Mount game archive into virtual FS
    if (lovePackageBuffer) {
      try {
        FS.createDataFile("/", "balatro.love", new Uint8Array(lovePackageBuffer), true, true, true);
        console.log("[DecideX] Successfully mounted balatro.love into virtual FS (/balatro.love), size:", lovePackageBuffer.byteLength);
      } catch (e) {
        console.warn("[DecideX] FS.createDataFile warning (may already exist):", e);
      }
    } else {
      console.error("[DecideX] lovePackageBuffer is empty during setupFileSystem!");
    }

    // Prepare save directory inside /home/web_user (which love.js already mounts to IDBFS)
    try {
      FS.mkdirTree("/home/web_user/.local/share/love/Balatro");
    } catch (e) {
      // Ignored if already exists
    }

    // Wrap the runtime's single initial sync so the UI observes its completion.
    // love.js starts FS.syncfs(true) after preRun; starting another sync here races
    // with that operation and can leave the UI waiting on a stale pending state.
    if (FS && FS.syncfs && !FS._decidex_syncfs_wrapped) {
      FS._decidex_syncfs_wrapped = true;
      const originalSyncfs = FS.syncfs;
      FS.syncfs = function (populate, callback) {
        let done = false;
        const timeoutMs = populate ? 15000 : 5000;
        const timer = setTimeout(() => {
          if (done) return;
          done = true;
          const error = new Error(`FS.syncfs timed out after ${timeoutMs}ms`);
          console.error("[DecideX] FS.syncfs failed:", error);
          if (populate) {
            initialSyncState = "failed";
            initialSyncStarted = false;
            btnLaunch.style.display = "block";
            statusText.textContent = "存档同步失败，请点击重试";
          }
          if (callback) callback(error);
        }, timeoutMs);
        try {
          originalSyncfs.call(FS, populate, function (err) {
            clearTimeout(timer);
            if (done) return;
            done = true;
            if (populate) {
              initialSyncStarted = false;
              if (err) {
                initialSyncState = "failed";
                btnLaunch.style.display = "block";
                statusText.textContent = "存档同步失败，请点击重试";
                console.error("[DecideX] Initial IDBFS sync failed:", err);
              } else {
                initialSyncState = "ready";
                console.log("[DecideX] Initial IDBFS sync completed.");
              }
            }
            if (callback) callback(err);
          });
        } catch (err) {
          clearTimeout(timer);
          if (done) return;
          done = true;
          if (populate) {
            initialSyncState = "failed";
            initialSyncStarted = false;
            btnLaunch.style.display = "block";
            statusText.textContent = "存档同步失败，请点击重试";
            console.error("[DecideX] Initial IDBFS sync failed:", err);
          }
          if (callback) callback(err);
        }
      };
    }
  }

  function syncSaveFileSystem() {
    const FS = window.Module && window.Module.FS;
    if (FS && FS.syncfs) {
      FS.syncfs(false, function (err) {
        if (err) console.warn("[DecideX] IDBFS write sync error:", err);
      });
    }
  }

  function syncInitialFileSystem() {
    const FS = window.Module && window.Module.FS;
    if (!FS || !FS.syncfs) {
      initialSyncState = "failed";
      throw new Error("IDBFS sync is unavailable");
    }
    initialSyncState = "pending";
    initialSyncStarted = true;
    btnLaunch.style.display = "none";
    statusText.textContent = "正在同步本地存档，请稍候...";
    FS.syncfs(true, function (err) {
      if (err) {
        initialSyncState = "failed";
        initialSyncStarted = false;
        btnLaunch.style.display = "block";
        statusText.textContent = "存档同步失败，请点击重试";
        console.error("[DecideX] Initial IDBFS sync failed:", err);
        return;
      }
      initialSyncState = "ready";
      initialSyncStarted = false;
      onInitSuccess();
    });
  }

  window.retryInitialFileSystemSync = function () {
    try {
      syncInitialFileSystem();
    } catch (err) {
      initialSyncState = "failed";
      initialSyncStarted = false;
      btnLaunch.style.display = "block";
      statusText.textContent = "存档同步不可用，请重试";
      console.error("[DecideX] Initial IDBFS sync retry failed:", err);
    }
  };

  // Auto-sync save data periodically
  setInterval(syncSaveFileSystem, 15000);
  window.addEventListener("beforeunload", syncSaveFileSystem);

  // Core persistent cache identifiers (versioned to ensure patch alignment)
  // Cache key: balatro-asset-cache-v68 / decidex-balatro-core-v68
  const EXPECTED_LOVE_SIZE = 56342649;
  const EXPECTED_WASM_SIZE = 4720510;
  const ASSET_VERSION = "v81";
  const CORE_CACHE_NAME = "decidex-balatro-core-" + ASSET_VERSION;
  const IDB_DB_NAME = "decidex_balatro_store_v2";
  const IDB_STORE_NAME = "game_assets";
  const ASSET_LOVE_URL = "balatro.love.zip?v=81";
  const ASSET_WASM_URL = "love.wasm.bin?v=81";

  // IndexedDB Storage Engine (immune to HTTP cache busters and query parameters)
  function openAssetDB() {
    return new Promise((resolve, reject) => {
      if (!window.indexedDB) {
        return reject(new Error("IndexedDB is not supported"));
      }
      const req = indexedDB.open(IDB_DB_NAME, 1);
      req.onupgradeneeded = (e) => {
        const db = e.target.result;
        if (!db.objectStoreNames.contains(IDB_STORE_NAME)) {
          db.createObjectStore(IDB_STORE_NAME);
        }
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }

  async function getFromIDB(key) {
    try {
      const db = await openAssetDB();
      return new Promise((resolve) => {
        const tx = db.transaction(IDB_STORE_NAME, "readonly");
        const store = tx.objectStore(IDB_STORE_NAME);
        const req = store.get(key);
        req.onsuccess = () => resolve(req.result || null);
        req.onerror = () => resolve(null);
      });
    } catch (e) {
      console.warn("[DecideX IDB] Read error for " + key, e);
      return null;
    }
  }

  async function putToIDB(key, buffer) {
    try {
      const db = await openAssetDB();
      return new Promise((resolve, reject) => {
        const tx = db.transaction(IDB_STORE_NAME, "readwrite");
        const store = tx.objectStore(IDB_STORE_NAME);
        const req = store.put(buffer, key);
        req.onsuccess = () => resolve(true);
        req.onerror = () => reject(req.error);
      });
    } catch (e) {
      console.warn("[DecideX IDB] Write error for " + key, e);
      return false;
    }
  }

  // Cache reset and reload utility
  window.decidexClearCacheAndReload = async function () {
    if (window.caches) {
      try {
        const keys = await caches.keys();
        for (const k of keys) {
          await caches.delete(k);
        }
      } catch (e) {
        console.warn("Failed clearing caches", e);
      }
    }
    if (window.indexedDB) {
      try {
        indexedDB.deleteDatabase(IDB_DB_NAME);
      } catch (e) {
        console.warn("Failed deleting IndexedDB", e);
      }
    }
    window.location.reload();
  };
  window.onInitSuccess = onInitSuccess;

  // Search across version-matched CacheStorage
  async function findInCacheStorage(urlPattern) {
    if (!window.caches) return null;
    try {
      const cache = await caches.open(CORE_CACHE_NAME);
      let res = await cache.match(urlPattern);
      if (res) return await res.arrayBuffer();
      const requests = await cache.keys();
      for (const req of requests) {
        if (req.url.includes(urlPattern)) {
          res = await cache.match(req);
          if (res) return await res.arrayBuffer();
        }
      }
    } catch (e) {
      console.warn("[DecideX CacheStorage] Search error:", e);
    }
    return null;
  }

  // Dual-Engine Asset Loader (IndexedDB + CacheStorage + Streaming Fallback)
  async function loadBalatroPackage() {
    statusText.textContent = "正在检查本地离线持久缓存 (免重复下载)...";
    progressBar.style.width = "10%";

    try {
      let loveBuf = null;
      let wasmBuf = null;

      // Tier 1: Check native IndexedDB first (Fastest, zero network, immune to URL queries)
      loveBuf = await getFromIDB("balatro.love");
      wasmBuf = await getFromIDB("love.wasm");

      // Tier 2: Check CacheStorage (including legacy caches) if IDB missed
      if (!loveBuf) {
        loveBuf = await findInCacheStorage("balatro.love");
        if (loveBuf) putToIDB("balatro.love", loveBuf); // auto-migrate to IDB
      }
      if (!wasmBuf) {
        wasmBuf = await findInCacheStorage("love.wasm");
        if (wasmBuf) putToIDB("love.wasm", wasmBuf); // auto-migrate to IDB
      }

      // Validate cache size integrity
      if (loveBuf && loveBuf.byteLength !== EXPECTED_LOVE_SIZE) {
        console.warn(`[DecideX Cache] Stale or invalid balatro.love size (${loveBuf.byteLength} != ${EXPECTED_LOVE_SIZE}), purging cache.`);
        loveBuf = null;
      }
      if (wasmBuf && wasmBuf.byteLength !== EXPECTED_WASM_SIZE) {
        console.warn(`[DecideX Cache] Stale or invalid love.wasm size (${wasmBuf.byteLength} != ${EXPECTED_WASM_SIZE}), purging cache.`);
        wasmBuf = null;
      }

      // If both assets hit local persistent cache: instant load, 0 network transfer!
      if (loveBuf && wasmBuf) {
        console.log(`[DecideX Cache] Instant hit! Loaded balatro.love (${(loveBuf.byteLength / 1048576).toFixed(1)}MB) and love.wasm (${(wasmBuf.byteLength / 1048576).toFixed(1)}MB) from local offline store.`);
        statusText.textContent = "已从本地离线持久缓存秒开就绪！";
        progressBar.style.width = "100%";
        lovePackageBuffer = loveBuf;
        window.Module.cache = window.Module.cache || {};
        window.Module.cache["11.5/love.wasm"] = wasmBuf;
        btnLaunch.style.display = "block";
        progressBar.parentElement.style.display = "none";
        return;
      }

      // Tier 3: Cache miss (first visit): stream download balatro.love (0% -> 85%) with Range resume
      statusText.textContent = "首次下载游戏资源包 (balatro.love 53.7MB)...";

      async function fetchWithResume(url, initialTotal, onProgress) {
        let loaded = 0;
        const chunks = [];
        let total = initialTotal;
        let attempts = 0;

        while (loaded < total && attempts < 20) {
          attempts++;
          try {
            const headers = loaded > 0 ? { "Range": `bytes=${loaded}-` } : {};
            const res = await fetch(url, { headers });
            if (loaded > 0) {
              if (res.status === 200) {
                console.warn(`[DecideX Download] Server returned 200 instead of 206 for Range request, resetting stream from 0`);
                chunks.length = 0;
                loaded = 0;
              } else if (res.status !== 206) {
                throw new Error(`HTTP ${res.status} when requesting range bytes=${loaded}-`);
              }
            } else if (!res.ok) {
              throw new Error(`HTTP ${res.status} fetching ${url}`);
            }
            if (loaded === 0) {
              const cl = res.headers.get("content-length");
              const cr = res.headers.get("content-range");
              if (cr) {
                const m = cr.match(/\/(\d+)/);
                if (m) total = parseInt(m[1], 10);
              } else if (cl) {
                total = parseInt(cl, 10);
              }
            }

            const reader = res.body.getReader();
            while (true) {
              const { done, value } = await reader.read();
              if (done) break;
              chunks.push(value);
              loaded += value.length;
              onProgress(loaded, total);
            }
          } catch (e) {
            console.warn(`[DecideX Download] Stream blip at ${(loaded / 1048576).toFixed(1)}MB / ${(total / 1048576).toFixed(1)}MB, resuming in 1s...`, e);
            if (loaded >= total) break;
            await new Promise(r => setTimeout(r, 1000));
          }
        }

        if (loaded < total) {
          throw new Error(`Download incomplete: ${loaded}/${total} bytes after ${attempts} attempts`);
        }

        const fullBuffer = new Uint8Array(loaded);
        let offset = 0;
        for (const chunk of chunks) {
          fullBuffer.set(chunk, offset);
          offset += chunk.length;
        }
        return fullBuffer.buffer;
      }

      lovePackageBuffer = await fetchWithResume(ASSET_LOVE_URL, EXPECTED_LOVE_SIZE, (loaded, total) => {
        const pct = Math.min(85, Math.round((loaded / total) * 85));
        progressBar.style.width = pct + "%";
        statusText.textContent = `下载游戏包 (1/2): ${(loaded / (1024 * 1024)).toFixed(1)} MB / ${(total / (1024 * 1024)).toFixed(1)} MB (${pct}%)`;
      });

      // Tier 4: Download love.wasm (85% -> 98%) with Range resume
      statusText.textContent = "正在下载 WebAssembly 运行核心 (love.wasm 4.7MB)...";
      progressBar.style.width = "88%";
      wasmBuf = await fetchWithResume(ASSET_WASM_URL, EXPECTED_WASM_SIZE, (loaded, total) => {
        const pct = 85 + Math.min(13, Math.round((loaded / total) * 13));
        progressBar.style.width = pct + "%";
        statusText.textContent = `下载运行核心 (2/2): ${(loaded / (1024 * 1024)).toFixed(1)} MB / ${(total / (1024 * 1024)).toFixed(1)} MB (${pct}%)`;
      });
      window.Module.cache = window.Module.cache || {};
      window.Module.cache["11.5/love.wasm"] = wasmBuf;

      // Tier 5: Persist both to IndexedDB AND CacheStorage in parallel
      statusText.textContent = "正在写入本地持久存储 (下次刷新/重启免下载)...";
      progressBar.style.width = "99%";

      const persistPromises = [
        putToIDB("balatro.love", lovePackageBuffer),
        putToIDB("love.wasm", wasmBuf)
      ];

      if (window.caches) {
        persistPromises.push(
          caches.open(CORE_CACHE_NAME).then(cache => {
            return Promise.all([
              cache.put(ASSET_LOVE_URL, new Response(lovePackageBuffer.slice(0), {
                headers: {
                  "Content-Type": "application/octet-stream",
                  "Content-Length": String(lovePackageBuffer.byteLength),
                  "Cache-Control": "public, max-age=31536000, immutable"
                }
              })),
              cache.put(ASSET_WASM_URL, new Response(wasmBuf.slice(0), {
                headers: {
                  "Content-Type": "application/wasm",
                  "Content-Length": String(wasmBuf.byteLength),
                  "Cache-Control": "public, max-age=31536000, immutable"
                }
              }))
            ]);
          }).catch(e => console.warn("[DecideX] CacheStorage put error:", e))
        );
      }

      await Promise.allSettled(persistPromises);
      console.log("[DecideX] Successfully persisted assets into IndexedDB and CacheStorage!");

      progressBar.style.width = "100%";
      statusText.textContent = "所有核心资源校验与持久化缓存完成，就绪！";

      // Show launch button
      btnLaunch.style.display = "block";
      progressBar.parentElement.style.display = "none";
    } catch (err) {
      statusText.innerHTML = `
        <span style="color:#ef4444;">下载失败: ${err.message}</span><br>
        <button class="btn-util" style="margin-top:10px;padding:6px 14px;background:#3b82f6;color:#fff;border:none;border-radius:4px;cursor:pointer;" onclick="location.reload()">重新载入</button>
      `;
      console.error(err);
    }
  }

  // Launch LÖVE runtime
  btnLaunch.addEventListener("click", () => {
    if (!lovePackageBuffer) {
      console.warn("[DecideX] Game assets not loaded yet, ignoring launch click.");
      return;
    }
    if (initialSyncState === "failed") {
      window.retryInitialFileSystemSync();
      return;
    }
    btnLaunch.style.display = "none";
    statusText.textContent = "正在启动 WebAssembly 虚拟机与渲染画布...";

    function bootLove() {
      if (typeof Love === "function") {
        try {
          statusText.textContent = "正在初始化 WebGL 上下文与图形管道...";
          Love(window.Module);
        } catch (err) {
          console.error("[DecideX] Love initialization error:", err);
          statusText.innerHTML = `
            <span style="color:#ef4444;">虚拟机初始化错误: ${err.message}</span><br>
            <button class="btn-util" style="margin-top:10px;padding:6px 14px;background:#ef4444;color:#fff;border:none;border-radius:4px;cursor:pointer;" onclick="decidexClearCacheAndReload()">清除本地缓存重试</button>
          `;
        }
      } else {
        console.log("[DecideX] Loading love.js dynamically...");
        const script = document.createElement("script");
        script.src = "love.js?v=61";
        script.onload = () => {
          if (typeof Love === "function") {
            try {
              Love(window.Module);
            } catch (err) {
              console.error("[DecideX] Love boot error:", err);
            }
          } else {
            console.error("[DecideX] Love function not found on window!");
          }
        };
        script.onerror = (e) => {
          statusText.innerHTML = `
            <span style="color:#ef4444;">加载 love.js 运行时失败</span><br>
            <button class="btn-util" style="margin-top:10px;padding:6px 14px;background:#ef4444;color:#fff;border:none;border-radius:4px;cursor:pointer;" onclick="decidexClearCacheAndReload()">清除本地缓存重试</button>
          `;
          console.error(e);
        };
        document.body.appendChild(script);
      }
    }

    bootLove();

    // Active progressive watchdog: ensures game canvas reveals smoothly without blocking
    let bootElapsed = 0;
    const bootWatchdog = setInterval(() => {
      bootElapsed += 250;
      if (isLoveRunning) {
        clearInterval(bootWatchdog);
        return;
      }
      if (window.Module && window.Module.asm && (window._lastDecidexState || bootElapsed >= 1500)) {
        console.log("[DecideX] Watchdog: WASM runtime ready, revealing game canvas.");
        clearInterval(bootWatchdog);
        onInitSuccess();
      } else if (bootElapsed >= 6000) {
        clearInterval(bootWatchdog);
        console.warn("[DecideX] Runtime startup is taking longer than expected.");
        statusText.textContent = initialSyncState === "pending"
          ? "正在同步本地存档，请稍候..."
          : "游戏仍在启动，请稍候...";
      }
    }, 250);
  });

  const RANK_NUM = {
    "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9, "10": 10,
    "J": 11, "Q": 12, "K": 13, "A": 14
  };

  // Helper to check Straight
  function isStraight(cards) {
    if (!cards || cards.length < 5) return false;
    const nums = [...new Set(cards.map(c => RANK_NUM[c.rank] || 0))].sort((a, b) => b - a);
    if (nums.length < 5) return false;
    for (let i = 0; i <= nums.length - 5; i++) {
      if (nums[i] - nums[i + 4] === 4 && new Set(nums.slice(i, i + 5)).size === 5) return true;
    }
    // Ace-low wheel: A-2-3-4-5
    if (nums.includes(14) && nums.includes(2) && nums.includes(3) && nums.includes(4) && nums.includes(5)) {
      return true;
    }
    return false;
  }

  function escapeHtml(str) {
    if (str === null || str === undefined) return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  // --- Four Pillars & Strategy Systems ---
  const JOKER_PILLAR_MAP = {
    // CHIPS
    "Blue Joker": "chips", "Bull": "chips", "Castle": "chips", "Ice Cream": "chips",
    "Scary Face": "chips", "Banner": "chips", "Odd Todd": "chips", "Stuntman": "chips",
    "Square Joker": "chips", "Wee Joker": "chips", "Hiker": "chips", "Arrowhead": "chips",
    // FLAT_MULT
    "Gros Michel": "flat_mult", "Half Joker": "flat_mult", "Ride the Bus": "flat_mult",
    "Green Joker": "flat_mult", "Abstract Joker": "flat_mult", "Even Steven": "flat_mult",
    "Joker": "flat_mult", "Misprint": "flat_mult", "Red Card": "flat_mult", "Supernova": "flat_mult",
    "Swashbuckler": "flat_mult", "Fortune Teller": "flat_mult", "Flash Card": "flat_mult",
    // X_MULT
    "Cavendish": "x_mult", "Card Sharp": "x_mult", "The Duo": "x_mult", "The Trio": "x_mult",
    "The Family": "x_mult", "The Order": "x_mult", "The Tribe": "x_mult", "Constellation": "x_mult",
    "Hologram": "x_mult", "Baron": "x_mult", "Blackboard": "x_mult", "Baseball Card": "x_mult",
    "Steel Joker": "x_mult", "Ancient Joker": "x_mult", "Campfire": "x_mult", "Obelisk": "x_mult",
    // ECONOMY
    "Golden Joker": "economy", "Mail-In Rebate": "economy", "Trading Card": "economy",
    "To the Moon": "economy", "Rocket": "economy", "Business Card": "economy",
    "Egg": "economy", "Cloud 9": "economy", "Delayed Gratification": "economy",
    // UTILITY
    "Blueprint": "utility", "Brainstorm": "utility", "Four Fingers": "utility",
    "Shortcut": "utility", "Splash": "utility", "Mime": "utility", "Hanging Chad": "utility",
    "Hack": "utility", "Dusk": "utility", "Sock and Buskin": "utility"
  };

  function analyzeSynergy(jokers) {
    let chips = 0, flatMult = 0, xMult = 0, economy = 0;
    for (const j of (jokers || [])) {
      if (j.debuff) continue;
      const p = JOKER_PILLAR_MAP[j.name] || (j.xmult && j.xmult > 1 ? "x_mult" : (j.mult >= 8 ? "flat_mult" : (j.chips >= 30 ? "chips" : "utility")));
      if (p === "chips") chips += (j.chips || 50);
      else if (p === "flat_mult") flatMult += (j.mult || 12);
      else if (p === "x_mult") xMult += (j.xmult || 1.5);
      else if (p === "economy") economy += 1;
    }

    let primaryNeed = "balanced";
    if (flatMult < 10 && chips < 40) primaryNeed = "flat_mult";
    else if (chips < 40) primaryNeed = "chips";
    else if (flatMult < 15) primaryNeed = "flat_mult";
    else if (xMult === 0) primaryNeed = "x_mult";
    else if (economy === 0) primaryNeed = "economy";

    return { chips, flatMult, xMult, economy, primaryNeed };
  }

  function computeOptimalJokerOrder(jokers) {
    if (!jokers || jokers.length <= 1) return { needsReorder: false, reason: "", optimalIndices: [] };
    const n = jokers.length;

    // Edition-aware and copy-aware position tier
    function getJokerTier(j) {
      if (j.name === "Blueprint") return 4.9;
      if (j.name === "Brainstorm") return 0.1;

      const p = JOKER_PILLAR_MAP[j.name] || (j.xmult > 1 ? "x_mult" : (j.mult > 0 ? "flat_mult" : "chips"));
      let tier = 2;
      if (p === "chips") tier = 1;
      else if (p === "flat_mult") tier = 2;
      else if (p === "x_mult") tier = 5;
      else if (p === "economy") tier = 0;
      else if (p === "utility") tier = 3;

      const ed = String(j.edition || "").toLowerCase();
      if (ed === "polychrome") {
        if (tier === 2) tier = 2.8; // Flat mult with poly triggers flat mult then x1.5
        else if (tier < 5) tier = 5.0; // Chips/economy with poly acts purely as xmult
      } else if (ed === "holo" || ed === "holographic") {
        if (tier < 2) tier = 2.0;
      }
      return tier;
    }

    const tiers = jokers.map(getJokerTier);

    // Detect inverted X-Mult: X-Mult (tier >= 4.8) sitting to the left of Flat Mult (2.0 <= tier < 4.0)
    let hasInvertedXMult = false;
    let maxFlatPos = -1;
    let minXMultPos = 999;
    for (let i = 0; i < n; i++) {
      if (tiers[i] >= 2.0 && tiers[i] < 4.0 && i > maxFlatPos) maxFlatPos = i;
      if (tiers[i] >= 4.8 && i < minXMultPos) minXMultPos = i;
    }
    if (minXMultPos < maxFlatPos) hasInvertedXMult = true;

    const indexed = jokers.map((_, i) => i);
    indexed.sort((a, b) => tiers[a] - tiers[b]);

    // Copy jokers dynamic mapping
    const blueprintIdx = jokers.findIndex(j => j.name === "Blueprint");
    const brainstormIdx = jokers.findIndex(j => j.name === "Brainstorm");
    const xmultIndices = jokers.map((j, i) => i).filter(i => tiers[i] >= 4.8 || (jokers[i].xmult || 1) > 1);

    if (brainstormIdx !== -1) {
      const candidates = jokers.map((_, i) => i).filter(i => !["Brainstorm", "Blueprint"].includes(jokers[i].name));
      if (candidates.length > 0) {
        const flatOrChips = candidates.filter(i => tiers[i] < 4.0);
        let bestTarget;
        if (flatOrChips.length > 0) {
          bestTarget = flatOrChips.sort((a, b) => (tiers[b] - tiers[a]) || (jokers[b].mult || 0) - (jokers[a].mult || 0))[0];
        } else {
          bestTarget = candidates.sort((a, b) => (jokers[b].xmult || 1) - (jokers[a].xmult || 1))[0];
        }
        const curPos = indexed.indexOf(bestTarget);
        if (curPos !== -1) {
          indexed.splice(curPos, 1);
          indexed.unshift(bestTarget);
        }
      }
    }

    if (blueprintIdx !== -1 && xmultIndices.length > 0) {
      const validX = xmultIndices.filter(i => indexed.indexOf(i) > 0);
      if (validX.length > 0) {
        const bestX = validX.sort((a, b) => (jokers[b].xmult || 1) - (jokers[a].xmult || 1))[0];
        const bpPos = indexed.indexOf(blueprintIdx);
        if (bpPos !== -1) indexed.splice(bpPos, 1);
        const xPos = indexed.indexOf(bestX);
        indexed.splice(xPos, 0, blueprintIdx);
      }
    }

    const currentOrder = jokers.map((_, i) => i);
    const needsReorder = indexed.some((idx, i) => idx !== currentOrder[i]);

    if (!needsReorder) {
      return { needsReorder: false, reason: "小丑排序符合最佳数学法则 (基础 -> 乘倍)", optimalIndices: [] };
    }

    // Balatro Lua indexes jokers 1-based
    const optimal1Based = indexed.map(i => i + 1);
    let reason = "调整小丑排布顺序，严格遵守 [筹码 -> 基础倍率 -> 乘法倍率] 递增放大链";
    if (hasInvertedXMult) {
      reason = "严重排位缺陷：乘法倍率 (X-Mult) 位于加法倍率左侧！重排后倍率收益大幅提升";
    } else if (blueprintIdx !== -1 || brainstormIdx !== -1) {
      reason = "优化复制小丑 (Blueprint/Brainstorm) 锚定位置以实现指数级输出";
    }

    return {
      needsReorder: true,
      reason: reason,
      optimalIndices: optimal1Based
    };
  }

  function detectConstraints(jokers, blind) {
    const bname = (blind && blind.name) || "";
    const hasRideTheBus = (jokers || []).some(j => j.name === "Ride the Bus" && !j.debuff);
    const hasGreenJoker = (jokers || []).some(j => j.name === "Green Joker" && !j.debuff);
    const hasSquareJoker = (jokers || []).some(j => j.name === "Square Joker" && !j.debuff);
    const isNeedle = (bname === "The Needle");
    const isWater = (bname === "The Water");
    const isEye = (bname === "The Eye");
    const isPsychic = (bname === "The Psychic");
    const isMouth = (bname === "The Mouth");
    const isFlint = (bname === "The Flint");
    const isPlant = (bname === "The Plant");
    const isTooth = (bname === "The Tooth");
    const isOx = (bname === "The Ox");
    const debuffedSuit = {
      "The Club": "C",
      "The Goad": "S",
      "The Window": "D",
      "The Head": "H"
    }[bname] || null;

    return {
      noFaceCards: hasRideTheBus,
      avoidDiscard: hasGreenJoker || isWater,
      hasSquareJoker: hasSquareJoker,
      oneHandOnly: isNeedle,
      noRepeatedHands: isEye,
      requires5Cards: isPsychic,
      singleHandType: isMouth,
      halvedBase: isFlint,
      debuffFaceCards: isPlant,
      costsMoneyPerCard: isTooth,
      wipesMoneyOnMostPlayed: isOx,
      debuffedSuit: debuffedSuit,
      bossName: bname,
      upcomingBoss: (blind && blind.upcoming_boss) || null
    };
  }

  // Scoring Cards Filter according to Balatro official rules
  function getScoringCards(selectedCards, handName, jokers) {
    if (!selectedCards || selectedCards.length === 0) return [];
    // Splash Joker: every played card counts in scoring
    const hasSplash = (jokers || []).some(j => j.name === "Splash" && !j.debuff);
    if (hasSplash) return [...selectedCards];

    // Stone cards always score regardless of hand
    const stoneCards = selectedCards.filter(c => c.enhancement === "stone");
    const normalCards = selectedCards.filter(c => c.enhancement !== "stone");

    const rankCounts = {};
    for (const c of normalCards) {
      rankCounts[c.rank] = (rankCounts[c.rank] || 0) + 1;
    }

    let scoring = [];
    if (handName === "Five of a Kind" || handName === "Flush Five") {
      const topRank = Object.keys(rankCounts).find(r => rankCounts[r] >= 5);
      scoring = normalCards.filter(c => c.rank === topRank).slice(0, 5);
    } else if (handName === "Four of a Kind") {
      const topRank = Object.keys(rankCounts).find(r => rankCounts[r] >= 4);
      scoring = normalCards.filter(c => c.rank === topRank).slice(0, 4);
    } else if (handName === "Full House" || handName === "Flush House") {
      const tripRank = Object.keys(rankCounts).find(r => rankCounts[r] === 3);
      const pairRank = Object.keys(rankCounts).find(r => rankCounts[r] >= 2 && r !== tripRank);
      scoring = normalCards.filter(c => c.rank === tripRank || c.rank === pairRank);
    } else if (handName === "Flush" || handName === "Straight Flush") {
      const suitCounts = {};
      for (const c of normalCards) suitCounts[c.suit] = (suitCounts[c.suit] || 0) + 1;
      const flushSuit = Object.keys(suitCounts).find(s => suitCounts[s] >= 5);
      scoring = normalCards.filter(c => c.suit === flushSuit).slice(0, 5);
    } else if (handName === "Straight") {
      const sorted = [...normalCards].sort((a, b) => (RANK_NUM[b.rank] || 0) - (RANK_NUM[a.rank] || 0));
      const seenRanks = new Set();
      const uniqueSorted = [];
      for (const c of sorted) {
        if (!seenRanks.has(c.rank)) {
          seenRanks.add(c.rank);
          uniqueSorted.push(c);
        }
      }
      let foundStraight = false;
      for (let i = 0; i <= uniqueSorted.length - 5; i++) {
        const sub = uniqueSorted.slice(i, i + 5);
        if ((RANK_NUM[sub[0].rank] - RANK_NUM[sub[4].rank]) === 4) {
          scoring = sub;
          foundStraight = true;
          break;
        }
      }
      if (!foundStraight && seenRanks.has("A") && seenRanks.has("2") && seenRanks.has("3") && seenRanks.has("4") && seenRanks.has("5")) {
        scoring = uniqueSorted.filter(c => ["A", "2", "3", "4", "5"].includes(c.rank));
      }
      if (scoring.length === 0) scoring = normalCards.slice(0, 5);
    } else if (handName === "Three of a Kind") {
      const topRank = Object.keys(rankCounts).find(r => rankCounts[r] >= 3);
      scoring = normalCards.filter(c => c.rank === topRank).slice(0, 3);
    } else if (handName === "Two Pair") {
      const pairRanks = Object.keys(rankCounts).filter(r => rankCounts[r] >= 2).sort((a, b) => (RANK_NUM[b] || 0) - (RANK_NUM[a] || 0)).slice(0, 2);
      scoring = normalCards.filter(c => pairRanks.includes(c.rank));
    } else if (handName === "Pair") {
      const pairRank = Object.keys(rankCounts).find(r => rankCounts[r] >= 2);
      scoring = normalCards.filter(c => c.rank === pairRank).slice(0, 2);
    } else {
      // High Card: single highest card
      const highest = [...normalCards].sort((a, b) => (RANK_VALUES[b.rank] || 0) - (RANK_VALUES[a.rank] || 0))[0];
      scoring = highest ? [highest] : [];
    }

    const combined = [...scoring];
    for (const sc of stoneCards) {
      if (!combined.includes(sc)) combined.push(sc);
    }
    return combined;
  }

  // Enhanced poker evaluator with card enhancements & joker abilities
  function evaluateHandScore(selectedCards, jokers, blind, stateContext) {
    if (!selectedCards || selectedCards.length === 0) return null;

    const constraints = detectConstraints(jokers, blind);
    const rankCounts = {};
    const suitCounts = {};
    for (const c of selectedCards) {
      rankCounts[c.rank] = (rankCounts[c.rank] || 0) + 1;
      suitCounts[c.suit] = (suitCounts[c.suit] || 0) + 1;
    }

    const counts = Object.values(rankCounts).sort((a, b) => b - a);
    const maxSuitCount = Math.max(...Object.values(suitCounts));
    const isFlush = maxSuitCount >= 5;
    const straight = isStraight(selectedCards);

    // 1. Hand classification
    let handName = "High Card";
    if (counts[0] >= 5 && isFlush) handName = "Flush Five";
    else if (counts[0] === 3 && counts[1] >= 2 && isFlush) handName = "Flush House";
    else if (counts[0] >= 5) handName = "Five of a Kind";
    else if (straight && isFlush) handName = "Straight Flush";
    else if (counts[0] === 4) handName = "Four of a Kind";
    else if (counts[0] === 3 && counts[1] >= 2) handName = "Full House";
    else if (isFlush) handName = "Flush";
    else if (straight) handName = "Straight";
    else if (counts[0] === 3) handName = "Three of a Kind";
    else if (counts[0] === 2 && counts[1] === 2) handName = "Two Pair";
    else if (counts[0] === 2) handName = "Pair";

    // 2. Identify scoring cards (only scoring cards trigger base chips, enhancement chips/mult, and edition)
    const scoringCards = getScoringCards(selectedCards, handName, jokers);

    let cardChips = 0;
    let cardMult = 0;
    let cardXMult = 1.0;

    for (const c of scoringCards) {
      const isCardDebuffed = Boolean(c.debuff) ||
        Boolean(constraints.debuffedSuit && c.suit === constraints.debuffedSuit) ||
        Boolean(constraints.debuffFaceCards && ["J", "Q", "K"].includes(c.rank));
      if (isCardDebuffed) {
        continue;
      }

      let cChips = (c.enhancement === "stone" ? 50 : (RANK_VALUES[c.rank] || 0));
      let cMult = 0;
      let cXMult = 1.0;

      // Card enhancement
      if (c.enhancement === "bonus") cChips += 30;
      else if (c.enhancement === "mult") cMult += 4;
      else if (c.enhancement === "glass") cXMult *= 2.0;
      else if (c.enhancement === "lucky") { cChips += 10; cMult += 2; }
      // Note: Steel card gives NO bonus when played (it triggers while held in hand)

      // Card edition
      if (c.edition === "foil") cChips += 50;
      else if (c.edition === "holo") cMult += 10;
      else if (c.edition === "poly") cXMult *= 1.5;

      // Seal retriggers
      const repeats = (c.seal === "red") ? 2 : 1;
      cardChips += cChips * repeats;
      cardMult += cMult * repeats;
      cardXMult *= Math.pow(cXMult, repeats);
    }

    // 3. Steel card held in hand: 1.5x Mult per unplayed steel card
    if (stateContext && stateContext.hand) {
      const selectedIndices = new Set(selectedCards.map(c => c.index).filter(x => x !== undefined));
      const unplayedCards = stateContext.hand.filter(c => !selectedIndices.has(c.index));
      for (const uc of unplayedCards) {
        const isUcDebuffed = Boolean(uc.debuff) ||
          Boolean(constraints.debuffedSuit && uc.suit === constraints.debuffedSuit);
        if (!isUcDebuffed && uc.enhancement === "steel") {
          const repeats = (uc.seal === "red") ? 2 : 1;
          cardXMult *= Math.pow(1.5, repeats);
        }
      }
    }

    const base = POKER_HANDS[handName] || { chips: 10, mult: 1 };
    let chips = base.chips + cardChips;
    let mult = base.mult + cardMult;
    let xmult = cardXMult;

    // 4. Apply Joker effects & build breakdown
    const activeJokerBadges = [];
    const scoringSuits = {};
    for (const sc of scoringCards) {
      scoringSuits[sc.suit] = (scoringSuits[sc.suit] || 0) + 1;
    }

    for (const j of (jokers || [])) {
      if (j.debuff) continue;
      let jChips = Number(j.chips) || 0;
      let jMult = Number(j.mult) || 0;
      let jXmult = Number(j.xmult) || 1.0;

      // Edition bonus on Joker
      if (j.edition === "foil") jChips += 50;
      else if (j.edition === "holo") jMult += 10;
      else if (j.edition === "poly") jXmult *= 1.5;

      // Extra counters on scaling Jokers (avoid double-counting if j.mult is already populated)
      if (j.extra && typeof j.extra === "object") {
        if (jChips === 0 && j.extra.chips) jChips += Number(j.extra.chips) || 0;
        if (jMult === 0 && j.extra.mult) jMult += Number(j.extra.mult) || 0;
        if (jXmult === 1.0 && (j.extra.x_mult || j.extra.xmult)) {
          jXmult *= Number(j.extra.x_mult || j.extra.xmult) || 1.0;
        }
      }

      // Conditional suit/rank jokers trigger on scoring cards only
      const jname = j.name || "";
      if (jname === "Lusty Joker") jMult += 4 * (scoringSuits["H"] || 0);
      else if (jname === "Greedy Joker") jMult += 4 * (scoringSuits["D"] || 0);
      else if (jname === "Wrathful Joker") jMult += 4 * (scoringSuits["S"] || 0);
      else if (jname === "Gluttonous Joker") jMult += 4 * (scoringSuits["C"] || 0);
      else if (jname === "Even Steven") {
        const evens = scoringCards.filter(c => ["2", "4", "6", "8", "10"].includes(c.rank)).length;
        jMult += 4 * evens;
      } else if (jname === "Odd Todd") {
        const odds = scoringCards.filter(c => ["A", "9", "7", "5", "3"].includes(c.rank)).length;
        jChips += 31 * odds;
      } else if (jname === "Fibonacci") {
        const fibs = scoringCards.filter(c => ["A", "2", "3", "5", "8"].includes(c.rank)).length;
        jMult += 8 * fibs;
      } else if (jname === "Scary Face") {
        const faces = scoringCards.filter(c => ["J", "Q", "K"].includes(c.rank)).length;
        jChips += 30 * faces;
      } else if (jname === "Smiley Face") {
        const faces = scoringCards.filter(c => ["J", "Q", "K"].includes(c.rank)).length;
        jMult += 5 * faces;
      } else if (jname === "Scholar") {
        const aces = scoringCards.filter(c => c.rank === "A").length;
        jChips += 20 * aces;
        jMult += 4 * aces;
      } else if (jname === "Walkie Talkie") {
        const tensOrFours = scoringCards.filter(c => c.rank === "10" || c.rank === "4").length;
        jChips += 10 * tensOrFours;
        jMult += 4 * tensOrFours;
      } else if (jname === "Half Joker" && selectedCards.length <= 3) {
        jMult += 20;
      } else if (jname === "Jolly Joker" && handName.includes("Pair")) {
        jMult += 8;
      } else if (jname === "Zany Joker" && handName.includes("Three of a Kind")) {
        jMult += 12;
      } else if (jname === "Mad Joker" && handName === "Two Pair") {
        jMult += 10;
      } else if (jname === "Crazy Joker" && handName.includes("Straight")) {
        jMult += 12;
      } else if (jname === "Droll Joker" && handName.includes("Flush")) {
        jMult += 10;
      } else if (jname === "Sly Joker" && handName.includes("Pair")) {
        jChips += 50;
      } else if (jname === "Wily Joker" && handName.includes("Three of a Kind")) {
        jChips += 100;
      } else if (jname === "Clever Joker" && handName === "Two Pair") {
        jChips += 80;
      } else if (jname === "Devious Joker" && handName.includes("Straight")) {
        jChips += 100;
      } else if (jname === "Crafty Joker" && handName.includes("Flush")) {
        jChips += 80;
      } else if (jname === "Banner" && stateContext && stateContext.discards_left) {
        jChips += 30 * Number(stateContext.discards_left);
      } else if (jname === "Mystic Summit" && stateContext && Number(stateContext.discards_left) === 0) {
        jMult += 15;
      } else if (jname === "Blue Joker" && stateContext && stateContext.deck_count) {
        jChips += 2 * Number(stateContext.deck_count);
      } else if (jname === "Bull" && stateContext && stateContext.money) {
        jChips += 2 * Math.max(0, Number(stateContext.money));
      } else if (jname === "Abstract Joker" && jokers) {
        jMult += 3 * jokers.length;
      }

      chips += jChips;
      mult += jMult;
      xmult *= jXmult;

      if (jChips > 0 || jMult > 0 || jXmult > 1.0) {
        activeJokerBadges.push({
          name: j.name,
          chips: jChips,
          mult: jMult,
          xmult: jXmult,
          edition: j.edition
        });
      }
    }

    const totalMult = mult * xmult;
    const totalScore = Math.floor(chips * totalMult);

    return {
      handName: handName,
      chips: chips,
      mult: Math.round(totalMult * 10) / 10,
      totalScore: totalScore,
      cards: selectedCards,
      jokerBadges: activeJokerBadges
    };
  }

  // Find optimal plays from hand
  function findBestPlays(state) {
    const hand = state.hand || [];
    if (hand.length === 0) return [];

    const constraints = detectConstraints(state.jokers, state.blind);
    const candidates = [];
    const n = hand.length;

    function combine(start, chosen) {
      if (chosen.length > 0 && chosen.length <= 5) {
        const evalRes = evaluateHandScore(chosen, state.jokers, state.blind, state);
        if (evalRes) {
          const scoring = getScoringCards(chosen, evalRes.handName, state.jokers);
          const junkCount = chosen.length - scoring.length;

          // The Psychic: Must play 5 cards. Less than 5 cards scores 0!
          if (constraints.requires5Cards && chosen.length < 5) {
            evalRes.totalScore = 0;
            evalRes.violation = "The Psychic 必须打满 5 张牌 (当前少于 5 张判定为 0 分)";
          }

          // Ride the Bus: Strict check on scoring face cards
          if (constraints.noFaceCards) {
            const hasScoringFace = scoring.some(c => ["J", "Q", "K"].includes(c.rank));
            if (hasScoringFace) {
              evalRes.totalScore = Math.floor(evalRes.totalScore * 0.01);
              evalRes.violation = "Ride the Bus 破戒 (计分牌含人头牌)";
            }
          }

          // Filtering Play metadata
          if (junkCount > 0) {
            evalRes.isFilteringPlay = true;
            evalRes.junkCards = chosen.filter(c => !scoring.includes(c));

            // Hard veto in candidate generation: NEVER attach good cards as unscored junk!
            const activeJokerNames = new Set((state.jokers || []).filter(j => !j.debuff).map(j => j.name));
            const hasEvenJoker = activeJokerNames.has("Even Steven");
            const hasOddJoker = activeJokerNames.has("Odd Todd");
            const hasFibJoker = activeJokerNames.has("Fibonacci");
            const hasHackJoker = activeJokerNames.has("Hack");
            const hasWalkie = activeJokerNames.has("Walkie Talkie");
            const hasScholar = activeJokerNames.has("Scholar");
            const hasFaceJoker = ["Scary Face", "Smiley Face", "Photograph", "Sock and Buskin", "Triboulet", "Business Card", "Pareidolia"].some(n => activeJokerNames.has(n));
            const hasBaron = activeJokerNames.has("Baron");
            const hasShootMoon = activeJokerNames.has("Shoot the Moon");
            const remHand = hand.filter(c => !chosen.includes(c));
            const remStraightRanks = new Set(remHand.filter(x => !["steel", "gold"].includes(x.enhancement)).map(x => RANK_NUM[x.rank] || 0));
            let hasBadJunk = false;

            for (const jc of evalRes.junkCards) {
              const isDebuffed = jc.debuff ||
                (constraints.debuffedSuit && jc.suit === constraints.debuffedSuit) ||
                (constraints.debuffFaceCards && ["J", "Q", "K"].includes(jc.rank));
              if (isDebuffed) {
                continue; // Debuffed cards are safe to dump
              }
              // Veto Face cards & Aces (A, K, Q, J)
              if (["A", "K", "Q", "J"].includes(jc.rank)) {
                hasBadJunk = true;
                break;
              }
              // Veto enhancements, editions, seals
              if (jc.enhancement || jc.edition || jc.seal) {
                hasBadJunk = true;
                break;
              }
              // Veto cards that trigger active jokers
              const triggersJoker = (
                (hasEvenJoker && ["2", "4", "6", "8", "10"].includes(jc.rank)) ||
                (hasOddJoker && ["A", "3", "5", "7", "9"].includes(jc.rank)) ||
                (hasFibJoker && ["A", "2", "3", "5", "8"].includes(jc.rank)) ||
                (hasHackJoker && ["2", "3", "4", "5"].includes(jc.rank)) ||
                (hasWalkie && ["10", "4"].includes(jc.rank)) ||
                (hasScholar && jc.rank === "A") ||
                (hasFaceJoker && ["J", "Q", "K"].includes(jc.rank)) ||
                ((activeJokerNames.has("Greedy Joker") || activeJokerNames.has("Rough Gem")) && jc.suit === "D") ||
                ((activeJokerNames.has("Lusty Joker") || activeJokerNames.has("Bloodstone")) && jc.suit === "H") ||
                ((activeJokerNames.has("Wrathful Joker") || activeJokerNames.has("Arrowhead") || activeJokerNames.has("Onyx Agate")) && jc.suit === "S") ||
                ((activeJokerNames.has("Gluttonous Joker") || activeJokerNames.has("Rough Gem")) && jc.suit === "C")
              );
              if (triggersJoker) {
                hasBadJunk = true;
                break;
              }
              // Veto straight connectors
              const cVal = RANK_NUM[jc.rank] || 0;
              const inStraightConnector = cVal > 0 && (
                (remStraightRanks.has(cVal - 1) && remStraightRanks.has(cVal + 1)) ||
                (remStraightRanks.size >= 3 && (
                  (remStraightRanks.has(cVal - 1) && remStraightRanks.has(cVal - 2)) ||
                  (remStraightRanks.has(cVal + 1) && remStraightRanks.has(cVal + 2))
                ))
              );
              if (inStraightConnector) {
                hasBadJunk = true;
                break;
              }
              if (hasBaron && jc.rank === "K") {
                hasBadJunk = true;
                break;
              }
              if (hasShootMoon && jc.rank === "Q") {
                hasBadJunk = true;
                break;
              }
              // Veto if this rank appears multiple times in hand (breaks pairs)
              const rankCountInHand = hand.filter(c => c.rank === jc.rank).length;
              if (rankCountInHand >= 2) {
                hasBadJunk = true;
                break;
              }
              // Veto if this suit has 3+ cards in hand (breaks flush outs)
              const suitCountInHand = hand.filter(c => c.suit === jc.suit).length;
              if (suitCountInHand >= 3) {
                hasBadJunk = true;
                break;
              }
            }

            if (hasBadJunk) {
              // Heavily penalize so pure hand or clean plays win
              evalRes.totalScore = Math.floor(evalRes.totalScore * 0.001);
              evalRes.violation = "附带废牌包含高价值人头牌/小丑协同资产/顺子连张/对子/同花组件";
            } else if (!constraints.requires5Cards && !(constraints.hasSquareJoker && chosen.length === 4)) {
              // Even with clean low junk, prefer pure hand unless zero discards and cannot clear
              if (Number(state.discards_left || 0) > 0) {
                evalRes.totalScore = Math.floor(evalRes.totalScore * Math.pow(0.15, junkCount));
              } else if (evalRes.totalScore >= Number(state.blind?.chips || 300)) {
                evalRes.totalScore = Math.floor(evalRes.totalScore * 0.95);
              }
            }
          }

          candidates.push(evalRes);
        }
      }
      if (chosen.length === 5) return;
      for (let i = start; i < n; i++) {
        combine(i + 1, [...chosen, hand[i]]);
      }
    }

    combine(0, []);
    candidates.sort((a, b) => b.totalScore - a.totalScore);

    const unique = [];
    const seen = new Set();
    for (const c of candidates) {
      const sig = c.cards.map(x => x.rank + x.suit).sort().join(",");
      if (!seen.has(sig)) {
        seen.add(sig);
        unique.push(c);
        if (unique.length >= 8) break;
      }
    }

    return unique;
  }

  // Strategic Discard Analyzer & In-hand Card Protection
  function isProtectedHeldCard(c, jokers) {
    if (!c) return false;
    if (c.enhancement === "steel") return true; // Provides x1.5 Mult held in hand
    if (c.enhancement === "gold") return true;  // Provides $3 held in hand
    if (c.seal === "blue") return true;         // Generates Planet card held in hand
    const hasBaron = (jokers || []).some(j => j.name === "Baron");
    if (hasBaron && c.rank === "K") return true; // Baron King x1.5 Mult held in hand
    const hasShootMoon = (jokers || []).some(j => j.name === "Shoot the Moon");
    if (hasShootMoon && c.rank === "Q") return true; // +13 Mult held in hand
    return false;
  }

  function findBestDiscards(state, bestPlay) {
    const hand = state.hand || [];
    const discardsLeft = Number(state.discards_left) || 0;
    if (hand.length === 0 || discardsLeft <= 0) return null;

    const blindGoal = Number(state.blind?.chips || 300);
    const scored = Number(state.blind?.scored || 0);
    const need = Math.max(0, blindGoal - scored);
    const constraints = detectConstraints(state.jokers, state.blind);

    // 0. Top Priority: Discard Boss-debuffed cards (The Window/The Club/The Goad/The Head/etc.)
    const debuffedCards = hand.filter(c =>
      c.debuff ||
      (constraints.debuffedSuit && c.suit === constraints.debuffedSuit) ||
      (constraints.debuffFaceCards && ["J", "Q", "K"].includes(c.rank))
    );
    if (debuffedCards.length > 0 && discardsLeft >= 1 && (!bestPlay || bestPlay.totalScore < need)) {
      const toDiscard = debuffedCards.slice(0, 5);
      return {
        target: `弃除被 ${constraints.bossName || 'Boss'} 禁用的无效卡牌`,
        reason: `当前持有 ${debuffedCards.length} 张被禁用的卡牌 (提供 0 筹码/0 倍率)，优先弃除换取有效手牌！`,
        cards: toDiscard,
        intent: "debuffed_discard"
      };
    }

    // Tally suits and ranks
    const suitMap = {};
    const rankMap = {};
    for (const c of hand) {
      suitMap[c.suit] = (suitMap[c.suit] || []).concat(c);
      rankMap[c.rank] = (rankMap[c.rank] || []).concat(c);
    }

    const multiCards = [];
    const singleCards = [];
    for (const [rank, cards] of Object.entries(rankMap)) {
      if (cards.length >= 2) multiCards.push(...cards);
      else singleCards.push(...cards);
    }

    // 1. Check for 4-flush (draw 1 to make flush, skip debuffed suits)
    for (const [suit, cards] of Object.entries(suitMap)) {
      if (constraints.debuffedSuit && suit === constraints.debuffedSuit) continue;
      if (cards.length >= 4 && cards.length < 5) {
        const keepSet = new Set(cards);
        const junk = hand.filter(c => !keepSet.has(c) && !isProtectedHeldCard(c, state.jokers));
        const toDiscard = junk.sort((a, b) => (RANK_VALUES[a.rank] || 0) - (RANK_VALUES[b.rank] || 0)).slice(0, 5);
        if (toDiscard.length > 0) {
          const suitName = { "S": "黑桃", "H": "红桃", "D": "方片", "C": "梅花" }[suit] || suit;
          return {
            target: `等待抽${suitName}同花 (4缺1)`,
            reason: `手牌已有 4 张${suitName}，弃掉 ${toDiscard.length} 张杂牌，摸中第 5 张同花概率高 (约 35%~50%)`,
            cards: toDiscard,
            intent: "flush_draw"
          };
        }
      }
    }

    // 2. Check for open-ended 4-straight (span === 3, 8 outs). Gutshot (span === 4, only 4 outs) is negative EV unless 0 pairs and >= 3 discards
    const uniqueRankNums = [...new Set(hand.map(c => RANK_NUM[c.rank] || 0))].sort((a, b) => a - b);
    if (uniqueRankNums.length >= 4) {
      for (let i = 0; i <= uniqueRankNums.length - 4; i++) {
        const sub = uniqueRankNums.slice(i, i + 4);
        const span = sub[3] - sub[0];
        const isOpenEnded = (span === 3);
        const isGutshot = (span === 4 && discardsLeft >= 3 && multiCards.length === 0);
        if (isOpenEnded || isGutshot) {
          const straightRanks = new Set(sub);
          const keepCards = [];
          const seenRanks = new Set();
          for (const c of hand) {
            const rnum = RANK_NUM[c.rank];
            if (straightRanks.has(rnum) && !seenRanks.has(rnum)) {
              seenRanks.add(rnum);
              keepCards.push(c);
            }
          }
          const junk = hand.filter(c => !keepCards.includes(c) && !isProtectedHeldCard(c, state.jokers));
          const toDiscard = junk.slice(0, 5);
          if (toDiscard.length > 0) {
            return {
              target: isOpenEnded ? "两头顺子听牌 (4缺1)" : "顺子卡张听牌 (4缺1)",
              reason: `保留连点 (${sub.join("-")})，弃掉 ${toDiscard.length} 张散牌博顺子成型`,
              cards: toDiscard,
              intent: isOpenEnded ? "straight_draw" : "gutshot_draw"
            };
          }
        }
      }
    }

    // 3. Keep pairs/trips, discard uncoordinated low singletons (strictly protecting held cards)
    if (multiCards.length > 0 && singleCards.length > 0) {
      const safeSingletons = singleCards.filter(c => !isProtectedHeldCard(c, state.jokers));
      if (safeSingletons.length > 0) {
        const toDiscard = safeSingletons.sort((a, b) => (RANK_VALUES[a.rank] || 0) - (RANK_VALUES[b.rank] || 0)).slice(0, Math.min(5, safeSingletons.length));
        return {
          target: "对子/三条升级 (博葫芦/四条)",
          reason: `保留成对的核心高分牌与高价值留手牌，弃掉 ${toDiscard.length} 张低点散牌寻找同点配对`,
          cards: toDiscard,
          intent: "pair_upgrade"
        };
      }
    }

    // 4. Default: Discard lowest 3 uncoordinated cards (filtering out protected cards)
    const discardable = hand.filter(c => !isProtectedHeldCard(c, state.jokers));
    const pool = discardable.length > 0 ? discardable : hand;
    const sortedHand = [...pool].sort((a, b) => (RANK_VALUES[a.rank] || 0) - (RANK_VALUES[b.rank] || 0));
    const toDiscard = sortedHand.slice(0, Math.min(4, sortedHand.length));
    return {
      target: "清理低点底牌",
      reason: `弃除点数最小的 ${toDiscard.length} 张杂牌，刷新手牌深度，等待下一轮高阶牌型`,
      cards: toDiscard,
      intent: "ditch_junk"
    };
  }

  const POKER_HAND_TIERS = {
    "High Card": 1,
    "Pair": 2,
    "Two Pair": 3,
    "Three of a Kind": 4,
    "Straight": 5,
    "Flush": 6,
    "Full House": 7,
    "Four of a Kind": 8,
    "Straight Flush": 9,
    "Five of a Kind": 10,
    "Flush House": 11,
    "Flush Five": 12
  };

  // Hand Filtering Optimizer: Piggyback unwanted cards onto a sub-5-card play
  function optimizePlayWithJunkFilter(basePlay, state) {
    if (!basePlay || !basePlay.cards || basePlay.cards.length === 0) return basePlay;
    if (basePlay.cards.length >= 5) return basePlay;

    const constraints = detectConstraints(state.jokers, state.blind);

    // 1. The Tooth charges $1 per card played! Under The Tooth, strictly minimize cards played.
    if (constraints.costsMoneyPerCard) {
      return basePlay;
    }

    // 2. Square Joker triggers +4 permanent chips ONLY on EXACTLY 4 cards played.
    if (constraints.hasSquareJoker && basePlay.cards.length === 4) {
      return basePlay;
    }

    // 3. Half Joker gives +20 Mult ONLY if hand size <= 3 cards!
    const activeJokers = (state.jokers || []).filter(j => !j.debuff);
    const hasHalfJoker = activeJokers.some(j => j.name === "Half Joker");
    const isPsychic = !!constraints.requires5Cards;

    if (hasHalfJoker && !isPsychic) {
      // If base play is already 3 cards (e.g. Three of a Kind), never add junk cards!
      if (basePlay.cards.length >= 3) {
        return basePlay;
      }
    }

    const blindGoal = Number(state.blind?.chips || 300);
    const scored = Number(state.blind?.scored || 0);
    const need = Math.max(0, blindGoal - scored);
    const discardsLeft = Number(state.discards_left) || 0;
    const handsLeft = Number(state.hands_left) || 1;

    // Strict Gating for Filtering Plays:
    // Only consider filtering plays under 3 explicit conditions:
    // 1) The Psychic: MUST play 5 cards, otherwise scores 0!
    // 2) Square Joker: target exactly 4 cards if currently < 4 to gain permanent +4 chips
    // 3) Zero discards left AND hand does not beat blind: cycle truly useless isolated junk
    // In all other cases (normal plays with discards left or already winning): keep hand pure!
    const isSquareJoker = !!constraints.hasSquareJoker && basePlay.cards.length < 4;
    const isZeroDiscards = (discardsLeft === 0 && basePlay.totalScore < need);

    if (!isPsychic && !isSquareJoker && !isZeroDiscards) {
      return basePlay;
    }

    let targetCount = isPsychic ? 5 : (isSquareJoker ? 4 : 5);
    if (hasHalfJoker && !isPsychic) {
      targetCount = Math.min(targetCount, 3);
    }

    // If play already beats the blind AND not The Psychic AND not Square Joker:
    // Never dump extra cards.
    if (basePlay.totalScore >= need && !isPsychic && !isSquareJoker) {
      return basePlay;
    }

    // Find candidate junk cards in hand not currently in the play
    const currentCardSet = new Set(basePlay.cards);
    const available = (state.hand || []).filter(c => !currentCardSet.has(c));
    if (available.length === 0) return basePlay;

    // Tally ranks in the remaining hand to avoid breaking other pairs
    const remRankCounts = {};
    for (const c of available) {
      remRankCounts[c.rank] = (remRankCounts[c.rank] || 0) + 1;
    }

    const activeJokerNames = new Set((state.jokers || []).filter(j => !j.debuff).map(j => j.name));
    const hasEvenJoker = activeJokerNames.has("Even Steven");
    const hasOddJoker = activeJokerNames.has("Odd Todd");
    const hasFibJoker = activeJokerNames.has("Fibonacci");
    const hasHackJoker = activeJokerNames.has("Hack");
    const hasWalkie = activeJokerNames.has("Walkie Talkie");
    const hasScholar = activeJokerNames.has("Scholar");
    const hasFaceJoker = ["Scary Face", "Smiley Face", "Photograph", "Sock and Buskin", "Triboulet", "Business Card", "Pareidolia"].some(n => activeJokerNames.has(n));
    const hasBaron = activeJokerNames.has("Baron");
    const hasShootMoon = activeJokerNames.has("Shoot the Moon");
    const handRanksSet = new Set((state.hand || []).map(x => RANK_NUM[x.rank] || 0));

    // Score candidates by dump desirability
    function getDumpDesirability(c) {
      // 1. Hard veto: Enhancements (Glass, Steel, Gold, Lucky, Mult, Bonus, Stone)
      if (c.enhancement) {
        return -9999;
      }
      // 2. Hard veto: Editions (Foil, Holo, Polychrome)
      if (c.edition) {
        return -9999;
      }
      // 3. Hard veto: Seals (Red, Purple, Blue, Gold)
      if (c.seal) {
        return -9999;
      }
      // 4. Ride the Bus veto: face cards reset Ride the Bus to 0!
      if (constraints.noFaceCards && ["J", "Q", "K"].includes(c.rank)) {
        return -9999;
      }

      // 5. Debuffed cards: HIGHEST priority to dump because they are disabled under this boss!
      const isDebuffed = c.debuff ||
        (constraints.debuffedSuit && c.suit === constraints.debuffedSuit) ||
        (constraints.debuffFaceCards && ["J", "Q", "K"].includes(c.rank));
      if (isDebuffed) {
        return 1000;
      }

      // 6. Hard veto: High ranks (A, K, Q, J) unless debuffed!
      // High cards have large base chips (10-11) and build powerful hands (Straights, Flushes, Pairs).
      // They should NEVER be thrown away as unscored junk!
      if (["A", "K", "Q", "J"].includes(c.rank)) {
        return isPsychic ? -500 : -9999;
      }

      // 7. Hard veto: Cards that trigger active scoring Jokers
      const triggersJoker = (
        (hasEvenJoker && ["2", "4", "6", "8", "10"].includes(c.rank)) ||
        (hasOddJoker && ["A", "3", "5", "7", "9"].includes(c.rank)) ||
        (hasFibJoker && ["A", "2", "3", "5", "8"].includes(c.rank)) ||
        (hasHackJoker && ["2", "3", "4", "5"].includes(c.rank)) ||
        (hasWalkie && ["10", "4"].includes(c.rank)) ||
        (hasScholar && c.rank === "A") ||
        (hasFaceJoker && ["J", "Q", "K"].includes(c.rank)) ||
        ((activeJokerNames.has("Greedy Joker") || activeJokerNames.has("Rough Gem")) && c.suit === "D") ||
        ((activeJokerNames.has("Lusty Joker") || activeJokerNames.has("Bloodstone")) && c.suit === "H") ||
        ((activeJokerNames.has("Wrathful Joker") || activeJokerNames.has("Arrowhead") || activeJokerNames.has("Onyx Agate")) && c.suit === "S") ||
        ((activeJokerNames.has("Gluttonous Joker") || activeJokerNames.has("Rough Gem")) && c.suit === "C")
      );
      if (triggersJoker) {
        return isPsychic ? -600 : -9999;
      }

      // 8. Hard veto: Straight connectors
      const remAvailable = available.filter(x => x !== c);
      const remStraightRanks = new Set(remAvailable.filter(x => !["steel", "gold"].includes(x.enhancement)).map(x => RANK_NUM[x.rank] || 0));
      const cVal = RANK_NUM[c.rank] || 0;
      const inStraightConnector = cVal > 0 && (
        (remStraightRanks.has(cVal - 1) && remStraightRanks.has(cVal + 1)) ||
        (remStraightRanks.size >= 3 && (
          (remStraightRanks.has(cVal - 1) && remStraightRanks.has(cVal - 2)) ||
          (remStraightRanks.has(cVal + 1) && remStraightRanks.has(cVal + 2))
        ))
      );
      if (inStraightConnector) {
        return isPsychic ? -400 : -9999;
      }

      if (hasBaron && c.rank === "K") return -9999;
      if (hasShootMoon && c.rank === "Q") return -9999;

      // 9. Protect Pairs/Trips in remaining hand!
      if (remRankCounts[c.rank] >= 2) {
        return isPsychic ? -300 : -9999;
      }

      // 10. Protect Flush outs: if total hand has 3+ cards of this suit, preserve them!
      const fullHandSuitCount = (state.hand || []).filter(x => x.suit === c.suit).length;
      if (fullHandSuitCount >= 3) {
        return isPsychic ? -200 : -9999;
      }

      // 11. Score isolated low-rank cards (2, 3, 4, 5, 6, 7, 8):
      const rankVal = RANK_VALUES[c.rank] || 5;
      if (rankVal <= 6) {
        // Pure low-rank isolated white cards (2, 3, 4, 5, 6)
        return (14 - rankVal) * 10; // 2 -> 120, 3 -> 110, 4 -> 100, 5 -> 90, 6 -> 80
      } else if (rankVal <= 8) {
        return (14 - rankVal) * 5;  // 7 -> 35, 8 -> 30
      } else {
        // Rank 9, 10
        return isPsychic ? (14 - rankVal) : -9999;
      }
    }

    const sortedJunk = [...available]
      .map(c => ({ card: c, score: getDumpDesirability(c) }))
      .filter(item => isPsychic ? item.score > -1000 : item.score > 0)
      .sort((a, b) => b.score - a.score)
      .map(item => item.card);

    let augmentedCards = [...basePlay.cards];
    const addedJunk = [];

    for (const jc of sortedJunk) {
      if (augmentedCards.length >= targetCount) break;

      const testSelection = [...augmentedCards, jc];
      const testEval = evaluateHandScore(testSelection, state.jokers, state.blind, state);
      if (!testEval) continue;

      // Ensure testEval doesn't degrade score or violate fatal constraints
      if (testEval.violation) continue;
      if (testEval.totalScore < basePlay.totalScore * 0.99) continue;

      // Verify that hand ranking didn't degrade
      const oldRankVal = POKER_HAND_TIERS[basePlay.handName] || 0;
      const newRankVal = POKER_HAND_TIERS[testEval.handName] || 0;
      if (newRankVal < oldRankVal) continue;

      // Accept this junk card!
      augmentedCards.push(jc);
      addedJunk.push(jc);
    }

    if (addedJunk.length > 0) {
      const finalEval = evaluateHandScore(augmentedCards, state.jokers, state.blind, state);
      return {
        ...basePlay,
        cards: augmentedCards,
        handName: finalEval ? finalEval.handName : basePlay.handName,
        chips: finalEval ? finalEval.chips : basePlay.chips,
        mult: finalEval ? finalEval.mult : basePlay.mult,
        totalScore: finalEval ? finalEval.totalScore : basePlay.totalScore,
        isFilteringPlay: true,
        junkCards: addedJunk
      };
    }

    return basePlay;
  }

  // Stage Decision Arbitration (Play vs Discard)
  function arbitrateAction(state, rawBestPlay, bestDiscard) {
    if (!rawBestPlay) return null;

    const constraints = detectConstraints(state.jokers, state.blind);
    const bestPlay = optimizePlayWithJunkFilter(rawBestPlay, state);

    const blind = state.blind || {};
    const chipsTarget = Number(blind.chips) || 300;
    const chipsScored = Number(blind.scored) || 0;
    const need = Math.max(0, chipsTarget - chipsScored);
    const handsLeft = (state.hands_left !== undefined && state.hands_left !== null) ? Number(state.hands_left) : 1;
    const discardsLeft = Number(state.discards_left) || 0;

    if (handsLeft <= 0) {
      return null;
    }

    const filteringNote = (bestPlay.junkCards && bestPlay.junkCards.length > 0)
      ? ` [附带 ${bestPlay.junkCards.length} 张废牌 (${bestPlay.junkCards.map(c => (c.rank || '') + (c.suit || '')).join(' ')}) 进行伪弃牌过滤]`
      : "";

    // 1. Force Play:
    // - Hand score directly beats remaining blind requirement
    if (bestPlay.totalScore >= need) {
      const isPsychicSave = constraints.requires5Cards && bestPlay.junkCards && bestPlay.junkCards.length > 0;
      return {
        verdict: "PLAY",
        confidence: "CERTAIN",
        badgeClass: "arb-play",
        title: isPsychicSave ? "⚡ 破咒绝杀 (The Psychic 5张成行)" : (filteringNote ? "⚡ 绝杀出牌 (附带杂牌清洗)" : "⚡ 推荐立即出牌 (直接通关)"),
        reason: isPsychicSave
          ? `当前处于 The Psychic (需5张)，已智能附带 ${bestPlay.junkCards.length} 张废牌补齐 5 张，打出 ${bestPlay.totalScore.toLocaleString()} 分直接通关！`
          : `本手预期得分 ${bestPlay.totalScore.toLocaleString()} 分直接超越通关差额 (${need.toLocaleString()} 分)，可直接通关${filteringNote}！`,
        cards: bestPlay.cards
      };
    }

    // - No discards left
    if (discardsLeft <= 0) {
      return {
        verdict: "PLAY",
        confidence: "FORCED",
        badgeClass: "arb-play",
        title: filteringNote ? "⚡ 伪弃牌出牌 (弃牌耗尽·手牌清洗)" : "⚡ 必须出牌 (弃牌耗尽)",
        reason: `剩余弃牌次数为 0，必须出牌积累筹码 (本手预计打出 +${bestPlay.totalScore.toLocaleString()} 分)${filteringNote}`,
        cards: bestPlay.cards
      };
    }

    // - Passive Constraints: Green Joker / The Water (strictly avoid discarding)
    if (constraints && constraints.avoidDiscard) {
      const isGreen = (state.jokers || []).some(j => j.name === "Green Joker" && !j.debuff);
      return {
        verdict: "PLAY",
        confidence: "HIGH",
        badgeClass: "arb-play",
        title: filteringNote ? "⚡ 规避弃牌出牌 (被动保护·手牌清洗)" : "⚡ 规避弃牌出牌 (被动保护)",
        reason: isGreen
          ? `当前持有 Green Joker，弃牌会永久损失 -1 倍率，优先出牌过牌${filteringNote}！`
          : `当前处于 The Water 盲注限制，严禁弃牌，直接出牌${filteringNote}！`,
        cards: bestPlay.cards
      };
    }

    // - Last hand left
    if (handsLeft === 1) {
      return {
        verdict: "PLAY",
        confidence: "CRITICAL",
        badgeClass: "arb-play",
        title: "⚡ 决胜出牌 (最后出牌)",
        reason: `仅剩最后 1 次出牌机会，不可再冒险弃牌，打出最高分组合${filteringNote}！`,
        cards: bestPlay.cards
      };
    }

    // - Steady accumulation check: If continuing to play best hand safely clears the blind across remaining hands, play it!
    if (bestPlay.totalScore * handsLeft >= need && bestPlay.totalScore >= need * 0.35) {
      return {
        verdict: "PLAY",
        confidence: "STEADY",
        badgeClass: "arb-play",
        title: filteringNote ? "⚡ 稳健出牌 (附带杂牌清洗)" : "⚡ 稳健出牌 (手牌足够通关)",
        reason: `当前手牌预期得分 ${bestPlay.totalScore.toLocaleString()} 分，结合剩余 ${handsLeft} 次出牌累计可得 ${(bestPlay.totalScore * handsLeft).toLocaleString()} 分 (需 ${need.toLocaleString()} 分)${filteringNote}，无需冒险弃牌！`,
        cards: bestPlay.cards
      };
    }

    // 2. High-expected Strategic Discard:
    if (bestDiscard && bestDiscard.cards && bestDiscard.cards.length > 0) {
      if (bestDiscard.intent === "debuffed_discard") {
        return {
          verdict: "DISCARD",
          confidence: "STRATEGIC",
          badgeClass: "arb-discard",
          title: "🗑️ 战略弃牌 (净化无效牌)",
          reason: bestDiscard.reason,
          cards: bestDiscard.cards
        };
      }
      if (bestDiscard.intent === "flush_draw" && (bestPlay.totalScore < need * 0.7 || discardsLeft >= 3)) {
        return {
          verdict: "DISCARD",
          confidence: "STRATEGIC",
          badgeClass: "arb-discard",
          title: "🗑️ 战略弃牌 (同花听牌)",
          reason: `手牌已持有 4 张同花，弃除非同花杂牌博取大同花翻盘是胜率最高解！`,
          cards: bestDiscard.cards
        };
      }
      if (bestDiscard.intent === "straight_draw" && discardsLeft >= 2 && bestPlay.totalScore < need * 0.45) {
        return {
          verdict: "DISCARD",
          confidence: "STRATEGIC",
          badgeClass: "arb-discard",
          title: "🗑️ 战略弃牌 (顺子听牌)",
          reason: `手牌具有 4 连张两头顺子胚子，建议战略弃牌寻找成型顺子！`,
          cards: bestDiscard.cards
        };
      }
      if (bestDiscard.intent === "pair_upgrade" && bestPlay.totalScore < need * 0.45 && discardsLeft >= 2) {
        return {
          verdict: "DISCARD",
          confidence: "HIGH",
          badgeClass: "arb-discard",
          title: "🗑️ 战略弃牌 (升级成型)",
          reason: `现有对子/三条单打仅得 ${bestPlay.totalScore} 分 (不足目标 45%)，建议弃杂牌挖葫芦或四条`,
          cards: bestDiscard.cards
        };
      }

      // If repeating current play over all remaining hands still cannot meet the blind (mathematically doomed without drawing better)
      if (bestPlay.totalScore * handsLeft < need) {
        return {
          verdict: "DISCARD",
          confidence: "CRITICAL",
          badgeClass: "arb-discard",
          title: "🗑️ 断腕弃牌 (绝境刷新)",
          reason: `当前牌型重复打出所有手牌也仅得 ${(bestPlay.totalScore * handsLeft).toLocaleString()} 分，无法达标 (${need.toLocaleString()})，必须弃除杂牌博取爆发牌型！`,
          cards: bestDiscard.cards
        };
      }
    }

    // 3. Steady Play (Accumulation):
    return {
      verdict: "PLAY",
      confidence: "STEADY",
      badgeClass: "arb-play",
      title: "⚡ 稳健出牌 (分步推进)",
      reason: `当前手牌可稳定获取 ${bestPlay.totalScore.toLocaleString()} 分，结合剩余 ${handsLeft} 次出牌可稳妥过关`,
      cards: bestPlay.cards
    };
  }

  const VOUCHER_BASE_SCORES = {
    "Antimatter": 95,
    "Blank": 70,
    "Grabber": 88,
    "Nacho Tong": 90,
    "Wasteful": 82,
    "Recycler": 85,
    "Recyclomancy": 85,
    "Seed Money": 86,
    "Money Tree": 88,
    "Director's Cut": 84,
    "Retcon": 86,
    "Overstock": 80,
    "Overstock Plus": 82,
    "Clearance Sale": 78,
    "Liquidation": 80,
    "Tarot Merchant": 76,
    "Tarot Tycoon": 80,
    "Planet Merchant": 76,
    "Planet Tycoon": 80,
    "Hone": 72,
    "Glow Up": 78,
    "Telescope": 86,
    "Observatory": 92,
    "Paint Brush": 82,
    "Palette": 88,
  };

  function scoreOwnedJoker(joker, synergy) {
    const jname = joker.name || "";
    let base = 50;
    if (["Cavendish", "Blueprint", "Brainstorm", "Constellation", "Hologram", "Baron"].includes(jname)) base = 90;
    else if (["Card Sharp", "Blackboard", "Baseball Card", "Stuntman", "Bull", "Bootstraps"].includes(jname)) base = 80;
    else if (["Green Joker", "Ride the Bus", "Red Card", "Square Joker", "Supernova", "Golden Joker", "Mail-In Rebate", "Trading Card"].includes(jname)) base = 70;
    else if (["Half Joker", "Gros Michel", "Blue Joker", "Banner", "Scary Face", "Odd Todd"].includes(jname)) base = 55;
    else base = 40;

    if (jname === "Ice Cream" && (joker.chips || 0) <= 20) base = 25;
    else if (jname === "Popcorn" && (joker.mult || 0) <= 4) base = 25;
    else if (jname === "Gros Michel") base = 45;

    return base;
  }

  // Tag Evaluation Catalog and Blind Choice System
  const TAG_EVAL_CATALOG = {
    "tag_negative": { name: "Negative Tag", ev: 9.5, tier: "S", desc: "下一个商店小丑额外提供 +1 槽位 (负片)" },
    "tag_polychrome": { name: "Polychrome Tag", ev: 8.2, tier: "S", desc: "下一个商店版本小丑免费且多彩 (x1.5 Mult)" },
    "tag_investment": { name: "Investment Tag", ev: 8.0, tier: "S", desc: "击败下一个 Boss 获得 $25 奖金" },
    "tag_ethereal": { name: "Ethereal Tag", ev: 7.5, tier: "A", desc: "免费获得 1 个幻灵牌包" },
    "tag_economy": { name: "Economy Tag", ev: 7.2, tier: "A", desc: "当前资金直接翻倍 (上限 +$40)" },
    "tag_rare": { name: "Rare Tag", ev: 7.0, tier: "A", desc: "下一个商店免费获得 1 张 Rare 稀有小丑" },
    "tag_coupon": { name: "Coupon Tag", ev: 6.8, tier: "A", desc: "下一个商店卡牌与补充包全免费" },
    "tag_holo": { name: "Holographic Tag", ev: 6.8, tier: "A", desc: "下一个商店版本小丑免费且全息 (+10 Mult)" },
    "tag_buffoon": { name: "Buffoon Tag", ev: 6.5, tier: "B", desc: "免费获得 1 个大型小丑包" },
    "tag_voucher": { name: "Voucher Tag", ev: 6.5, tier: "B", desc: "下一个商店额外增加 1 张优惠券" },
    "tag_foil": { name: "Foil Tag", ev: 6.2, tier: "B", desc: "下一个商店版本小丑免费且闪箔 (+50 Chips)" },
    "tag_charm": { name: "Charm Tag", ev: 6.2, tier: "B", desc: "免费获得 1 个大型奥秘包" },
    "tag_meteor": { name: "Meteor Tag", ev: 6.0, tier: "B", desc: "免费获得 1 个大型天体包" },
    "tag_uncommon": { name: "Uncommon Tag", ev: 5.5, tier: "B", desc: "下一个商店免费获得 1 张 Uncommon 罕见小丑" },
    "tag_double": { name: "Double Tag", ev: 5.5, tier: "B", desc: "复制下一个获取的 Tag 效果" },
    "tag_orbital": { name: "Orbital Tag", ev: 5.2, tier: "B", desc: "指定手牌等级提升 3 级" },
    "tag_standard": { name: "Standard Tag", ev: 5.0, tier: "C", desc: "免费获得 1 个大型标准包" },
    "tag_d_six": { name: "D6 Tag", ev: 5.0, tier: "C", desc: "下一个商店初始刷新费用为 $0" },
    "tag_boss": { name: "Boss Tag", ev: 4.5, tier: "C", desc: "重置下一个 Boss Blind" },
    "tag_speed": { name: "Speed Tag", ev: 3.5, tier: "D", desc: "每跳过一次盲注获得 $5" },
    "tag_handy": { name: "Handy Tag", ev: 3.5, tier: "D", desc: "每打出过一次手牌获得 $1" },
    "tag_garbage": { name: "Garbage Tag", ev: 3.5, tier: "D", desc: "每次未使用的弃牌获得 $1" },
    "tag_top_up": { name: "Top-up Tag", ev: 3.2, tier: "D", desc: "随机生成最多 2 张基础小丑" },
    "tag_juggle": { name: "Juggle Tag", ev: 3.0, tier: "D", desc: "下回合手牌上限 +3" },
  };

  function evaluateBlindChoice(state) {
    const b = state.blind || {};
    const bname = b.name || "Small Blind";
    const btype = (b.type || "small").toLowerCase();
    const chipsReq = b.chips || 300;
    const ante = state.ante || 1;
    const money = state.money || 0;
    const jokers = state.jokers || [];
    const jCount = jokers.length;

    if (btype === "boss" || bname.toLowerCase().includes("boss")) {
      return {
        action: "select_blind",
        canSkip: false,
        selectEv: 8.0,
        skipEv: 0.0,
        tagKey: null,
        tagName: null,
        reason: "Boss 盲注无法跳过，必须进入出牌阶段斩杀"
      };
    }

    let selectEv = 6.4;
    if (btype === "big") selectEv += 0.3;
    const interest = Math.min(5, Math.floor(money / 5));
    selectEv += interest * 0.12;
    if (ante === 1) selectEv += 0.6;
    if (jCount < 2) selectEv += 0.8;
    else if (jCount >= 4) selectEv -= 0.2;
    selectEv = Math.min(8.5, Math.max(5.0, selectEv));

    const tagData = state.blind_tag || {};
    let rawKey = tagData.key || tagData.name || "";
    let normKey = String(rawKey).toLowerCase().replace(/\s+/g, "_");
    if (!normKey.startsWith("tag_")) normKey = "tag_" + normKey;
    const tagInfo = TAG_EVAL_CATALOG[normKey] || { name: tagData.name || "Unknown Tag", ev: 4.8, tier: "C", desc: "普通奖励 Tag" };

    let skipEv = tagInfo.ev;
    let riskWarning = null;

    if (normKey === "tag_economy") {
      if (money >= 20) skipEv += 1.5;
      else if (money <= 6) skipEv -= 3.2;
    }
    if (normKey === "tag_investment") {
      if (jCount >= 2) skipEv += 0.8;
      else if (jCount === 0) skipEv -= 1.8;
    }

    if (ante === 1 && btype === "big" && jCount === 0) {
      skipEv -= 4.5;
      riskWarning = "⚠️ 严禁在 Ante 1 零小丑时跳过大盲注，直面 Boss 极大概率暴毙！";
    } else if (ante === 1 && btype === "small" && jCount === 0) {
      if (tagInfo.tier !== "S") {
        skipEv -= 2.2;
        riskWarning = "Ante 1 首轮盲注缺少小丑基础，非 S 级神级 Tag 建议进入商店采购";
      }
    } else if (jCount === 0 && ante > 1) {
      skipEv -= 2.8;
      riskWarning = "场上暂无小丑战力，急需进入商店构建阵容";
    } else if (jCount >= 3 && ["S", "A"].includes(tagInfo.tier)) {
      skipEv += 0.5;
    }

    skipEv = Math.max(1.0, skipEv);
    const shouldSkip = skipEv > selectEv;
    return {
      action: shouldSkip ? "skip_blind" : "select_blind",
      canSkip: true,
      selectEv: Number(selectEv.toFixed(2)),
      skipEv: Number(skipEv.toFixed(2)),
      tagKey: normKey,
      tagName: tagInfo.name,
      tagTier: tagInfo.tier,
      tagDesc: tagInfo.desc,
      riskWarning: riskWarning,
      reason: shouldSkip
        ? `Tag [${tagInfo.name}] (${tagInfo.tier}级) 期望收益 (${skipEv.toFixed(1)}) 优于进入盲注 (${selectEv.toFixed(1)}): ${tagInfo.desc}`
        : (riskWarning
            ? `优先进入盲注以保全战力，避免盲目跳过: ${riskWarning}`
            : `进入盲注稳健获取奖金与商店发展机会，Tag [${tagInfo.name}] 价值不足以弥补跳过商店损失`)
    };
  }

  // Shop Evaluation System
  function findBestShopPurchases(state) {
    const shop = state.shop || {};
    const cards = shop.cards || [];
    const boosters = shop.boosters || [];
    const vouchers = shop.vouchers || [];
    const money = Number(state.money) || 0;
    const jokersCount = Number(state.jokers_count) || (state.jokers ? state.jokers.length : 0);
    const maxJokerSlots = Number(state.joker_slots) || 5;
    const consumableCount = Number(state.consumables ? state.consumables.length : 0);
    const maxConsumableSlots = Number(state.consumable_slots) || 2;
    const rerollCost = Number(shop.reroll_cost) || 5;

    const synergy = analyzeSynergy(state.jokers);

    const TOP_TIER = [
      "Cavendish", "Blueprint", "Brainstorm", "Constellation", "Hologram",
      "Card Sharp", "Blackboard", "Baron", "Baseball Card", "Stuntman",
      "Green Joker", "Ride the Bus", "Red Card", "Square Joker", "Supernova",
      "Golden Joker", "Bull", "Bootstraps", "Mail-In Rebate", "Trading Card",
      "Half Joker", "Gros Michel", "Blue Joker", "Banner", "Scary Face", "Odd Todd"
    ];

    const PLANET_NAMES = ["Mercury", "Venus", "Earth", "Mars", "Jupiter", "Saturn", "Uranus", "Neptune", "Pluto", "Planet X", "Ceres", "Eris"];
    const TAROT_NAMES = [
      "The Fool", "The Magician", "The High Priestess", "The Empress", "The Emperor",
      "The Hierophant", "The Lovers", "The Chariot", "Justice", "The Hermit",
      "The Wheel of Fortune", "Strength", "The Hanged Man", "Death", "Temperance",
      "The Devil", "The Tower", "The Star", "The Moon", "The Sun", "Judgement", "The World"
    ];

    const evaluatedCards = [];
    for (const c of cards) {
      let score = 50;
      const cname = c.name || "";
      const cost = Number(c.cost) || 0;
      const canAfford = money >= cost;

      const isPlanet = (c.set === "Planet") || (c.type === "planet") || PLANET_NAMES.includes(cname);
      const isTarot = (c.set === "Tarot") || (c.type === "tarot") || TAROT_NAMES.includes(cname);
      const isSpectral = (c.set === "Spectral") || (c.type === "spectral");
      const isConsumable = isPlanet || isTarot || isSpectral;

      const hasSlot = isConsumable ? (consumableCount < maxConsumableSlots) : (jokersCount < maxJokerSlots);
      let pillar = "utility";

      if (isConsumable) {
        if (isPlanet) {
          pillar = "planet";
          if (["Jupiter", "Earth", "Saturn", "Mercury", "Planet X", "Mars", "Uranus"].includes(cname)) {
            score = 80;
          } else {
            score = 68;
          }
        } else if (isTarot) {
          pillar = "tarot";
          if (cname === "The Hermit") {
            score = money >= 8 ? 88 : (money >= 5 ? 78 : 55);
          } else if (cname === "Temperance") {
            let totalSell = 0;
            for (const j of (state.jokers || [])) totalSell += (j.sell_cost || j.sell_value || 1);
            score = totalSell >= 4 ? 82 : 55;
          } else if (cname === "Judgement") {
            score = jokersCount < maxJokerSlots ? 90 : 40;
          } else if (cname === "The Wheel of Fortune") {
            const hasEditionless = (state.jokers || []).some(j => !j.edition);
            score = hasEditionless ? 72 : 35;
          } else if (cname === "The Emperor" || cname === "The High Priestess") {
            score = consumableCount < maxConsumableSlots ? 76 : 45;
          } else if (cname === "Death" || cname === "The Hanged Man") {
            score = 78;
          } else {
            score = 65;
          }
        } else if (isSpectral) {
          pillar = "spectral";
          score = (cname === "Black Hole" || cname === "Immolate" || cname === "Cryptid") ? 92 : 75;
        }
      } else {
        // Standard Joker evaluation
        pillar = JOKER_PILLAR_MAP[cname] || (c.xmult && c.xmult > 1 ? "x_mult" : (c.mult >= 8 ? "flat_mult" : (c.chips >= 30 ? "chips" : "utility")));

        if (TOP_TIER.includes(cname)) score += 35;
        if (pillar === synergy.primaryNeed) score += 30; // 契合四角大厦当前短板加分
        if (c.xmult && c.xmult > 1.0) score += 40;
        if (c.mult && c.mult >= 8) score += 20;
        if (c.chips && c.chips >= 50) score += 20;
        if (c.edition === "poly") score += 30;
        else if (c.edition === "holo") score += 20;
        else if (c.edition === "foil") score += 15;

        // Early game Ante 1-2 survival boost for basic scoring jokers
        if ((state.ante || 1) <= 2 && jokersCount < 2) {
          if (c.mult || c.chips || ["Joker", "Half Joker", "Misprint", "Popcorn", "Ice Cream", "Blue Joker", "Gros Michel", "Abstract Joker"].includes(cname)) {
            score += 35; // 早期生存小丑极大加成
          }
        }

        // Boss Blind 针对性规避与特化加成
        const upcomingBoss = (state.blind && state.blind.upcoming_boss) || null;
        if (upcomingBoss) {
          if (upcomingBoss === "The Plant") {
            const faceJokers = ["Scary Face", "Smiley Face", "Photograph", "Baron", "Sock and Buskin", "Pareidolia", "Business Card"];
            if (faceJokers.includes(cname)) score -= 50;
          } else if (upcomingBoss === "The Head" && ["Lusty Joker", "Bloodstone"].includes(cname)) {
            score -= 45;
          } else if (upcomingBoss === "The Window" && ["Greedy Joker", "Rough Gem"].includes(cname)) {
            score -= 45;
          } else if (upcomingBoss === "The Goad" && ["Wrathful Joker", "Arrowhead"].includes(cname)) {
            score -= 45;
          } else if (upcomingBoss === "The Club" && ["Gluttonous Joker", "Onyx Agate"].includes(cname)) {
            score -= 45;
          } else if (upcomingBoss === "The Needle") {
            if (["Burglar", "Grabber", "Nacho Tong"].includes(cname)) score += 35;
            if (["Ice Cream", "Bull", "Cavendish"].includes(cname)) score += 25;
          } else if (upcomingBoss === "The Water") {
            if (["Mail-In Rebate", "Castle"].includes(cname)) score -= 35;
          }
        }

        // 利息线保护 ($25 黄金利息法则) - 仅当非生死紧急时惩罚
        if (canAfford && ((state.ante || 1) > 1 || jokersCount >= 2)) {
          const remaining = money - cost;
          const curInterest = Math.min(5, Math.floor(money / 5));
          const newInterest = Math.min(5, Math.floor(remaining / 5));
          const interestLoss = curInterest - newInterest;
          if (interestLoss > 0 && !TOP_TIER.includes(cname) && pillar !== synergy.primaryNeed) {
            score -= (interestLoss * 20);
          }
        }
      }

      if (!hasSlot) score -= 35;
      if (!canAfford) score -= 50;

      evaluatedCards.push({
        ...c,
        isConsumable: isConsumable,
        pillar: pillar,
        score: score,
        canAfford: canAfford,
        hasSlot: hasSlot,
        recommendation: score >= 75 ? "强烈推荐" : (score >= 60 ? "值得考虑" : "暂不推荐")
      });
    }

    evaluatedCards.sort((a, b) => b.score - a.score);
    const bestCard = evaluatedCards[0];

    // Evaluate shop boosters
    const evaluatedBoosters = [];
    for (const b of boosters) {
      const bname = b.name || "";
      const cost = Number(b.cost) || 0;
      const canAfford = money >= cost;
      let bScore = 50;

      if (bname.includes("Buffoon")) {
        // Buffoon pack: provides jokers! Critical for early survival
        bScore = (jokersCount === 0 ? 100 : (jokersCount === 1 ? 88 : (jokersCount < maxJokerSlots ? 72 : 35)));
      } else if (bname.includes("Celestial")) {
        bScore = 75;
      } else if (bname.includes("Arcana")) {
        bScore = 72;
      } else if (bname.includes("Spectral")) {
        bScore = 78;
      } else if (bname.includes("Standard")) {
        bScore = 48;
      }

      if (bname.includes("Mega") || bname.includes("Jumbo")) {
        bScore += 12;
      }

      // Safe threshold for booster purchase:
      // Early game survival: Buffoon pack has threshold = 0 if < 2 jokers.
      let safeThreshold = 0;
      if (bname.includes("Buffoon") && jokersCount < 2) {
        safeThreshold = 0;
      } else if ((state.ante || 1) <= 2) {
        safeThreshold = 4;
      } else {
        safeThreshold = 20;
      }
      const budgetAllowed = canAfford && (money - cost >= safeThreshold);

      evaluatedBoosters.push({
        ...b,
        score: bScore,
        canAfford: canAfford,
        budgetAllowed: budgetAllowed,
        recommendation: budgetAllowed && bScore >= 80 ? "强烈推荐" : (budgetAllowed ? "值得购买" : "资金暂缓")
      });
    }
    evaluatedBoosters.sort((a, b) => b.score - a.score);
    const bestBooster = evaluatedBoosters.find(b => b.budgetAllowed) || null;

    // Evaluate shop vouchers
    const evaluatedVouchers = [];
    for (const v of vouchers) {
      const vname = v.name || "Voucher";
      const cost = Number(v.cost) || 10;
      const vScore = VOUCHER_BASE_SCORES[vname] || 65;
      const canAfford = money >= cost;
      const budgetAllowed = (money - cost >= 25) || (canAfford && vScore >= 85);
      evaluatedVouchers.push({
        ...v,
        name: vname,
        cost: cost,
        score: vScore,
        canAfford: canAfford,
        budgetAllowed: budgetAllowed,
        recommendation: budgetAllowed && vScore >= 80 ? "强烈推荐" : (budgetAllowed ? "值得购买" : "资金暂缓")
      });
    }
    evaluatedVouchers.sort((a, b) => b.score - a.score);
    const bestVoucher = evaluatedVouchers.find(v => v.canAfford && v.budgetAllowed) || null;

    // Full Joker Replacement / Sell Evaluation
    let sellRecommendation = null;
    if (jokersCount >= maxJokerSlots && state.jokers && state.jokers.length > 0 && evaluatedCards.length > 0) {
      const bestCandidate = evaluatedCards.find(c => !c.isConsumable);
      if (bestCandidate && bestCandidate.score >= 80 && money >= bestCandidate.cost) {
        let weakest = null;
        let weakestScore = 999;
        state.jokers.forEach((j, idx) => {
          const sc = scoreOwnedJoker(j, synergy);
          if (sc < weakestScore) {
            weakestScore = sc;
            weakest = { index: idx + 1, name: j.name, score: sc };
          }
        });
        if (weakest && weakestScore <= 48 && (bestCandidate.score - weakestScore) >= 30) {
          sellRecommendation = {
            sellIndex: weakest.index,
            sellName: weakest.name,
            sellScore: weakestScore,
            targetCard: bestCandidate,
            scoreGain: bestCandidate.score - weakestScore,
          };
        }
      }
    }

    // Reroll safety evaluation:
    // CRITICAL: Rerolling costs $rerollCost (usually $5).
    // If money < rerollCost + 4 (e.g. $8 < $9), rerolling leaves you with <= $3,
    // which makes it impossible to buy ANY Joker or Booster on the refreshed shelf!
    const hasAffordableJoker = evaluatedCards.some(c => !c.isConsumable && c.canAfford && c.hasSlot && c.score >= 45);
    const hasAffordableBuffoon = evaluatedBoosters.some(b => (b.name || "").includes("Buffoon") && b.canAfford);
    const hasStrongShopCard = (bestCard && bestCard.canAfford && bestCard.hasSlot && bestCard.score >= 70) ||
                              (bestVoucher && bestVoucher.canAfford && bestVoucher.score >= 75) ||
                              (bestBooster && bestBooster.canAfford && bestBooster.score >= 70);

    const safeReroll = money >= 25 + rerollCost && !hasStrongShopCard;
    const emergencyReroll = (state.ante || 1) <= 2 && jokersCount === 0 &&
                            money >= (rerollCost + 4) &&
                            !hasAffordableJoker &&
                            !hasAffordableBuffoon &&
                            !hasStrongShopCard;
    const shouldReroll = safeReroll || emergencyReroll;

    let interestAdvice = "💰 当前资金充足";
    if (money >= 25) {
      interestAdvice = "当前资金已达 $25 吃满每轮 $5 顶额利息，盈余资金可自由采购或刷新";
    } else {
      const nextThreshold = (Math.floor(money / 5) + 1) * 5;
      interestAdvice = `资金 $${money}，差 $${nextThreshold - money} 达 $${nextThreshold} 利息线 (每 $5 结算 $1 利息)`;
    }

    return {
      evaluatedCards: evaluatedCards,
      bestCard: bestCard && bestCard.canAfford && bestCard.hasSlot && (bestCard.score >= 60 || (jokersCount < 2 && bestCard.score >= 45)) ? bestCard : null,
      evaluatedBoosters: evaluatedBoosters,
      bestBooster: bestBooster,
      evaluatedVouchers: evaluatedVouchers,
      bestVoucher: bestVoucher,
      sellRecommendation: sellRecommendation,
      boosters: boosters,
      interestAdvice: interestAdvice,
      canReroll: money >= rerollCost,
      shouldReroll: shouldReroll,
      rerollCost: rerollCost,
      hasSlot: jokersCount < maxJokerSlots,
      synergy: synergy
    };
  }

  const CARD_RANKS_ORDER = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"];
  function getRankVal(r) {
    const idx = CARD_RANKS_ORDER.indexOf(r);
    return idx >= 0 ? idx : 0;
  }

  function selectConsumableTargets(cname, hand, jokers) {
    if (!hand || hand.length === 0) return [];
    const indexed = hand.map((c, i) => ({ card: c, index: i + 1 }));
    const hasBaron = (jokers || []).some(j => j.name === "Baron");

    if (cname === "The Hanged Man") {
      // Destroy up to 2 worst unenhanced, non-sealed cards
      const unenhanced = indexed.filter(x => !x.card.enhancement && !x.card.seal && !x.card.edition);
      const pool = unenhanced.length > 0 ? unenhanced : indexed;
      pool.sort((a, b) => getRankVal(a.card.rank) - getRankVal(b.card.rank));
      return pool.slice(0, 2).map(x => x.index);
    } else if (cname === "Death") {
      // Convert left card into right card: 2 cards needed
      if (indexed.length < 2) return [];
      const sorted = [...indexed].sort((a, b) => {
        const valA = (a.card.enhancement || a.card.edition || a.card.seal ? 100 : 0) + getRankVal(a.card.rank);
        const valB = (b.card.enhancement || b.card.edition || b.card.seal ? 100 : 0) + getRankVal(b.card.rank);
        return valA - valB;
      });
      const worst = sorted[0];
      const best = sorted[sorted.length - 1];
      if (worst.index !== best.index) return [worst.index, best.index];
      return [sorted[0].index, sorted[1].index];
    } else if (cname === "The Chariot") {
      // Steel Card (+1.5x Mult held in hand):
      // If Baron present, target King! Otherwise target low unenhanced card kept in hand
      if (hasBaron) {
        const kings = indexed.filter(x => x.card.rank === "K" && x.card.enhancement !== "steel");
        if (kings.length > 0) return [kings[0].index];
      }
      const unenhanced = indexed.filter(x => !x.card.enhancement && !x.card.seal && !x.card.edition);
      const pool = unenhanced.length > 0 ? unenhanced : indexed;
      pool.sort((a, b) => getRankVal(a.card.rank) - getRankVal(b.card.rank));
      return [pool[0].index];
    } else if (cname === "The Devil") {
      // Gold Card ($3 held in hand at end of round): target low unenhanced card kept in hand
      const unenhanced = indexed.filter(x => !x.card.enhancement && !x.card.seal && !x.card.edition);
      const pool = unenhanced.length > 0 ? unenhanced : indexed;
      pool.sort((a, b) => getRankVal(a.card.rank) - getRankVal(b.card.rank));
      return [pool[0].index];
    } else if (cname === "The Tower") {
      // Stone Card (+50 chips): target lowest unenhanced junk
      const unenhanced = indexed.filter(x => !x.card.enhancement && !x.card.seal && !x.card.edition);
      const pool = unenhanced.length > 0 ? unenhanced : indexed;
      pool.sort((a, b) => getRankVal(a.card.rank) - getRankVal(b.card.rank));
      return [pool[0].index];
    } else if (cname === "Justice") {
      // Glass Card (2x Mult when scored): target highest rank card (Ace / King)
      const sorted = [...indexed].sort((a, b) => getRankVal(b.card.rank) - getRankVal(a.card.rank));
      return [sorted[0].index];
    } else if (cname === "The Empress" || cname === "The Hierophant" || cname === "The Magician") {
      // Empress (+4 Mult), Hierophant (+30 Chips), Magician (Lucky): target up to 2 unenhanced highest ranks
      const unenhanced = indexed.filter(x => !x.card.enhancement);
      const pool = unenhanced.length > 0 ? unenhanced : indexed;
      pool.sort((a, b) => getRankVal(b.card.rank) - getRankVal(a.card.rank));
      return pool.slice(0, 2).map(x => x.index);
    } else if (cname === "The Lovers") {
      // Wild Card: target unenhanced card
      const unenhanced = indexed.filter(x => !x.card.enhancement);
      const pool = unenhanced.length > 0 ? unenhanced : indexed;
      pool.sort((a, b) => getRankVal(b.card.rank) - getRankVal(a.card.rank));
      return [pool[0].index];
    } else if (cname === "Strength") {
      // Target up to 2 cards rank < 'A'
      const valid = indexed.filter(x => getRankVal(x.card.rank) < getRankVal("A"));
      const pool = valid.length > 0 ? valid : indexed;
      pool.sort((a, b) => getRankVal(b.card.rank) - getRankVal(a.card.rank));
      return pool.slice(0, 2).map(x => x.index);
    } else if (["The Moon", "The Sun", "The World", "The Star"].includes(cname)) {
      // Suit converters (up to 3 cards to target suit)
      const targetSuitMap = { "The Moon": "C", "The Sun": "H", "The World": "S", "The Star": "D" };
      const targetSuit = targetSuitMap[cname];
      const offSuit = indexed.filter(x => (x.card.suit || "")[0] !== targetSuit);
      offSuit.sort((a, b) => getRankVal(b.card.rank) - getRankVal(a.card.rank));
      return offSuit.slice(0, 3).map(x => x.index);
    } else if (cname === "Cryptid") {
      // Spectral Cryptid: duplicate best card
      const sorted = [...indexed].sort((a, b) => {
        const valA = (a.card.edition ? 200 : 0) + (["glass", "steel"].includes(a.card.enhancement) ? 100 : 0) + (a.card.seal === "red" ? 100 : 0) + getRankVal(a.card.rank);
        const valB = (b.card.edition ? 200 : 0) + (["glass", "steel"].includes(b.card.enhancement) ? 100 : 0) + (b.card.seal === "red" ? 100 : 0) + getRankVal(b.card.rank);
        return valB - valA;
      });
      return [sorted[0].index];
    } else if (cname === "Aura") {
      // Add Foil/Holo/Poly to 1 card without edition
      const noEd = indexed.filter(x => !x.card.edition);
      const pool = noEd.length > 0 ? noEd : indexed;
      pool.sort((a, b) => getRankVal(b.card.rank) - getRankVal(a.card.rank));
      return [pool[0].index];
    } else if (cname === "Deja Vu") {
      // Red seal (retrigger) to high rank scoring card
      const noSeal = indexed.filter(x => !x.card.seal);
      const pool = noSeal.length > 0 ? noSeal : indexed;
      pool.sort((a, b) => getRankVal(b.card.rank) - getRankVal(a.card.rank));
      return [pool[0].index];
    } else if (cname === "Trance") {
      // Blue seal (Planet card in hand) to low rank kept card
      const noSeal = indexed.filter(x => !x.card.seal);
      const pool = noSeal.length > 0 ? noSeal : indexed;
      pool.sort((a, b) => getRankVal(a.card.rank) - getRankVal(b.card.rank));
      return [pool[0].index];
    } else if (cname === "Talisman") {
      // Gold seal ($3 on score) to high rank scoring card
      const noSeal = indexed.filter(x => !x.card.seal);
      const pool = noSeal.length > 0 ? noSeal : indexed;
      pool.sort((a, b) => getRankVal(b.card.rank) - getRankVal(a.card.rank));
      return [pool[0].index];
    } else if (cname === "Medium") {
      // Purple seal (Tarot on discard) to low rank junk card
      const noSeal = indexed.filter(x => !x.card.seal);
      const pool = noSeal.length > 0 ? noSeal : indexed;
      pool.sort((a, b) => getRankVal(a.card.rank) - getRankVal(b.card.rank));
      return [pool[0].index];
    }
    return [];
  }

  // Alias for backward compatibility
  const selectTarotHandTargets = selectConsumableTargets;

  // Consumable Evaluation System
  function findBestConsumableToUse(state) {
    const cons = state.consumables || [];
    if (cons.length === 0) return null;
    const PLANET_NAMES = ["Mercury", "Venus", "Earth", "Mars", "Jupiter", "Saturn", "Uranus", "Neptune", "Pluto", "Planet X", "Ceres", "Eris"];
    const TARGETED_CONSUMABLE_SET = new Set([
      "The Hanged Man", "Death", "The Chariot", "Justice", "The Devil", "The Tower",
      "The Empress", "The Hierophant", "The Magician", "The Lovers", "Strength",
      "The Moon", "The Sun", "The World", "The Star", "Cryptid", "Aura",
      "Deja Vu", "Trance", "Talisman", "Medium"
    ]);

    for (let i = 0; i < cons.length; i++) {
      const c = cons[i];
      const name = c.name || "";
      const isPlanet = (c.set === "Planet") || PLANET_NAMES.includes(name) || (c.type === "planet");
      if (c.can_use === false && !isPlanet) continue;
      const idx = c.index !== undefined ? c.index : (i + 1);

      // Black Hole: upgrades ALL poker hands +1 level!
      if (name === "Black Hole") {
        return { card: c, index: idx, targetIndices: null, reason: "全牌型等级统一 +1 级！" };
      }
      // Planet cards: permanently boost hand level!
      if (isPlanet) {
        return { card: c, index: idx, targetIndices: null, reason: `升级手牌基础等级 (${name})` };
      }
      // The Hermit: Doubles money up to $20!
      if (name === "The Hermit" && (state.money || 0) >= 5) {
        return { card: c, index: idx, targetIndices: null, reason: `金钱翻倍 (现持 $${state.money})` };
      }
      // Temperance: Gives total sell value of all jokers
      if (name === "Temperance") {
        let totalSell = 0;
        for (const j of (state.jokers || [])) totalSell += (j.sell_cost || j.sell_value || 1);
        if (totalSell >= 4) return { card: c, index: idx, targetIndices: null, reason: `变现小丑总价值 (+$${totalSell})` };
      }
      // The Wheel of Fortune: 1 in 4 to add Foil, Holo, or Polychrome to a random joker
      if (name === "The Wheel of Fortune") {
        const hasEditionless = (state.jokers || []).some(j => !j.edition);
        if (hasEditionless) return { card: c, index: idx, targetIndices: null, reason: "为小丑博取闪箔/全息/多彩特效" };
      }
      // Emperor / High Priestess: Add tarot / planet cards
      if (name === "The Emperor" || name === "The High Priestess") {
        const maxSlots = Number(state.consumable_slots) || 2;
        if (cons.length < maxSlots) return { card: c, index: idx, targetIndices: null, reason: `获取补充消耗牌 (${name})` };
      }
      // The Fool: duplicate last tarot/planet
      if (name === "The Fool") {
        return { card: c, index: idx, targetIndices: null, reason: "复制上一张使用的塔罗/星球牌" };
      }
      // Judgement: Create a random joker
      if (name === "Judgement") {
        const maxJokers = Number(state.joker_slots) || 5;
        if ((state.jokers || []).length < maxJokers) return { card: c, index: idx, targetIndices: null, reason: "随机生成新小丑" };
      }
      // Immolate: destroys 5 cards, gains $20
      if (name === "Immolate" && (state.hand || []).length >= 5) {
        return { card: c, index: idx, targetIndices: null, reason: "销毁 5 张杂牌并获取 $20 现金" };
      }
      // Targeted Consumables (Tarots, Spectrals)
      if (TARGETED_CONSUMABLE_SET.has(name)) {
        const targets = selectConsumableTargets(name, state.hand, state.jokers);
        if (targets && targets.length > 0) {
          return { card: c, index: idx, targetIndices: targets, reason: `强化/改造手牌 (${name} 作用于 ${targets.length} 张牌)` };
        }
      }
    }
    return null;
  }

  // Non-combat consumable evaluation (SHOP, BLIND_SELECT)
  function findBestNonCombatConsumableToUse(state) {
    const cons = state.consumables || [];
    if (cons.length === 0) return null;
    const PLANET_NAMES = ["Mercury", "Venus", "Earth", "Mars", "Jupiter", "Saturn", "Uranus", "Neptune", "Pluto", "Planet X", "Ceres", "Eris"];

    for (let i = 0; i < cons.length; i++) {
      const c = cons[i];
      const name = c.name || "";
      const isPlanet = (c.set === "Planet") || PLANET_NAMES.includes(name) || (c.type === "planet");
      if (c.can_use === false && !isPlanet) continue;
      const idx = c.index !== undefined ? c.index : (i + 1);

      // Black Hole: upgrades ALL poker hands +1 level!
      if (name === "Black Hole") {
        return { card: c, index: idx, targetIndices: null, reason: "全牌型等级统一 +1 级！" };
      }
      // Planet cards: permanently boost hand level and free consumable slot!
      if (isPlanet) {
        return { card: c, index: idx, targetIndices: null, reason: `升级手牌基础等级 (${name}) 并腾出槽位` };
      }
      // The Hermit: Doubles money up to $20!
      if (name === "The Hermit" && (state.money || 0) >= 4) {
        return { card: c, index: idx, targetIndices: null, reason: `金钱翻倍 (现持 $${state.money})` };
      }
      // Temperance: Gives total sell value of all jokers (max $50)
      if (name === "Temperance") {
        let totalSell = 0;
        for (const j of (state.jokers || [])) totalSell += (j.sell_cost || j.sell_value || 1);
        if (totalSell >= 3) return { card: c, index: idx, targetIndices: null, reason: `变现小丑总价值 (+$${totalSell})` };
      }
      // The Wheel of Fortune: 1 in 4 to add Foil, Holo, or Polychrome to a random joker
      if (name === "The Wheel of Fortune") {
        const hasEditionless = (state.jokers || []).some(j => !j.edition);
        if (hasEditionless) return { card: c, index: idx, targetIndices: null, reason: "为小丑博取闪箔/全息/多彩特效" };
      }
      // Judgement: Create a random joker if empty slot
      if (name === "Judgement") {
        const maxJokers = Number(state.joker_slots) || 5;
        if ((state.jokers || []).length < maxJokers) return { card: c, index: idx, targetIndices: null, reason: "空槽随机生成新小丑" };
      }
      // Emperor / High Priestess: Add tarot / planet cards if empty slot
      if (name === "The Emperor" || name === "The High Priestess") {
        const maxSlots = Number(state.consumable_slots) || 2;
        if (cons.length < maxSlots) return { card: c, index: idx, targetIndices: null, reason: `获取补充消耗牌 (${name})` };
      }
      // The Fool: duplicate last tarot/planet
      if (name === "The Fool") {
        return { card: c, index: idx, targetIndices: null, reason: "复制上一张使用的塔罗/星球牌" };
      }
      // The Soul: Creates a Legendary Joker!
      if (name === "The Soul") {
        const maxJokers = Number(state.joker_slots) || 5;
        if ((state.jokers || []).length < maxJokers) return { card: c, index: idx, targetIndices: null, reason: "召唤传奇小丑！" };
      }
      // Spectral non-combat utilities
      if (name === "Ankh") {
        const jCount = (state.jokers || []).length;
        const maxJokers = Number(state.joker_slots) || 5;
        if (jCount === 1 && maxJokers > 1) return { card: c, index: idx, targetIndices: null, reason: "克隆单核心小丑 (Ankh)" };
      }
      if (name === "Hex") {
        const jCount = (state.jokers || []).length;
        if (jCount === 1) return { card: c, index: idx, targetIndices: null, reason: "单核心小丑赋予多彩 (Hex)" };
      }
      if (name === "Wraith") {
        const maxJokers = Number(state.joker_slots) || 5;
        if ((state.jokers || []).length < maxJokers && (state.money || 0) <= 5) return { card: c, index: idx, targetIndices: null, reason: "低金钱时召唤稀有小丑 (Wraith)" };
      }
    }
    return null;
  }

  // Pack Card Usability Checker: Prevents selecting cards whose activation conditions are not met
  function canUsePackCard(card, state) {
    if (!card) return false;
    const name = card.name || "";
    const sname = state.state_name;
    const jCount = (state.jokers || []).length;
    const jMax = Number(state.joker_slots) || 5;
    const cCount = (state.consumables || []).length;
    const cMax = Number(state.consumable_slots) || 2;
    const handLen = (state.hand || []).length;

    if (card.set === "Joker" || sname === "BUFFOON_PACK") {
      return jCount < jMax || (card.edition && card.edition === "negative");
    }
    if (name === "Judgement") {
      return jCount < jMax;
    }
    if (name === "The Emperor" || name === "The High Priestess") {
      return cCount < cMax;
    }
    if (name === "The Wheel of Fortune") {
      return (state.jokers || []).some(j => !j.edition);
    }
    if (name === "Death") {
      return handLen >= 2;
    }
    if (["The Hanged Man", "The Chariot", "Justice", "The Devil", "The Tower",
         "The Empress", "The Hierophant", "The Magician", "The Lovers", "Strength",
         "The Moon", "The Sun", "The World", "The Star"].includes(name)) {
      return handLen >= 1;
    }
    if (sname === "SPECTRAL_PACK") {
      if (name === "Ankh" || name === "Hex") return jCount >= 1;
      if (name === "Cryptid" || name === "Aura" || name === "Deja Vu" || name === "Trance" || name === "Talisman" || name === "Medium") {
        return handLen >= 1;
      }
    }
    return true;
  }

  // DecideX HUD Renderer
  window.onBalatroStateUpdate = function (state) {
    currentGameState = state;
    window._lastDecidexState = state;
    window._lastDecidexStateCount = (window._lastDecidexStateCount || 0) + 1;

    // 1. Immediately notify AutoPilot so state transitions are NEVER blocked by early returns!
    if (window.AutoPilot && typeof window.AutoPilot.onStateUpdated === "function") {
      window.AutoPilot.onStateUpdated(state);
    }

    // Update Quick Status Strip
    const cr = state;
    document.getElementById("hud-ante").textContent = cr.ante || 1;
    document.getElementById("hud-round").textContent = cr.round || 1;
    document.getElementById("hud-money").textContent = "$" + (cr.money || 0);
    document.getElementById("hud-hands").textContent = cr.hands_left || 0;
    document.getElementById("hud-discards").textContent = cr.discards_left || 0;

    // Update Detailed Context Bar
    const elDeck = document.getElementById("hud-deck");
    if (elDeck) elDeck.textContent = cr.deck_count !== undefined ? cr.deck_count : (52 - ((cr.hand || []).length));
    const elDiscardPile = document.getElementById("hud-discard-pile");
    if (elDiscardPile) elDiscardPile.textContent = cr.discard_count !== undefined ? cr.discard_count : 0;
    const elJokerSlots = document.getElementById("hud-joker-slots");
    if (elJokerSlots) {
      const jCount = cr.jokers_count !== undefined ? cr.jokers_count : ((cr.jokers || []).length);
      const jMax = cr.joker_slots || 5;
      elJokerSlots.textContent = `${jCount}/${jMax}`;
    }

    const blind = state.blind || {};
    const blindGoal = Number(blind.chips || 300);
    const scored = Number(blind.scored || 0);
    const need = Math.max(0, blindGoal - scored);

    const container = document.getElementById("primary-rec-container");
    const candList = document.getElementById("candidates-container");

    // Phase: SHOP
    if (state.state_name === "SHOP") {
      const shopAdv = findBestShopPurchases(state);
      let shelfHtml = "";
      for (const card of shopAdv.evaluatedCards) {
        shelfHtml += `
          <div style="display:flex;justify-content:space-between;align-items:center;background:rgba(255,255,255,0.05);padding:6px 10px;margin-bottom:6px;border-radius:4px;">
            <div>
              <span style="font-weight:bold;color:${card.edition ? '#a855f7' : '#fff'};">${escapeHtml(card.name)}</span>
              <span style="font-size:11px;color:#f59e0b;margin-left:6px;">$${Number(card.cost) || 0}</span>
              ${card.edition ? `<span class="vz-badge vz-warn" style="font-size:9px;padding:1px 4px;margin-left:4px;">${escapeHtml(card.edition)}</span>` : ''}
              <div style="font-size:10px;color:#888;">${escapeHtml(card.recommendation)} (评分 ${card.score})</div>
            </div>
            ${card.canAfford && card.hasSlot ? `
              <button class="btn-act btn-play" style="padding:4px 10px;font-size:11px;" onclick="executeBuyAction(${card.index}, 'shop_jokers')">购买</button>
            ` : `<span style="font-size:11px;color:#666;">${!card.hasSlot ? '槽满' : '资金不足'}</span>`}
          </div>
        `;
      }
      if (shopAdv.evaluatedCards.length === 0) {
        shelfHtml = `<div style="font-size:11px;color:#888;padding:8px 0;">当前货架暂无在售小丑牌</div>`;
      }

      container.innerHTML = `
        <div class="recommend-card">
          <div class="rec-header">
            <span class="rec-action-type">🛒 商店智能采购顾问</span>
            <span class="vz-badge vz-good">小丑槽: ${Number(state.jokers_count) || 0}/${Number(state.joker_slots) || 5}</span>
          </div>
          <div style="font-size:11px;color:#38bdf8;margin:6px 0;">${escapeHtml(shopAdv.interestAdvice)}</div>
          ${shopAdv.bestCard ? `
            <div style="background:rgba(34,197,94,0.15);border:1px solid rgba(34,197,94,0.3);padding:8px;border-radius:6px;margin:8px 0;">
              <div style="font-weight:bold;color:#4ade80;">💡 优先推荐: ${escapeHtml(shopAdv.bestCard.name)} ($${Number(shopAdv.bestCard.cost) || 0})</div>
              <div style="font-size:11px;color:#ccc;margin-top:2px;">极佳战力提升，完美契合当前构筑！</div>
              <button class="btn-act btn-play" style="margin-top:6px;width:100%;" onclick="executeBuyAction(${shopAdv.bestCard.index}, 'shop_jokers')">🛒 一键购买推荐小丑</button>
            </div>
          ` : `
            <div style="background:rgba(234,179,8,0.1);border:1px solid rgba(234,179,8,0.25);padding:8px;border-radius:6px;margin:8px 0;font-size:11px;color:#facc15;">
              💡 暂无质变小丑，建议保留本金吃利息或适当刷新
            </div>
          `}
          <div style="font-weight:bold;font-size:11px;color:#888;margin:8px 0 4px 0;">在售小丑清单</div>
          ${shelfHtml}
          <div style="display:flex;gap:6px;margin-top:10px;">
            <button class="btn-act btn-play" style="flex:1;padding:6px;" onclick="executeRerollShop()">🔄 刷新商店 ($${shopAdv.rerollCost})</button>
            <button class="btn-act btn-discard" style="flex:1;padding:6px;" onclick="executeToggleShop()">➡️ 进入下轮</button>
          </div>
        </div>
      `;

      if (candList) candList.innerHTML = `<div style="font-size:11px;color:#888;">商店阶段无需出牌</div>`;
      return;
    }

    // Phase: BLIND_SELECT
    if (state.state_name === "BLIND_SELECT") {
      const choice = evaluateBlindChoice(state);
      const isSkipRec = choice.action === "skip_blind";
      const tagHtml = choice.tagName ? `
        <div style="margin: 6px 0; padding: 6px 10px; background: rgba(255,255,255,0.06); border-radius: 6px; border: 1px solid rgba(255,255,255,0.1);">
          <div style="display:flex; justify-content:space-between; font-size:12px;">
            <span>🏷️ 当前 Tag: <b style="color:#f59e0b;">${choice.tagName}</b> (${choice.tagTier}级)</span>
            <span>Tag 期望: <b style="color:${isSkipRec ? '#4ade80' : '#888'};">${choice.skipEv}</b> vs 盲注: <b style="color:${!isSkipRec ? '#4ade80' : '#888'};">${choice.selectEv}</b></span>
          </div>
          <div style="font-size:11px; color:#aaa; margin-top:2px;">${choice.tagDesc || ''}</div>
          ${choice.riskWarning ? `<div style="font-size:11px; color:#ef4444; margin-top:4px;">${choice.riskWarning}</div>` : ''}
        </div>
      ` : '';

      container.innerHTML = `
        <div class="rec-placeholder" style="text-align: left; padding: 10px 14px;">
          <div style="font-weight: bold; margin-bottom: 4px; font-size: 13px;">🎯 盲注选择: <span style="color:#60a5fa;">${state.blind?.name || 'Small Blind'}</span> (目标 ${(state.blind?.chips || 300).toLocaleString()} 筹码)</div>
          ${tagHtml}
          <div style="font-size: 11px; margin-bottom: 8px; color: #ccc;">💡 DecideX 建议: ${choice.reason}</div>
          <div style="display:flex; gap: 8px;">
            <button class="btn-act ${!isSkipRec ? 'btn-play' : ''}" style="flex:1; padding: 6px 10px; border-radius: 6px; ${!isSkipRec ? 'font-weight:bold; border: 1px solid #4ade80;' : 'background:#374151; color:#aaa;'}" onclick="window.decidexSelectBlind()">
              ▶️ 进入盲注 (${choice.selectEv})
            </button>
            ${choice.canSkip ? `
            <button class="btn-act ${isSkipRec ? 'btn-play' : ''}" style="flex:1; padding: 6px 10px; border-radius: 6px; ${isSkipRec ? 'font-weight:bold; border: 1px solid #f59e0b; background:#b45309;' : 'background:#374151; color:#aaa;'}" onclick="window.decidexSkipBlind()">
              ⏩ 跳过盲注 (${choice.skipEv})
            </button>` : ''}
          </div>
        </div>`;
      if (candList) candList.innerHTML = `<div style="font-size:11px;color:#888;">盲注选择阶段: ${choice.reason}</div>`;
      return;
    }

    // Phase: GAME_WON / Victory
    if (state.state_name === "GAME_WON" || state.won) {
      container.innerHTML = `
        <div class="rec-placeholder">
          <div style="font-weight: bold; margin-bottom: 6px; color: #4ade80; font-size: 15px;">🏆 恭喜通关！胜利击败 Ante 8 终极 Boss</div>
          <div style="font-size: 12px; margin-bottom: 12px; color: #94a3b8;">本局已达成通关胜利！可继续无尽模式挑战极限高分或重开新对局</div>
          <div style="display:flex; gap: 8px; justify-content: center;">
            <button class="btn-act btn-play" style="padding: 7px 16px;" onclick="window.decidexContinueEndless()">🚀 无尽模式 (Endless)</button>
            <button class="btn-act btn-discard" style="padding: 7px 16px;" onclick="window.decidexStartRun()">🔄 重新开启新局</button>
          </div>
        </div>`;
      if (candList) candList.innerHTML = `<div style="font-size:11px;color:#888;">通关胜利！</div>`;
      return;
    }

    // Phase: GAME_OVER
    if (state.state_name === "GAME_OVER") {
      container.innerHTML = `
        <div class="rec-placeholder">
          <div style="font-weight: bold; margin-bottom: 6px; color: #ef4444; font-size: 15px;">💀 游戏结束 (Game Over)</div>
          <div style="font-size: 12px; margin-bottom: 12px; color: #94a3b8;">到达 Ante ${state.ante || 1} · 回合 ${state.round || 1} · 资金 $${state.money || 0}</div>
          <button class="btn-act btn-play" style="padding: 8px 20px; margin: 4px auto;" onclick="window.decidexStartRun()">🔄 重新开始新一局</button>
        </div>`;
      if (candList) candList.innerHTML = `<div style="font-size:11px;color:#888;">游戏已结算，点击上方按钮开启全新对局</div>`;
      return;
    }

    // Phase: ROUND_EVAL
    if (state.state_name === "ROUND_EVAL") {
      container.innerHTML = `
        <div class="rec-placeholder">
          <div style="font-weight: bold; margin-bottom: 6px; color: #4ade80;">🎉 盲注通关！回合结算</div>
          <button class="btn-act btn-play" style="padding: 6px 14px; margin: 6px auto;" onclick="window.decidexCashOut()">💰 一键结算并进入商店</button>
          <div style="font-size: 11px; margin-top: 6px; color: #888;">结算金币与利息后，将前往商店选购小丑牌与补充包</div>
        </div>`;
      if (candList) candList.innerHTML = `<div style="font-size:11px;color:#888;">结算阶段无需出牌</div>`;
      return;
    }

    // Phase: SELECTING_HAND
    const topPlays = findBestPlays(state);
    if (topPlays.length === 0) {
      const stateLabels = {
        "DRAW_TO_HAND": "当前阶段: 正在发牌...",
        "HAND_PLAYED": "当前阶段: 计分结算中...",
        "ROUND_EVAL": "当前阶段: 回合结算",
        "MENU": "当前阶段: 主菜单",
        "SPLASH": "当前阶段: 标题界面"
      };
      const label = stateLabels[state.state_name] || `当前阶段: ${state.state_name || "准备就绪"}`;
      container.innerHTML = `<div class="rec-placeholder"><div>${label}</div><div style="font-size: 11px; margin-top: 4px; color: #888;">选牌阶段开启后，AI 将自动分析当前手牌的最优打法</div></div>`;
      if (candList) candList.innerHTML = `<div style="font-size:11px;color:#888;">暂无候选打法</div>`;
      return;
    }

    currentRecommendation = topPlays[0];
    const bestDiscard = findBestDiscards(state, currentRecommendation);
    const arbitration = arbitrateAction(state, currentRecommendation, bestDiscard);
    window._currentArbitration = arbitration;
    if (arbitration && arbitration.cards) {
      window._lastBestPlayCards = arbitration.cards;
    } else {
      window._lastBestPlayCards = currentRecommendation.cards;
    }
    const bestPlay = (arbitration && arbitration.verdict === "PLAY" && arbitration.cards)
      ? { ...currentRecommendation, cards: arbitration.cards, junkCards: (arbitration.cards.length > (currentRecommendation.cards || []).length) ? arbitration.cards.slice((currentRecommendation.cards || []).length) : [] }
      : currentRecommendation;
    window.currentRecommendation = bestPlay;

    container.innerHTML = "";

    // 0. Strategy Alerts: Joker Ordering & Passive Constraints
    const orderAdv = computeOptimalJokerOrder(state.jokers);
    if (orderAdv.needsReorder) {
      const orderBox = document.createElement("div");
      orderBox.style.cssText = "background:rgba(239,68,68,0.15);border:1px solid rgba(239,68,68,0.4);border-radius:6px;padding:8px;margin-bottom:8px;font-size:11px;color:#fca5a5;";
      orderBox.innerHTML = `<strong>⚠️ 小丑排序告警:</strong> ${escapeHtml(orderAdv.reason)}`;
      container.appendChild(orderBox);
    }

    const constraints = detectConstraints(state.jokers, state.blind);
    if (constraints.noFaceCards) {
      const cBox = document.createElement("div");
      cBox.style.cssText = "background:rgba(234,179,8,0.15);border:1px solid rgba(234,179,8,0.4);border-radius:6px;padding:6px 8px;margin-bottom:8px;font-size:11px;color:#fde047;";
      cBox.innerHTML = `<strong>🛡️ 被动约束生效:</strong> Ride the Bus 在场，严禁打出人头牌 (J/Q/K)，出牌推荐已主动过滤避险。`;
      container.appendChild(cBox);
    }

    // 0. Stage Decision Arbitration Banner
    if (arbitration) {
      const arbBox = document.createElement("div");
      arbBox.className = `arbitration-banner ${arbitration.badgeClass}`;
      arbBox.innerHTML = `
        <div class="arb-header">
          <span class="arb-title">${escapeHtml(arbitration.title)}</span>
          <span class="arb-confidence-tag">${escapeHtml(arbitration.confidence)}</span>
        </div>
        <div class="arb-reason">${escapeHtml(arbitration.reason)}</div>
      `;
      container.appendChild(arbBox);
    }

    // 1. Primary Recommendation Card (Best Play)
    const playBox = document.createElement("div");
    const isPlayPreferred = arbitration && arbitration.verdict === "PLAY";
    playBox.className = "recommend-card" + (isPlayPreferred ? " rec-card-preferred" : " rec-card-dimmed");

    let jokerBadgesHtml = "";
    if (bestPlay.jokerBadges && bestPlay.jokerBadges.length > 0) {
      jokerBadgesHtml = `<div style="display:flex;flex-wrap:wrap;gap:4px;margin-top:6px;">` +
        bestPlay.jokerBadges.map(b => `
          <span style="font-size:10px;background:rgba(59,130,246,0.15);border:1px solid rgba(59,130,246,0.3);padding:1px 5px;border-radius:3px;color:#93c5fd;">
            🃏 ${escapeHtml(b.name)}${b.chips ? ' +' + b.chips + '筹码' : ''}${b.mult ? ' +' + b.mult + '倍' : ''}${b.xmult > 1 ? ' ×' + b.xmult : ''}
          </span>
        `).join("") + `</div>`;
    }

    playBox.innerHTML = `
      <div class="rec-header">
        <span class="rec-action-type">
          ${isPlayPreferred ? '<span class="arb-pick-tag">⭐ 决策首选</span> ' : ''}⚡ 出牌推荐: ${escapeHtml(bestPlay.handName)}
        </span>
        <span class="vz-badge ${bestPlay.totalScore >= need ? "vz-good" : "vz-warn"}">
          ${bestPlay.totalScore >= need ? "可直接过关" : `差 ${need - bestPlay.totalScore} 分`}
        </span>
      </div>
      <div class="rec-chips-mult">
        <span class="chips-box">${bestPlay.chips}</span>
        <span class="score-equals">×</span>
        <span class="mult-box">${bestPlay.mult}</span>
        <span class="score-equals">=</span>
        <span class="total-score">${bestPlay.totalScore.toLocaleString()}</span>
      </div>
      <div class="mini-cards-row">
        ${bestPlay.cards.map(c => `
          <div class="mini-card suit-${c.suit}">
            <span class="card-rank">${escapeHtml(c.rank)}</span>
            <span class="card-suit">${SUIT_SYMBOLS[c.suit] || escapeHtml(c.suit)}</span>
          </div>
        `).join("")}
      </div>
      ${jokerBadgesHtml}
      <div class="rec-actions">
        <button class="btn-act btn-play" onclick="executePlayAction(window._lastBestPlayCards)">▶️ 一键打出推荐牌 (${bestPlay.cards.length}张)</button>
      </div>
    `;
    container.appendChild(playBox);

    // 2. Strategic Discard Recommendation Card
    if (bestDiscard && bestDiscard.cards && bestDiscard.cards.length > 0) {
      const isDiscardPreferred = arbitration && arbitration.verdict === "DISCARD";
      const discardBox = document.createElement("div");
      discardBox.className = "recommend-card" + (isDiscardPreferred ? " rec-card-preferred" : " rec-card-dimmed");
      discardBox.style.marginTop = "8px";
      discardBox.style.border = isDiscardPreferred ? "1px solid rgba(239, 68, 68, 0.8)" : "1px solid rgba(239, 68, 68, 0.3)";
      discardBox.style.background = isDiscardPreferred ?
        "linear-gradient(135deg, rgba(30, 41, 59, 0.85) 0%, rgba(239, 68, 68, 0.18) 100%)" :
        "linear-gradient(135deg, rgba(30, 41, 59, 0.7) 0%, rgba(239, 68, 68, 0.08) 100%)";

      discardBox.innerHTML = `
        <div class="rec-header">
          <span class="rec-action-type" style="color:#f87171;">
            ${isDiscardPreferred ? '<span class="arb-pick-tag arb-discard-tag">⭐ 决策首选</span> ' : ''}🗑️ 战略弃牌: ${escapeHtml(bestDiscard.target)}
          </span>
          <span class="vz-badge vz-warn">余弃牌 ${Number(state.discards_left) || 0}</span>
        </div>
        <div style="font-size:11px;color:#cbd5e1;margin:4px 0 6px 0;line-height:1.4;">${escapeHtml(bestDiscard.reason)}</div>
        <div class="mini-cards-row">
          ${bestDiscard.cards.map(c => `
            <div class="mini-card suit-${c.suit}" style="opacity:0.85;border-color:rgba(239,68,68,0.5);">
              <span class="card-rank">${escapeHtml(c.rank)}</span>
              <span class="card-suit">${SUIT_SYMBOLS[c.suit] || escapeHtml(c.suit)}</span>
            </div>
          `).join("")}
        </div>
        <div class="rec-actions">
          <button class="btn-act btn-discard" onclick="executeDiscardAction(window._lastBestDiscardCards)">🗑️ 一键弃掉杂牌 (${bestDiscard.cards.length}张)</button>
        </div>
      `;
      window._lastBestDiscardCards = bestDiscard.cards;
      container.appendChild(discardBox);
    }

    // 3. Render Top Candidates
    if (candList) {
      candList.innerHTML = "";
      const maxScore = bestPlay.totalScore || 1;
      for (let i = 0; i < Math.min(5, topPlays.length); i++) {
        const p = topPlays[i];
        const pct = Math.min(100, Math.round((p.totalScore / maxScore) * 100));
        const row = document.createElement("div");
        row.className = "candidate-row";
        row.innerHTML = `
          <div class="cand-top">
            <span>#${i + 1} ${p.handName} (${p.cards.map(c => c.rank + SUIT_SYMBOLS[c.suit]).join(" ")})</span>
            <b>${p.totalScore.toLocaleString()}</b>
          </div>
          <div class="cand-bar-wrap">
            <div class="cand-bar" style="width: ${pct}%"></div>
          </div>
        `;
        candList.appendChild(row);
      }
    }
  };

  let luaActionQueue = Promise.resolve();
  let luaActionSequence = 0;
  function dispatchLuaAction(luaCode, label = "action") {
    const sequence = ++luaActionSequence;
    if (window.AutoPilot && typeof window.AutoPilot.onActionDispatch === "function") {
      window.AutoPilot.onActionDispatch(label, sequence);
    }
    const actionPromise = luaActionQueue.catch(() => {}).then(() => new Promise((resolve, reject) => {
      const FS = window.Module && window.Module.FS;
      if (!FS) return reject(new Error("游戏引擎尚未就绪"));
      const ackPath = "/tmp/balatro_eval_ack.txt";
      const evalPath = "/tmp/balatro_eval.lua";
      try { FS.unlink(ackPath); } catch (_) {}
      FS.writeFile(evalPath, `-- DECIDEX_SEQ:${sequence}\n${luaCode}`);
      const deadline = Date.now() + 2500;
      let evalConsumedTime = null;
      const poll = () => {
        try {
          const ack = FS.readFile(ackPath, { encoding: "utf8" }).trim();
          const parts = ack.split("|");
          if (parts[0] === String(sequence)) {
            if (parts[1] === "ok") return resolve(sequence);
            return reject(new Error(`Lua ${label} failed: ${parts.slice(2).join("|") || "unknown error"}`));
          }
        } catch (_) {}

        // Check if Lua has consumed and unlinked /tmp/balatro_eval.lua
        let evalExists = true;
        try {
          evalExists = FS.analyzePath(evalPath).exists;
        } catch (_) {}
        if (!evalExists) {
          if (!evalConsumedTime) {
            evalConsumedTime = Date.now();
          } else if (Date.now() - evalConsumedTime >= 80) {
            return resolve(sequence);
          }
        }

        if (Date.now() >= deadline) {
          if (!evalExists || evalConsumedTime) return resolve(sequence);
          return reject(new Error(`Lua ${label} acknowledgement timed out`));
        }
        setTimeout(poll, 40);
      };
      poll();
    }));
    luaActionQueue = actionPromise.catch(error => {
      console.error(`[DecideX] Lua action ${label} #${sequence} failed:`, error);
      if (window.AutoPilot && typeof window.AutoPilot.onActionDispatchFailure === "function") {
        window.AutoPilot.onActionDispatchFailure(label, error, sequence);
      }
      return false;
    });
    return luaActionQueue;
  }

  function reportActionDispatchFailure(action, error, sequence) {
    if (window.AutoPilot && typeof window.AutoPilot.onActionDispatchFailure === "function") {
      window.AutoPilot.onActionDispatchFailure(action, error, sequence);
    }
  }

  function unavailableAction(action) {
    const error = new Error("游戏引擎尚未就绪");
    console.warn(`[DecideX] Cannot dispatch ${action}:`, error.message);
    reportActionDispatchFailure(action, error, null);
    return Promise.resolve(false);
  }

  // Programmatic execution via serialized Emscripten virtual FS mailbox
  window.executePlayAction = function (cards) {
    if (!window.Module || !window.Module.FS) return unavailableAction("play");
    const st = currentGameState || window._lastDecidexState;
    const maxHand = (st && Array.isArray(st.hand)) ? st.hand.length : 8;
    const indices = (cards || [])
      .map(c => Number(c && c.index !== undefined ? c.index : c))
      .filter(x => Number.isInteger(x) && x >= 1 && x <= maxHand)
      .slice(0, 5);
    if (indices.length === 0) return unavailableAction("play");
    const luaCode = `local fn = (_G.DECIDEX_EXECUTE or (G and G.DECIDEX_EXECUTE)); if fn then fn("play", {indices = {${indices.join(",")}}}) end`;
    console.log("[DecideX] Queued play action:", luaCode);
    return dispatchLuaAction(luaCode, "play");
  };

  window.executeDiscardAction = function (cards) {
    if (!window.Module || !window.Module.FS) return unavailableAction("discard");
    const st = currentGameState || window._lastDecidexState;
    const maxHand = (st && Array.isArray(st.hand)) ? st.hand.length : 8;
    const indices = (cards || [])
      .map(c => Number(c && c.index !== undefined ? c.index : c))
      .filter(x => Number.isInteger(x) && x >= 1 && x <= maxHand)
      .slice(0, 5);
    if (indices.length === 0) return unavailableAction("discard");
    const luaCode = `local fn = (_G.DECIDEX_EXECUTE or (G and G.DECIDEX_EXECUTE)); if fn then fn("discard", {indices = {${indices.join(",")}}}) end`;
    console.log("[DecideX] Dispatched discard action:", luaCode);
    return dispatchLuaAction(luaCode, "discard");
  };

  window.executeBuyAction = function (index, area) {
    if (!window.Module || !window.Module.FS) return unavailableAction("buy_card");
    const luaCode = `local fn = (_G.DECIDEX_EXECUTE or (G and G.DECIDEX_EXECUTE)); if fn then fn("buy_card", {index = ${index}, area = "${area || 'shop_jokers'}"}) end`;
    console.log("[DecideX] Dispatched buy action:", luaCode);
    return dispatchLuaAction(luaCode, "buy_card");
  };

  window.executeSellCard = function (index, area) {
    if (!window.Module || !window.Module.FS) return unavailableAction("sell_card");
    const luaCode = `local fn = (_G.DECIDEX_EXECUTE or (G and G.DECIDEX_EXECUTE)); if fn then fn("sell_card", {index = ${index}, area = "${area || 'jokers'}"}) end`;
    console.log("[DecideX] Dispatched sell action:", luaCode);
    return dispatchLuaAction(luaCode, "sell_card");
  };

  window.executeRerollShop = function () {
    if (!window.Module || !window.Module.FS) return unavailableAction("reroll_shop");
    const luaCode = `local fn = (_G.DECIDEX_EXECUTE or (G and G.DECIDEX_EXECUTE)); if fn then fn("reroll_shop") end`;
    console.log("[DecideX] Dispatched reroll_shop action");
    return dispatchLuaAction(luaCode, "reroll_shop");
  };

  window.executeToggleShop = function () {
    if (!window.Module || !window.Module.FS) return unavailableAction("toggle_shop");
    const luaCode = `local fn = (_G.DECIDEX_EXECUTE or (G and G.DECIDEX_EXECUTE)); if fn then fn("toggle_shop") end`;
    console.log("[DecideX] Dispatched toggle_shop action");
    return dispatchLuaAction(luaCode, "toggle_shop");
  };

  window.executeReorderJokers = function (order) {
    if (!window.Module || !window.Module.FS) return unavailableAction("reorder_jokers");
    const orderStr = (order || []).join(", ");
    const luaCode = `local fn = (_G.DECIDEX_EXECUTE or (G and G.DECIDEX_EXECUTE)); if fn then fn("reorder_jokers", {order = {${orderStr}}}) end`;
    console.log("[DecideX] Dispatched reorder_jokers action:", orderStr);
    return dispatchLuaAction(luaCode, "reorder_jokers");
  };

  window.executeUseCard = function (index, targetIndices, area) {
    if (!window.Module || !window.Module.FS) return unavailableAction("use_card");
    const tIndices = (targetIndices && targetIndices.length > 0) ? `{${targetIndices.join(",")}}` : "nil";
    const luaCode = `local fn = (_G.DECIDEX_EXECUTE or (G and G.DECIDEX_EXECUTE)); if fn then fn("use_card", {index = ${index}, area = "${area || 'consumeables'}", target_indices = ${tIndices}}) end`;
    console.log("[DecideX] Dispatched use_card action");
    return dispatchLuaAction(luaCode, "use_card");
  };

  window.executeSkipBooster = function () {
    if (!window.Module || !window.Module.FS) return unavailableAction("skip_booster");
    const luaCode = `local fn = (_G.DECIDEX_EXECUTE or (G and G.DECIDEX_EXECUTE)); if fn then fn("skip_booster") end`;
    console.log("[DecideX] Dispatched skip_booster action");
    return dispatchLuaAction(luaCode, "skip_booster");
  };

  window.executeSelectPackCard = function (index, targetIndices) {
    if (!window.Module || !window.Module.FS) return unavailableAction("select_pack_card");
    const tIndices = (targetIndices && targetIndices.length > 0) ? `{${targetIndices.join(",")}}` : "nil";
    const luaCode = `local fn = (_G.DECIDEX_EXECUTE or (G and G.DECIDEX_EXECUTE)); if fn then fn("select_pack_card", {index = ${index}, target_indices = ${tIndices}}) end`;
    console.log("[DecideX] Dispatched select_pack_card action:", index, "targets:", targetIndices);
    return dispatchLuaAction(luaCode, "select_pack_card");
  };


  // Utilities
  window.decidexSelectBlind = function () {
    if (!window.Module || !window.Module.FS) return unavailableAction("select_blind");
    console.log("[DecideX] Dispatched select_blind action");
    return dispatchLuaAction('local fn = (_G.DECIDEX_EXECUTE or (G and G.DECIDEX_EXECUTE)); if fn then fn("select_blind") end', "select_blind");
  };

  window.decidexSkipBlind = function () {
    if (!window.Module || !window.Module.FS) return unavailableAction("skip_blind");
    console.log("[DecideX] Dispatched skip_blind action");
    return dispatchLuaAction('local fn = (_G.DECIDEX_EXECUTE or (G and G.DECIDEX_EXECUTE)); if fn then fn("skip_blind") end', "skip_blind");
  };

  window.decidexCashOut = function () {
    if (!window.Module || !window.Module.FS) return unavailableAction("cash_out");
    console.log("[DecideX] Dispatched cash_out action");
    return dispatchLuaAction('local fn = (_G.DECIDEX_EXECUTE or (G and G.DECIDEX_EXECUTE)); if fn then fn("cash_out") end', "cash_out");
  };

  window.decidexSortRank = function () {
    if (!window.Module || !window.Module.FS) return unavailableAction("sort_rank");
    return dispatchLuaAction('local fn = (_G.DECIDEX_EXECUTE or (G and G.DECIDEX_EXECUTE)); if fn then fn("sort_rank") end', "sort_rank");
  };

  window.decidexSortSuit = function () {
    if (!window.Module || !window.Module.FS) return unavailableAction("sort_suit");
    return dispatchLuaAction('local fn = (_G.DECIDEX_EXECUTE or (G and G.DECIDEX_EXECUTE)); if fn then fn("sort_suit") end', "sort_suit");
  };

  window.decidexReEvaluate = function () {
    if (!window.Module || !window.Module.FS) return unavailableAction("force_state_dump");
    return dispatchLuaAction('local fn = (_G.DECIDEX_EXECUTE or (G and G.DECIDEX_EXECUTE)); if fn then fn("force_state_dump") end', "force_state_dump");
  };

  window.decidexStartRun = function () {
    if (!window.Module || !window.Module.FS) return unavailableAction("start_run");
    console.log("[AutoPilot] Dispatched start_run action");
    return dispatchLuaAction('local fn = (_G.DECIDEX_EXECUTE or (G and G.DECIDEX_EXECUTE)); if fn then fn("start_run") end', "start_run");
  };

  window.decidexGoToMenu = function () {
    if (!window.Module || !window.Module.FS) return unavailableAction("go_to_menu");
    console.log("[AutoPilot] Dispatched go_to_menu action");
    return dispatchLuaAction('local fn = (_G.DECIDEX_EXECUTE or (G and G.DECIDEX_EXECUTE)); if fn then fn("go_to_menu") end', "go_to_menu");
  };

  window.decidexContinueEndless = function () {
    if (!window.Module || !window.Module.FS) return unavailableAction("continue_endless");
    console.log("[AutoPilot] Dispatched continue_endless action");
    return dispatchLuaAction('local fn = (_G.DECIDEX_EXECUTE or (G and G.DECIDEX_EXECUTE)); if fn then fn("continue_endless") end', "continue_endless");
  };


  // Save Export / Import
  window.decidexExportSave = function () {
    const FS = window.Module && window.Module.FS;
    if (!FS) return;
    try {
      const savePath = "/home/web_user/.local/share/love/Balatro/1/profile.jkr";
      if (!FS.analyzePath(savePath).exists) {
        console.warn("暂未找到本地存档文件");
        return;
      }
      const data = FS.readFile(savePath);
      const blob = new Blob([data], { type: "application/octet-stream" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "balatro_profile.jkr";
      a.click();
    } catch (e) {
      console.warn("导出存档失败: " + e.message);
    }
  };

  // Auto-Pilot Autonomous Game Player
  const AutoPilot = {
    enabled: false,
    busy: false,
    stepTimeout: null,
    watchdogTimer: null,
    pendingState: null,
    lastBlindSelectTime: 0,
    lastStartRunTime: 0,
    _lastProgressTimestamp: 0,
    _shopAttemptedItems: new Set(),
    _playSawHandPlayed: false,
    _discardSawDraw: false,

    toggle(enable) {
      this.enabled = enable !== undefined ? Boolean(enable) : !this.enabled;
      const chk = document.getElementById("autopilot-toggle");
      if (chk) chk.checked = this.enabled;
      this.updateStatus(this.enabled ? "运行中" : "已暂停", this.enabled);

      console.log(`[AutoPilot] ${this.enabled ? "Enabled" : "Disabled"}`);
      if (this.enabled) {
        this._lastProgressTimestamp = Date.now();
        this.startWatchdog();
        this.scheduleNextAction(200);
      } else {
        this.stopWatchdog();
        if (this.stepTimeout) {
          clearTimeout(this.stepTimeout);
          this.stepTimeout = null;
        }
        this.busy = false;
        this._actionInFlight = null;
        this._playSawHandPlayed = false;
        this._discardSawDraw = false;
        this.pendingState = null;
      }
    },

    startWatchdog() {
      if (this.watchdogTimer) clearInterval(this.watchdogTimer);
      // Responsive watchdog (350ms) with anti-hang and anti-deadlock guards
      this.watchdogTimer = setInterval(() => {
        if (!this.enabled) return;
        const now = Date.now();

        // 1. Force-clear busy lock if held too long (max 8.0s for play, 5.0s for discard, 3.0s for others)
        const maxBusyTime = (this._actionInFlight === "play") ? 8000 : ((this._actionInFlight === "discard") ? 5000 : 3000);
        if (this.busy && this._busyTimestamp && (now - this._busyTimestamp > maxBusyTime)) {
          console.warn(`[AutoPilot Watchdog] Busy lock held > ${maxBusyTime}ms for action ${this._actionInFlight}, auto-clearing busy lock`);
          this.busy = false;
          if (this._actionInFlight !== "select_blind" && this._actionInFlight !== "skip_blind" && this._actionInFlight !== "play" && this._actionInFlight !== "discard") {
            this._actionInFlight = null;
          }
          this._busyTimestamp = null;
        }

        // 2. Anti-hang: if stuck in an actionable state for too long without progress, force unlock
        if (!this._lastProgressTimestamp) this._lastProgressTimestamp = now;
        const curState = currentGameState || window._lastDecidexState;
        const curStateName = curState && curState.state_name;
        // In animation states (HAND_PLAYED, DRAW_TO_HAND, NEW_ROUND), refresh progress timestamp so watchdog doesn't break in-flight combat locks!
        if (["HAND_PLAYED", "DRAW_TO_HAND", "NEW_ROUND"].includes(curStateName)) {
          this._lastProgressTimestamp = now;
        }
        const maxStuckTime = (this._actionInFlight === "play") ? 15000 : ((this._actionInFlight === "discard") ? 8000 : 3800);
        if (now - this._lastProgressTimestamp > maxStuckTime) {
          if (this._actionInFlight) {
            if ((this._actionInFlight === "select_blind" || this._actionInFlight === "skip_blind") && curStateName === "BLIND_SELECT") {
              // Waiting for blind transition animation, keep in-flight lock active and refresh progress timestamp
              this._lastProgressTimestamp = now;
            } else {
              console.warn(`[AutoPilot Watchdog] Action ${this._actionInFlight} stuck for ${now - this._lastProgressTimestamp}ms, force-clearing in-flight lock`);
              this._actionInFlight = null;
            }
          }
          this.busy = false;
        }

        if (this.busy) return;
        const state = currentGameState || window._lastDecidexState;
        if (!state || !state.state_name) return;
        const sname = state.state_name;

        // Animation states: do NOT nudge actions during animation
        if (["HAND_PLAYED", "DRAW_TO_HAND", "NEW_ROUND"].includes(sname)) {
          return;
        }

        // In actionable states, if idle, nudge execution immediately
        if (["SPLASH", "MENU", "BLIND_SELECT", "SELECTING_HAND", "ROUND_EVAL", "SHOP", "TAROT_PACK", "PLANET_PACK", "SPECTRAL_PACK", "STANDARD_PACK", "BUFFOON_PACK"].includes(sname)) {
          this.executeStep();
        }
      }, 350);
    },

    stopWatchdog() {
      if (this.watchdogTimer) {
        clearInterval(this.watchdogTimer);
        this.watchdogTimer = null;
      }
    },

    updateStatus(text, isGood = false) {
      const badge = document.getElementById("autopilot-status");
      if (badge) {
        badge.textContent = text;
        badge.className = `vz-badge ${isGood ? "vz-good" : "vz-warn"}`;
      }
    },

    getDelayForState(sname) {
      if (sname === "SPLASH" || sname === "MENU") return 150;
      if (sname === "BLIND_SELECT") return 150;
      if (sname === "ROUND_EVAL") return 180;
      if (sname === "SHOP") return 220;
      if (sname === "SELECTING_HAND") return 350;
      if (["TAROT_PACK", "PLANET_PACK", "SPECTRAL_PACK", "STANDARD_PACK", "BUFFOON_PACK"].includes(sname)) return 300;
      return 250;
    },

    onActionDispatch(action, sequence) {
      if (!this.enabled) return;
      this._actionInFlight = action;
      this._actionSequence = sequence;
      this._busyTimestamp = Date.now();
    },

    onActionResult(resultStr) {
      if (!this.enabled) return;
      const mAction = resultStr.match(/action=([^\s]+)/);
      const mOk = resultStr.match(/ok=([^\s]+)/);
      const mInfo = resultStr.match(/info=(.*)$/);
      const action = mAction ? mAction[1] : null;
      const ok = mOk ? mOk[1] === "true" : false;
      const info = mInfo ? mInfo[1].trim() : "";

      if (action) {
        if (!ok) {
          // The sequenced ack/timeout path owns failure recovery. Keeping this
          // log-only avoids counting the same failed action twice.
          console.warn(`[AutoPilot] Action ${action} rejected by Lua: ${info}`);
        } else {
          console.log(`[AutoPilot] Action ${action} confirmed by Lua: ${info}`);
          this._actionFailureStreak = 0;
          this._lastProgressTimestamp = Date.now();
        }
      }
    },

    onActionDispatchFailure(action, error, sequence) {
      if (!this.enabled) return;
      const current = this._actionInFlight;
      if (current && current !== action) return;

      const errMsg = error ? String(error.message || error) : "";
      if (
        errMsg.includes("already selected") ||
        errMsg.includes("already in run")
      ) {
        console.log(`[AutoPilot] Benign notice for ${action}: ${errMsg}; not counting as failure streak.`);
        this._actionInFlight = null;
        this._busyTimestamp = null;
        this.pendingState = null;
        this.busy = false;
        return;
      }

      // When tab is hidden/backgrounded, Chrome pauses RAF/game loop; do not count this as an engine failure!
      if (typeof document !== "undefined" && document.hidden) {
        console.warn(`[AutoPilot] Dispatch timeout for ${action} while tab hidden in background; ignoring failure count`);
        this._actionInFlight = null;
        this._busyTimestamp = null;
        this.pendingState = null;
        this.busy = false;
        return;
      }

      const errStr = String(error || "");
      const isTransient = /not ready|animating|debounced|in progress|busy|locked|waiting|already in play/i.test(errStr);
      if (isTransient) {
        console.log(`[AutoPilot] Dispatch transient wait for ${action}: ${errStr}`);
        if (action === "select_blind" || action === "skip_blind") {
          this._actionInFlight = null;
          this._blindActionSent = false;
        } else if (action !== "play" && action !== "discard") {
          this._actionInFlight = null;
        }
        this._busyTimestamp = null;
        this.pendingState = null;
        this.busy = false;
        this.updateStatus("界面过渡中，等待就绪...", true);
        this.scheduleNextAction(600);
        return;
      }

      this._actionFailureStreak = (this._actionFailureStreak || 0) + 1;
      this._actionInFlight = null;
      this._busyTimestamp = null;
      this.pendingState = null;
      this.busy = false;
      this._lastProgressTimestamp = Date.now();
      console.warn(`[AutoPilot] Dispatch failed for ${action}${sequence ? ` #${sequence}` : ""}; cleared action lock`, error);

      if (this._actionFailureStreak >= 12) {
        this.updateStatus("动作连续失败，自动托管已暂停", false);
        this.toggle(false);
        return;
      }
      this.updateStatus("动作失败，正在重试...", false);
      this.scheduleNextAction(600);
    },

    onStateUpdated(state) {
      if (!state) return;
      const sname = state.state_name;
      const now = Date.now();
      const handsLeft = Number(state.hands_left);
      const discardsLeft = Number(state.discards_left);
      const handLen = Array.isArray(state.hand) ? state.hand.length : 0;
      const money = Number(state.money);

      const stateChanged = Boolean(sname && sname !== this._lastSeenState);
      if (stateChanged) {
        this._actionFailureStreak = 0;
      }
      if (!this.enabled) return;

      // Play lifecycle tracking: Must witness HAND_PLAYED before considering play completed
      if (this._actionInFlight === "play") {
        if (sname === "HAND_PLAYED") {
          this._playSawHandPlayed = true;
        } else if (this._playSawHandPlayed && (sname === "SELECTING_HAND" || sname === "ROUND_EVAL" || sname === "GAME_OVER" || sname === "GAME_WON")) {
          // Play lifecycle completed cleanly!
          this._actionInFlight = null;
          this._playSawHandPlayed = false;
          this.busy = false;
        } else if (now - (this._lastPlayActionTime || 0) > 15000) {
          // Hard watchdog fallback (15s)
          this._actionInFlight = null;
          this._playSawHandPlayed = false;
          this.busy = false;
        }
      }

      // Discard lifecycle tracking
      if (this._actionInFlight === "discard") {
        if (sname === "DRAW_TO_HAND") {
          this._discardSawDraw = true;
        } else if (this._discardSawDraw && sname === "SELECTING_HAND") {
          const expected = Math.min(Number(state.hand_size || 8), (state.hand ? state.hand.length : 0) + Number(state.deck_count || 0));
          const currentHandLen = state.hand ? state.hand.length : 0;
          if (currentHandLen >= expected || now - (this._lastDiscardActionTime || 0) > 3000) {
            this._actionInFlight = null;
            this._discardSawDraw = false;
            this.busy = false;
          }
        } else if (now - (this._lastDiscardActionTime || 0) > 7000) {
          this._actionInFlight = null;
          this._discardSawDraw = false;
          this.busy = false;
        }
      }

      const actionMadeProgress = (
        (this._lastHandsLeft !== undefined && handsLeft !== this._lastHandsLeft) ||
        (this._lastDiscardsLeft !== undefined && discardsLeft !== this._lastDiscardsLeft) ||
        (this._lastHandLen !== undefined && handLen !== this._lastHandLen && this._actionInFlight && this._actionInFlight !== "play" && this._actionInFlight !== "discard")
      );

      // When the state changes or progress is made, unlock if NOT in combat animation lifecycle
      if (stateChanged || actionMadeProgress) {
        if (this._actionInFlight !== "play" && this._actionInFlight !== "discard") {
          this.busy = false;
          this._actionInFlight = null;
        }
        this.pendingState = null;
        this._actionFailureStreak = 0;
        this._lastHandsLeft = handsLeft;
        this._lastDiscardsLeft = discardsLeft;
        this._lastHandLen = handLen;
        this._lastProgressTimestamp = now;
        this.scheduleNextAction(this.getDelayForState(sname));
        return;
      }

      this._lastHandsLeft = handsLeft;
      this._lastDiscardsLeft = discardsLeft;
      this._lastHandLen = handLen;

      if (this.busy) {
        this.pendingState = state;
        return;
      }
      this.scheduleNextAction(this.getDelayForState(sname));
    },

    scheduleNextAction(delayMs) {
      if (!this.enabled || this.busy) return;
      if (this.stepTimeout) clearTimeout(this.stepTimeout);
      this.stepTimeout = setTimeout(() => {
        this.stepTimeout = null;
        this.executeStep();
      }, delayMs);
    },

    async executeStep() {
      if (!this.enabled || this.busy) return;
      const state = currentGameState || window._lastDecidexState;
      if (!state || !state.state_name) return;

      const sname = state.state_name;
      let executedAction = false;
      let unlockCooldownMs = 300;

      // Ignore transient UNKNOWN states (during screen wipe / UI destruction) so we don't clear action locks or reset dwell times
      if (!sname || sname === "UNKNOWN") {
        this.updateStatus("游戏状态加载中...", false);
        this.busy = false;
        this._busyTimestamp = null;
        this.pendingState = null;
        this.scheduleNextAction(400);
        return;
      }

      // Track state dwell time and reset sub-state clocks on transition
      const now = Date.now();
      if (sname !== this._lastSeenState) {
        this._lastSeenState = sname;
        this._stateEnterTime = now;
        this._lastProgressTimestamp = now;
        if (sname !== "BLIND_SELECT") {
          this._blindSelectEnterTime = null;
          this._blindActionSent = false;
        }
        if (sname !== "ROUND_EVAL") this._roundEvalEnterTime = null;
        if (sname !== "SHOP") {
          this._shopEnterTime = null;
          this._shopPurchasedCard = false;
          this._shopPurchasedBooster = false;
          this._shopAttemptedItems.clear();
        }
      }

      this.busy = true;
      this._busyTimestamp = now;
      try {
        console.log(`[AutoPilot] Step executing for state: ${sname}`);

        if (sname === "SPLASH" || sname === "MENU") {
          if (now - this.lastStartRunTime > 6000) {
            this.lastStartRunTime = now;
            this.updateStatus("正在启动新局...", true);
            console.log(`[AutoPilot] In ${sname}, auto starting new run`);
            window.decidexStartRun();
            executedAction = true;
            unlockCooldownMs = 2000;
          } else {
            this.updateStatus("等待新局加载...", true);
            executedAction = false;
            unlockCooldownMs = 400;
          }
        } else if (sname === "BLIND_SELECT") {
          this.lastToggleShopTime = 0;
          this.lastCashOutTime = 0;
          this._shopEnterTime = null;
          this._togglingShop = false;
          if (!this._blindSelectEnterTime) {
            this._blindSelectEnterTime = now;
            this._blindActionSent = false;
          }
          const timeInBlindSelect = now - this._blindSelectEnterTime;
          const timeSinceBlindAction = now - (this.lastBlindSelectTime || 0);

          // If blind action was sent, but we've been stuck in BLIND_SELECT for > 4.5s, clear the locks to self-heal
          if (this._blindActionSent && timeSinceBlindAction > 4500) {
            console.warn(`[AutoPilot] Blind action sent ${timeSinceBlindAction}ms ago but still in BLIND_SELECT, clearing lock to retry`);
            this._blindActionSent = false;
            this._actionInFlight = null;
          }

          if (this._blindActionSent || this._actionInFlight === "select_blind" || this._actionInFlight === "skip_blind") {
            this.updateStatus("等待盲注转场动画...", true);
            executedAction = false;
            unlockCooldownMs = 300;
          } else {
            // Priority 0: Use non-combat consumables (Planets, Hermit, Temperance, Wheel) before entering blind
            const nonCombatCons = findBestNonCombatConsumableToUse(state);
            if (nonCombatCons && (!this._lastConsumableTime || now - this._lastConsumableTime > 1200)) {
              this._lastConsumableTime = now;
              this._lastProgressTimestamp = now;
              this.updateStatus(`盲注前使用消耗牌: ${nonCombatCons.card.name}`, true);
              console.log("[AutoPilot] Blind select auto-using consumable:", nonCombatCons.card.name, nonCombatCons.reason);
              window.executeUseCard(nonCombatCons.index, null, "consumeables");
              executedAction = true;
              unlockCooldownMs = 600;
            } else if (state.can_select_blind) {
              if (now - (this.lastBlindSelectTime || 0) > 1000) {
                this.lastBlindSelectTime = now;
                this._lastProgressTimestamp = now;
                this._blindActionSent = true;
                const choice = evaluateBlindChoice(state);
                if (choice.action === "skip_blind" && state.can_skip_blind) {
                  this._actionInFlight = "skip_blind";
                  this.updateStatus(`跳过盲注获取 [${choice.tagName || 'Tag'}]...`, true);
                  console.log(`[AutoPilot] In BLIND_SELECT, auto skipping blind for ${choice.tagName} (skipEv=${choice.skipEv} > selectEv=${choice.selectEv}, time=${timeInBlindSelect}ms)`);
                  window.decidexSkipBlind();
                } else {
                  this._actionInFlight = "select_blind";
                  this.updateStatus("自动选择盲注...", true);
                  console.log(`[AutoPilot] In BLIND_SELECT, auto selecting blind (can_select_blind=true, time=${timeInBlindSelect}ms)`);
                  window.decidexSelectBlind();
                }
                executedAction = true;
                unlockCooldownMs = 1500;
              } else {
                this.updateStatus("盲注确认中...", true);
                executedAction = false;
                unlockCooldownMs = 200;
              }
            } else if (timeInBlindSelect > 3500 && !this._blindActionSent && !this._actionInFlight) {
              // Watchdog fallback after 3.5s of being stuck in BLIND_SELECT without can_select_blind (only if action was not yet sent)
              if (now - (this.lastBlindSelectTime || 0) > 2000) {
                this.lastBlindSelectTime = now;
                this._lastProgressTimestamp = now;
                this._actionInFlight = "select_blind";
                this._blindActionSent = true;
                this.updateStatus("超时保底选择盲注...", true);
                console.log(`[AutoPilot] In BLIND_SELECT, watchdog fallback selecting blind (time=${timeInBlindSelect}ms)`);
                window.decidexSelectBlind();
                executedAction = true;
                unlockCooldownMs = 1500;
              } else {
                this.updateStatus("盲注确认中...", true);
                executedAction = false;
                unlockCooldownMs = 200;
              }
            } else {
              this.updateStatus("等待盲注界面就绪...", false);
              executedAction = false;
              unlockCooldownMs = 250;
            }
          }
        } else if (sname === "SELECTING_HAND") {
          this.lastToggleShopTime = 0;
          this.lastCashOutTime = 0;
          this._shopEnterTime = null;
          this._togglingShop = false;
          if (!state.hand || state.hand.length === 0) {
            this.updateStatus("等待发牌...", false);
            executedAction = false;
            unlockCooldownMs = 200;
          } else if (Number(state.hands_left) <= 0) {
            this.updateStatus("出牌次数已耗尽，等待盲注结算...", false);
            executedAction = false;
            unlockCooldownMs = 500;
          } else if (Number(state.deck_count || 0) > 0 && state.hand.length < Math.min(Number(state.hand_size || 8), state.hand.length + Number(state.deck_count || 0)) && (!this._handIncompleteSince || now - this._handIncompleteSince < 2800)) {
            const expected = Math.min(Number(state.hand_size || 8), state.hand.length + Number(state.deck_count || 0));
            if (!this._handIncompleteSince) {
              this._handIncompleteSince = now;
            }
            this.updateStatus(`等待手牌发全 (${state.hand.length}/${expected})...`, true);
            executedAction = false;
            unlockCooldownMs = 300;
          } else if (this._actionInFlight === "play" && (now - (this._lastPlayActionTime || 0) < 15000)) {
            this._handIncompleteSince = null;
            // A play was dispatched and is animating; wait up to 15.0s
            this.updateStatus("出牌结算中...", true);
            executedAction = false;
            unlockCooldownMs = 400;
          } else if (this._actionInFlight === "discard" && (now - (this._lastDiscardActionTime || 0) < 8000)) {
            this._handIncompleteSince = null;
            // A discard was dispatched and is drawing; wait up to 8.0s
            this.updateStatus("弃牌处理中...", true);
            executedAction = false;
            unlockCooldownMs = 400;
          } else {
            this._handIncompleteSince = null;
            // If in-flight action was not acknowledged after timeout, clear it for retry
            if (this._actionInFlight) {
              console.warn(`[AutoPilot] In-flight action ${this._actionInFlight} timed out in SELECTING_HAND, resetting action lock`);
              this._actionInFlight = null;
              this._playSawHandPlayed = false;
              this._discardSawDraw = false;
            }

            // Priority 1: Check if jokers need reordering according to left-to-right math rule
            const orderAdv = computeOptimalJokerOrder(state.jokers);
            const now = Date.now();
            if (orderAdv && orderAdv.needsReorder && (!this._lastReorderTime || now - this._lastReorderTime > 1200)) {
              this._lastReorderTime = now;
              this._lastProgressTimestamp = now;
              this.updateStatus("优化小丑排布顺序...", true);
              console.log("[AutoPilot] Auto-reordering jokers:", orderAdv.reason, orderAdv.optimalIndices);
              window.executeReorderJokers(orderAdv.optimalIndices);
              executedAction = true;
              unlockCooldownMs = 400;
            } else {
              // Priority 2: Check if beneficial consumables can be used before playing/discarding
              const bestCons = findBestConsumableToUse(state);
              if (bestCons && (!this._lastConsumableTime || now - this._lastConsumableTime > 1200)) {
                this._lastConsumableTime = now;
                this._lastProgressTimestamp = now;
                this.updateStatus(`使用消耗牌: ${bestCons.card.name}`, true);
                console.log("[AutoPilot] Auto-using consumable:", bestCons.card.name, bestCons.reason);
                window.executeUseCard(bestCons.index, bestCons.targetIndices || null, "consumeables");
                executedAction = true;
                unlockCooldownMs = 500;
              } else {
                const topPlays = findBestPlays(state);
                if (topPlays && topPlays.length > 0) {
                  const bestPlay = topPlays[0];
                  const bestDiscard = findBestDiscards(state, bestPlay);
                  const arb = arbitrateAction(state, bestPlay, bestDiscard);

                  if (arb && arb.verdict === "DISCARD" && arb.cards && arb.cards.length > 0) {
                    if (now - (this._lastDiscardActionTime || 0) > 2500) {
                      this._lastDiscardActionTime = now;
                      this._lastProgressTimestamp = now;
                      this._actionInFlight = "discard";
                      this._discardSawDraw = false;
                      this.updateStatus(`自动弃牌 (${arb.cards.length}张)...`, true);
                      console.log("[AutoPilot] Executing strategic discard:", arb.reason);
                      window.executeDiscardAction(arb.cards);
                      executedAction = true;
                      unlockCooldownMs = 1600;
                    } else {
                      this.updateStatus("弃牌冷却中...", true);
                      executedAction = false;
                      unlockCooldownMs = 350;
                    }
                  } else if (arb && arb.cards && arb.cards.length > 0) {
                    if (now - (this._lastPlayActionTime || 0) > 3000) {
                      this._lastPlayActionTime = now;
                      this._lastProgressTimestamp = now;
                      this._actionInFlight = "play";
                      this._playSawHandPlayed = false;
                      this.updateStatus(`自动出牌 (${bestPlay.handName})...`, true);
                      console.log("[AutoPilot] Executing play hand:", bestPlay.handName, arb.reason);
                      window.executePlayAction(arb.cards);
                      executedAction = true;
                      unlockCooldownMs = 2500;
                    } else {
                      this.updateStatus("出牌冷却中...", true);
                      executedAction = false;
                      unlockCooldownMs = 300;
                    }
                  }
                }
              }
            }
          }
        } else if (sname === "ROUND_EVAL") {
          this._shopEnterTime = null;
          this._togglingShop = false;
          if (!this._roundEvalEnterTime) this._roundEvalEnterTime = now;
          const timeInEval = now - this._roundEvalEnterTime;

          if (now - (this.lastCashOutTime || 0) < 1200) {
            this.updateStatus("正在进入商店...", true);
            executedAction = false;
            unlockCooldownMs = 300;
          } else if (state.can_cash_out || timeInEval > 5000) {
            this.lastCashOutTime = now;
            this._lastProgressTimestamp = now;
            this.updateStatus("通关结算，前往商店...", true);
            console.log(`[AutoPilot] Auto cash out (can_cash_out=${state.can_cash_out}, time=${timeInEval}ms)`);
            window.decidexCashOut();
            executedAction = true;
            unlockCooldownMs = 800;
          } else {
            this.updateStatus("清算通关奖励与利息中...", true);
            executedAction = false;
            unlockCooldownMs = 300;
          }
        } else if (sname === "SHOP") {
          this.lastCashOutTime = 0;
          this._roundEvalEnterTime = 0;
          this._blindSelectEnterTime = null;
          if (this._togglingShop) {
            if (now - (this._togglingShopStart || 0) < 3500) {
              this.updateStatus("正在进入下轮盲注...", true);
              executedAction = false;
              unlockCooldownMs = 300;
              return;
            } else {
              this._togglingShop = false;
            }
          }
          const shop = state.shop || {};
          const cards = shop.cards || [];
          const boosters = shop.boosters || [];
          if (!this._shopEnterTime) this._shopEnterTime = now;
          const timeInShop = now - this._shopEnterTime;

          // If shop items haven't arrived yet and we just entered, wait for shelf rendering (up to 1500ms)
          if (cards.length === 0 && boosters.length === 0 && timeInShop < 1500) {
            this.updateStatus("等待商店上架商品...", false);
            console.log("[AutoPilot] Shop waiting for shelf population, elapsed:", timeInShop);
            executedAction = false;
            unlockCooldownMs = 200;
          } else if (state.can_toggle_shop || timeInShop > 2500) {
            const shopAdv = findBestShopPurchases(state);
            const bestCard = shopAdv && shopAdv.bestCard;
            const bestBooster = shopAdv && shopAdv.bestBooster;
            const bestVoucher = shopAdv && shopAdv.bestVoucher;
            const sellRec = shopAdv && shopAdv.sellRecommendation;
            const jokersCount = (state.jokers || []).length;
            const shouldBuyCard = bestCard && bestCard.canAfford && bestCard.hasSlot && (
              bestCard.recommendation === "强烈推荐" ||
              (jokersCount < 2 && bestCard.score >= 45) ||
              (jokersCount === 0 && bestCard.score >= 35) ||
              (bestCard.isConsumable && bestCard.score >= 60)
            );

            const cardKey = bestCard ? `card_${bestCard.index}_${bestCard.name}` : null;
            const voucherKey = bestVoucher ? `voucher_${bestVoucher.index}_${bestVoucher.name}` : null;

            // Priority 0: Use non-combat consumables (Planets, Hermit, Temperance, Wheel, Judgement)
            // to gain cash, free consumable slots for buying, or level up hands!
            const nonCombatCons = findBestNonCombatConsumableToUse(state);
            if (nonCombatCons && (!this._lastConsumableTime || now - this._lastConsumableTime > 1200)) {
              this._lastConsumableTime = now;
              this._lastProgressTimestamp = now;
              this.updateStatus(`商店使用消耗牌: ${nonCombatCons.card.name}`, true);
              console.log("[AutoPilot] Shop auto-using consumable:", nonCombatCons.card.name, nonCombatCons.reason);
              window.executeUseCard(nonCombatCons.index, null, "consumeables");
              executedAction = true;
              unlockCooldownMs = 600;
            } else if (sellRec && !this._shopSoldJoker) {
              this._shopSoldJoker = true;
              this._lastProgressTimestamp = now;
              this.updateStatus(`出售小丑: ${sellRec.sellName} (腾出槽位购买 ${sellRec.targetCard.name})`, true);
              console.log("[AutoPilot] Auto sell joker:", sellRec.sellName, "at index", sellRec.sellIndex, "score gain:", sellRec.scoreGain);
              window.executeSellCard(sellRec.sellIndex, "jokers");
              executedAction = true;
              unlockCooldownMs = 600;
            } else if (bestBooster && (state.ante || 1) <= 2 && jokersCount < 2 && (bestBooster.name || "").includes("Buffoon") && !this._shopAttemptedItems.has(`booster_${bestBooster.index}`)) {
              // Priority 1: In early game with 0/1 joker, buying Buffoon pack is HIGHEST survival priority!
              const bKey = `booster_${bestBooster.index}`;
              this._shopAttemptedItems.add(bKey);
              this._lastProgressTimestamp = now;
              this.updateStatus(`开局紧急采购小丑包: ${bestBooster.name}`, true);
              console.log("[AutoPilot] Auto buy early buffoon pack for survival:", bestBooster.name, "at index", bestBooster.index);
              window.executeBuyAction(bestBooster.index, "shop_booster");
              executedAction = true;
              unlockCooldownMs = 600;
            } else if (shouldBuyCard && cardKey && !this._shopAttemptedItems.has(cardKey)) {
              // Priority 2: Buy recommended card (Joker or Consumable)
              this._shopAttemptedItems.add(cardKey);
              this._lastProgressTimestamp = now;
              const cardLabel = bestCard.isConsumable ? `购买消耗牌: ${bestCard.name}` : `购买小丑: ${bestCard.name}`;
              this.updateStatus(cardLabel, true);
              console.log("[AutoPilot] Auto buy card:", bestCard.name, "isConsumable:", bestCard.isConsumable, "score:", bestCard.score);
              window.executeBuyAction(bestCard.index, "shop_jokers");
              executedAction = true;
              unlockCooldownMs = 600;
            } else if (bestBooster && !this._shopAttemptedItems.has(`booster_${bestBooster.index}`)) {
              // Priority 3: Buy affordable booster pack
              const bKey = `booster_${bestBooster.index}`;
              this._shopAttemptedItems.add(bKey);
              this._lastProgressTimestamp = now;
              this.updateStatus(`购买补充包: ${bestBooster.name || '补充包'}`, true);
              console.log("[AutoPilot] Auto buy booster pack:", bestBooster.name, "at index", bestBooster.index);
              window.executeBuyAction(bestBooster.index, "shop_booster");
              executedAction = true;
              unlockCooldownMs = 600;
            } else if (bestVoucher && voucherKey && !this._shopAttemptedItems.has(voucherKey)) {
              // Priority 4: Buy recommended voucher
              this._shopAttemptedItems.add(voucherKey);
              this._lastProgressTimestamp = now;
              this.updateStatus(`购买优惠券: ${bestVoucher.name}`, true);
              console.log("[AutoPilot] Auto buy voucher:", bestVoucher.name, "at index", bestVoucher.index, "score:", bestVoucher.score);
              window.executeBuyAction(bestVoucher.index, "shop_vouchers");
              executedAction = true;
              unlockCooldownMs = 600;
            } else if (shopAdv && shopAdv.shouldReroll && !this._shopRerolled) {
              // Priority 5: Reroll shop if cash is abundant or genuine emergency
              this._shopRerolled = true;
              this._lastProgressTimestamp = now;
              this.updateStatus(`刷新商店 ($${shopAdv.rerollCost})...`, true);
              console.log("[AutoPilot] Auto reroll shop:", "cost=", shopAdv.rerollCost);
              window.executeRerollShop();
              executedAction = true;
              unlockCooldownMs = 600;
            } else {
              if (this._togglingShop || (now - (this.lastToggleShopTime || 0) < 2500)) {
                this.updateStatus("正在进入下轮盲注...", true);
                executedAction = false;
                unlockCooldownMs = 300;
              } else {
                this._togglingShop = true;
                this._togglingShopStart = now;
                this.lastToggleShopTime = now;
                this._lastProgressTimestamp = now;
                this._shopSoldJoker = false;
                this._shopAttemptedItems.clear();
                this._shopRerolled = false;
                this.updateStatus("离开商店进入下轮...", true);
                console.log(`[AutoPilot] Auto toggle shop (can_toggle_shop=${state.can_toggle_shop}, time=${timeInShop}ms)`);
                window.executeToggleShop();
                executedAction = true;
                unlockCooldownMs = 1000;
              }
            }
          } else {
            this.updateStatus("等待商店就绪...", false);
            console.log("[AutoPilot] Shop waiting for can_toggle_shop to become true, elapsed:", timeInShop);
            executedAction = false;
            unlockCooldownMs = 150;
          }
        } else if (["TAROT_PACK", "PLANET_PACK", "SPECTRAL_PACK", "STANDARD_PACK", "BUFFOON_PACK"].includes(sname)) {
          const packCards = state.pack_cards || [];
          if (packCards.length > 0) {
            this._packEnterTime = null;
            let bestCard = null;
            if (sname === "PLANET_PACK") {
              const priorityPlanets = ["Jupiter", "Earth", "Saturn", "Mercury", "Uranus", "Mars", "Venus", "Pluto", "Planet X", "Ceres", "Eris"];
              for (const p of priorityPlanets) {
                const found = packCards.find(c => c.name === p);
                if (found && canUsePackCard(found, state)) { bestCard = found; break; }
              }
              if (!bestCard) bestCard = packCards.find(c => canUsePackCard(c, state)) || packCards[0];
            } else if (sname === "BUFFOON_PACK") {
              const syn = analyzeSynergy(state.jokers);
              const jCount = (state.jokers || []).length;
              const jMax = Number(state.joker_slots) || 5;

              // If joker slots are full, check if we should sell a weak joker to acquire an S-tier joker
              if (jCount >= jMax && !this._shopSoldJoker) {
                let worstJoker = null;
                let worstScore = 999;
                (state.jokers || []).forEach((j, idx) => {
                  const p = JOKER_PILLAR_MAP[j.name] || "utility";
                  const score = (p === syn.primaryNeed ? 30 : 15);
                  if (score < worstScore) { worstScore = score; worstJoker = { joker: j, index: idx + 1 }; }
                });
                let bestPackJoker = null;
                let bestPackScore = -999;
                for (const c of packCards) {
                  const p = JOKER_PILLAR_MAP[c.name] || "utility";
                  let score = (p === syn.primaryNeed ? 50 : 20);
                  if (score > bestPackScore) { bestPackScore = score; bestPackJoker = c; }
                }
                if (worstJoker && bestPackJoker && bestPackScore >= 45 && worstScore <= 15) {
                  this._shopSoldJoker = true;
                  this._lastProgressTimestamp = now;
                  this.updateStatus(`补充包换牌出售: ${worstJoker.joker.name}`, true);
                  console.log("[AutoPilot] Selling weak joker in buffoon pack:", worstJoker.joker.name);
                  window.executeSellCard(worstJoker.index, "jokers");
                  executedAction = true;
                  unlockCooldownMs = 600;
                  return;
                }
              }

              let bestScore = -999;
              for (const c of packCards) {
                if (!canUsePackCard(c, state)) continue;
                const cname = c.name || "";
                const p = JOKER_PILLAR_MAP[cname] || (c.xmult && c.xmult > 1 ? "x_mult" : (c.mult >= 8 ? "flat_mult" : (c.chips >= 30 ? "chips" : "utility")));
                let score = 50;
                if (TOP_TIER.includes(cname)) score += 35;
                if (p === syn.primaryNeed) score += 30;
                if (c.xmult && c.xmult > 1.0) score += 40;
                if (c.mult && c.mult >= 8) score += 20;
                if (c.chips && c.chips >= 50) score += 20;
                if (c.edition === "poly") score += 30;
                else if (c.edition === "holo") score += 20;
                else if (c.edition === "foil") score += 15;
                if ((state.ante || 1) <= 2 && jCount < 2) {
                  if (c.mult || c.chips || ["Joker", "Half Joker", "Misprint", "Popcorn", "Ice Cream", "Blue Joker", "Gros Michel", "Abstract Joker"].includes(cname)) {
                    score += 35;
                  }
                }
                if (score > bestScore) { bestScore = score; bestCard = c; }
              }
            } else if (sname === "TAROT_PACK") {
              const priorityTarot = [
                "The Hermit", "The Wheel of Fortune", "The Emperor", "The High Priestess",
                "Judgement", "Death", "The Hanged Man", "The Empress", "The Hierophant",
                "The Chariot", "Justice", "The Devil", "Strength", "The Magician",
                "The Lovers", "The Sun", "The World", "The Moon", "The Star", "The Tower", "The Fool"
              ];
              for (const t of priorityTarot) {
                const found = packCards.find(c => c.name === t);
                if (found && canUsePackCard(found, state)) { bestCard = found; break; }
              }
              if (!bestCard) {
                bestCard = packCards.find(c => canUsePackCard(c, state)) || null;
              }
            } else if (sname === "SPECTRAL_PACK") {
              const jCount = (state.jokers || []).length;
              let bestScore = -999;
              for (const c of packCards) {
                if (!canUsePackCard(c, state)) continue;
                const name = c.name || "";
                let score = 50;
                if (name === "Black Hole") score = 100;
                else if (name === "Immolate") score = 95;
                else if (name === "Cryptid") score = 92;
                else if (name === "DejaVu") score = 90;
                else if (name === "Trance") score = 88;
                else if (name === "Talisman" || name === "Medium") score = 85;
                else if (name === "Aura") score = 82;
                else if (name === "Ectoplasm") score = 80;
                else if (name === "Ankh") {
                  score = (jCount === 1 ? 98 : (jCount === 2 ? 40 : 10)); // 仅当单核小丑时无损质变
                } else if (name === "Hex") {
                  score = (jCount === 1 ? 85 : 10);
                } else if (name === "Wraith") {
                  score = ((state.money || 0) <= 8 ? 85 : 30);
                }
                if (score > bestScore) { bestScore = score; bestCard = c; }
              }
            } else if (sname === "STANDARD_PACK") {
              let bestScore = -999;
              for (const c of packCards) {
                let score = 30;
                if (c.edition === "poly") score += 35;
                else if (c.edition === "holo") score += 25;
                else if (c.edition === "foil") score += 18;

                if (c.seal === "red") score += 30;
                else if (c.seal === "purple" || c.seal === "blue") score += 24;
                else if (c.seal === "gold") score += 20;

                if (c.enhancement === "glass") score += 28;
                else if (c.enhancement === "steel") score += 26;
                else if (c.enhancement === "lucky") score += 20;
                else if (["bonus", "mult", "wild"].includes(c.enhancement)) score += 15;

                if (["A", "K", "Q"].includes(c.rank)) score += 8;
                else if (["2", "3"].includes(c.rank) && !c.edition && !c.seal && !c.enhancement) score -= 15;

                if (score > bestScore) { bestScore = score; bestCard = c; }
              }
            }

            if (bestCard && canUsePackCard(bestCard, state)) {
              let targetIndices = null;
              const targets = selectConsumableTargets(bestCard.name, state.hand, state.jokers);
              if (targets && targets.length > 0) {
                targetIndices = targets;
              }
              const targetDesc = targetIndices ? ` (目标手牌: ${targetIndices.join(', ')})` : '';
              this.updateStatus(`补充包选择: ${bestCard.name || '卡牌'}${targetDesc}`, true);
              console.log("[AutoPilot] Selecting pack card:", bestCard.name, "at index", bestCard.index || 1, "targets:", targetIndices);
              window.executeSelectPackCard(bestCard.index || 1, targetIndices);
              executedAction = true;
              unlockCooldownMs = 600;
            } else if (state.can_skip_booster) {
              this.updateStatus("跳过补充包...", true);
              console.log("[AutoPilot] Skipping booster pack (no usable card)");
              window.executeSkipBooster();
              executedAction = true;
              unlockCooldownMs = 400;
            } else {
              this.updateStatus("等待补充包就绪...", false);
              executedAction = false;
              unlockCooldownMs = 200;
            }
          } else {
            if (!this._packEnterTime) this._packEnterTime = now;
            const timeInPack = now - this._packEnterTime;
            if (timeInPack < 3500) {
              this.updateStatus("等待补充包卡牌生成...", true);
              executedAction = false;
              unlockCooldownMs = 350;
            } else if (state.can_skip_booster) {
              this.updateStatus("补充包超时无卡，跳过...", true);
              console.log("[AutoPilot] Skipping empty booster pack after timeout");
              window.executeSkipBooster();
              executedAction = true;
              unlockCooldownMs = 600;
            } else {
              this.updateStatus("等待补充包卡牌...", false);
              executedAction = false;
              unlockCooldownMs = 250;
            }
          }
        } else if (sname === "GAME_WON" || state.won) {
          this.updateStatus("🎉 达成通关胜利！5秒后自动继续新对局...", true);
          if (!this._gameWonTimer) {
            this._gameWonTimer = setTimeout(() => {
              this._gameWonTimer = null;
              if (this.enabled) {
                console.log("[AutoPilot] Auto restarting new run after GAME_WON");
                window.decidexStartRun();
              }
            }, 5000);
          }
          executedAction = true;
          unlockCooldownMs = 1000;
        } else if (sname === "GAME_OVER") {
          this.updateStatus("💀 游戏结束，3秒后自动开启全新对局...", true);
          if (!this._gameOverTimer) {
            this._gameOverTimer = setTimeout(() => {
              this._gameOverTimer = null;
              if (this.enabled) {
                console.log("[AutoPilot] Auto restarting new run after GAME_OVER");
                window.decidexStartRun();
              }
            }, 3000);
          }
          executedAction = true;
          unlockCooldownMs = 1000;
        } else if (sname === "HAND_PLAYED") {
          this.updateStatus("计分结算中...", true);
          executedAction = false;
          unlockCooldownMs = 400;
        } else if (sname === "DRAW_TO_HAND") {
          this.updateStatus("发牌动画中...", true);
          executedAction = false;
          unlockCooldownMs = 400;
        } else if (sname === "NEW_ROUND") {
          this._actionInFlight = null;
          this.updateStatus("进入新回合...", true);
          executedAction = false;
          unlockCooldownMs = 400;
        } else {
          this.updateStatus(`等待动画 (${sname})...`, false);
          executedAction = false;
          unlockCooldownMs = 150;
        }
      } catch (e) {
        console.error("[AutoPilot] Error during step:", e);
        executedAction = false;
        unlockCooldownMs = 150;
      } finally {
        // If no action was dispatched, unlock busy immediately so we don't stall
        const delay = executedAction ? unlockCooldownMs : 100;
        setTimeout(() => {
          this.busy = false;
          if (this.enabled) {
            this.updateStatus("运行中", true);
            if (this.pendingState) {
              const nextState = this.pendingState;
              this.pendingState = null;
              this.onStateUpdated(nextState);
            }
          }
        }, delay);
      }
    }
  };

  window.AutoPilot = AutoPilot;
  window.arbitrateAction = arbitrateAction;

  if (typeof document !== "undefined") {
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden && AutoPilot.enabled) {
        AutoPilot._actionFailureStreak = 0;
        AutoPilot.scheduleNextAction(150);
      }
    });
  }

  // Kickoff asset preload
  window.addEventListener("DOMContentLoaded", loadBalatroPackage);
})();
