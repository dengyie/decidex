/**
 * DecideX Balatro WebAssembly Host & Advisor Application
 * Runs Balatro via love.js (LÖVE 11.5) with bi-directional Lua/JS bridge,
 * IndexedDB savegame persistence, and real-time floating AI decision HUD.
 */

(function () {
  "use strict";

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

  function onInitSuccess() {
    if (isLoveRunning) return;
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
      if (!isLoveRunning && splashOverlay && !splashOverlay.classList.contains("hidden")) {
        onInitSuccess();
      }
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
      } else {
        console.log("[LÖVE stdout]", text);
      }
    },
    printErr: function (text) {
      console.warn("[LÖVE stderr]", text);
    },
    prerun: setupFileSystem,
    preRun: [setupFileSystem],
    postrun: onInitSuccess,
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

    // Timeout guard for FS.syncfs to prevent IDBFS hanging indefinitely
    if (FS && FS.syncfs && !FS._syncfs_guarded) {
      FS._syncfs_guarded = true;
      const originalSyncfs = FS.syncfs;
      FS.syncfs = function (populate, callback) {
        let done = false;
        const timer = setTimeout(() => {
          if (!done) {
            done = true;
            console.warn("[DecideX] FS.syncfs timed out after 1000ms, continuing boot.");
            if (callback) callback(null);
          }
        }, 1000);
        originalSyncfs.call(FS, populate, function (err) {
          clearTimeout(timer);
          if (!done) {
            done = true;
            if (callback) callback(err);
          }
        });
      };
    }
  }

  function syncSaveFileSystem() {
    const FS = window.Module.FS;
    if (FS && FS.syncfs) {
      FS.syncfs(false, function (err) {
        if (err) console.warn("[DecideX] IDBFS write sync error:", err);
      });
    }
  }

  // Auto-sync save data periodically
  setInterval(syncSaveFileSystem, 15000);
  window.addEventListener("beforeunload", syncSaveFileSystem);

  const CACHE_NAME = "balatro-asset-cache-v22";
  const ASSET_URL = "balatro.love.zip?v=22";
  const WASM_URL = "love.wasm.bin?v=22";

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
    window.location.reload();
  };
  window.onInitSuccess = onInitSuccess;

  // Asset downloader with progress tracking and CacheStorage persistence
  async function loadBalatroPackage() {
    statusText.textContent = "正在检查本地缓存...";
    progressBar.style.width = "5%";

    try {
      // 1. Check CacheStorage first for both game assets and WASM binary
      if (window.caches) {
        try {
          const cache = await caches.open(CACHE_NAME);
          const [cachedLove, cachedWasm] = await Promise.all([
            cache.match(ASSET_URL),
            cache.match(WASM_URL)
          ]);
          if (cachedLove && cachedWasm) {
            console.log("[DecideX] Found balatro.love and love.wasm in CacheStorage, loading instantly...");
            statusText.textContent = "已从本地离线缓存加载资源，就绪！";
            progressBar.style.width = "100%";
            lovePackageBuffer = await cachedLove.arrayBuffer();
            const wasmBuf = await cachedWasm.arrayBuffer();
            window.Module.cache = window.Module.cache || {};
            window.Module.cache["11.5/love.wasm"] = wasmBuf;
            btnLaunch.style.display = "block";
            progressBar.parentElement.style.display = "none";
            return;
          }
        } catch (cacheErr) {
          console.warn("[DecideX] CacheStorage read error:", cacheErr);
        }
      }

      // 2. Not in cache: stream download balatro.love.zip (0% -> 85%)
      statusText.textContent = "首次下载游戏资源包 (balatro.love 53.7MB)...";
      const response = await fetch(ASSET_URL);
      if (!response.ok) throw new Error("HTTP " + response.status + " fetching " + ASSET_URL);

      const contentLength = response.headers.get("content-length");
      const total = contentLength ? parseInt(contentLength, 10) : 56 * 1024 * 1024;
      let loaded = 0;

      const reader = response.body.getReader();
      const chunks = [];

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        chunks.push(value);
        loaded += value.length;
        const pct = Math.min(85, Math.round((loaded / total) * 85));
        progressBar.style.width = pct + "%";
        statusText.textContent = `下载游戏包 (1/2): ${(loaded / (1024 * 1024)).toFixed(1)} MB / ${(total / (1024 * 1024)).toFixed(1)} MB (${pct}%)`;
      }

      const fullBuffer = new Uint8Array(loaded);
      let offset = 0;
      for (const chunk of chunks) {
        fullBuffer.set(chunk, offset);
        offset += chunk.length;
      }
      lovePackageBuffer = fullBuffer.buffer;

      // 3. Pre-fetch love.wasm.bin (85% -> 98%) so launch never has network latency
      statusText.textContent = "正在下载 WebAssembly 运行核心 (love.wasm 4.7MB)...";
      progressBar.style.width = "88%";
      const wasmRes = await fetch(WASM_URL);
      if (!wasmRes.ok) throw new Error("HTTP " + wasmRes.status + " fetching " + WASM_URL);

      const wasmBuf = await wasmRes.arrayBuffer();
      window.Module.cache = window.Module.cache || {};
      window.Module.cache["11.5/love.wasm"] = wasmBuf;

      // 4. Atomically persist both completed memory buffers into CacheStorage
      if (window.caches) {
        statusText.textContent = "正在写入本地持久离线缓存 (下次启动免下载)...";
        progressBar.style.width = "99%";
        try {
          const cache = await caches.open(CACHE_NAME);
          await Promise.all([
            cache.put(ASSET_URL, new Response(lovePackageBuffer.slice(0), {
              headers: {
                "Content-Type": "application/octet-stream",
                "Content-Length": String(lovePackageBuffer.byteLength),
                "Cache-Control": "public, max-age=31536000"
              }
            })),
            cache.put(WASM_URL, new Response(wasmBuf.slice(0), {
              headers: {
                "Content-Type": "application/wasm",
                "Content-Length": String(wasmBuf.byteLength),
                "Cache-Control": "public, max-age=31536000"
              }
            }))
          ]);
          console.log("[DecideX] Successfully persisted both assets to CacheStorage!");
        } catch (cacheWriteErr) {
          console.warn("[DecideX] CacheStorage write error:", cacheWriteErr);
        }
      }

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
        script.src = "love.js?v=22";
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
      if (window.Module && window.Module.asm && (window._lastDecidexState || bootElapsed >= 2000)) {
        console.log("[DecideX] Watchdog: WASM runtime ready, revealing game canvas.");
        clearInterval(bootWatchdog);
        onInitSuccess();
      } else if (bootElapsed >= 8000) {
        clearInterval(bootWatchdog);
        statusText.innerHTML = `
          <span style="color:#f59e0b;">启动时间稍长，可能由于设备显卡编译着色器较慢</span><br>
          <button class="btn-util" style="margin-top:10px;padding:6px 14px;background:#3b82f6;color:#fff;border:none;border-radius:4px;cursor:pointer;" onclick="onInitSuccess()">直接进入游戏画布</button>
          <button class="btn-util" style="margin-top:10px;margin-left:8px;padding:6px 14px;background:#ef4444;color:#fff;border:none;border-radius:4px;cursor:pointer;" onclick="decidexClearCacheAndReload()">清除缓存并刷新</button>
        `;
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
    if (!jokers || jokers.length <= 1) return { needsReorder: false, reason: "" };
    let seenXMult = false;
    for (let i = 0; i < jokers.length; i++) {
      const j = jokers[i];
      const p = JOKER_PILLAR_MAP[j.name] || (j.xmult > 1 ? "x_mult" : (j.mult > 0 ? "flat_mult" : "chips"));
      if (p === "x_mult") {
        seenXMult = true;
      } else if (seenXMult && p === "flat_mult") {
        return {
          needsReorder: true,
          reason: `小丑排序倒置！加法倍率 (${j.name}) 位于乘倍小丑右侧，倍率未先加后乘，损失高达 40%+ 最终得分！建议将乘倍小丑挪到最右侧。`
        };
      }
    }
    return { needsReorder: false, reason: "小丑排序符合最佳数学法则 (基础 -> 乘倍)" };
  }

  function detectConstraints(jokers, blind) {
    const hasRideTheBus = (jokers || []).some(j => j.name === "Ride the Bus" && !j.debuff);
    const hasGreenJoker = (jokers || []).some(j => j.name === "Green Joker" && !j.debuff);
    const isNeedle = blind && (blind.name === "The Needle");
    const isWater = blind && (blind.name === "The Water");
    const isEye = blind && (blind.name === "The Eye");

    return {
      noFaceCards: hasRideTheBus,
      avoidDiscard: hasGreenJoker || isWater,
      oneHandOnly: isNeedle,
      noRepeatedHands: isEye
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
        if (uc.enhancement === "steel") {
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
          if (constraints.noFaceCards) {
            const hasScoringFace = evalRes.cards.some(c => ["J", "Q", "K"].includes(c.rank));
            if (hasScoringFace) {
              evalRes.totalScore = Math.floor(evalRes.totalScore * 0.1); // 破戒惩罚 (Ride the Bus 严禁出人头牌)
              evalRes.violation = "Ride the Bus 破戒 (含人头牌)";
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

  // Strategic Discard Analyzer
  function findBestDiscards(state, bestPlay) {
    const hand = state.hand || [];
    const discardsLeft = Number(state.discards_left) || 0;
    if (hand.length === 0 || discardsLeft <= 0) return null;

    const blindGoal = Number(state.blind?.chips || 300);
    const scored = Number(state.blind?.scored || 0);
    const need = Math.max(0, blindGoal - scored);

    // Tally suits and ranks
    const suitMap = {};
    const rankMap = {};
    for (const c of hand) {
      suitMap[c.suit] = (suitMap[c.suit] || []).concat(c);
      rankMap[c.rank] = (rankMap[c.rank] || []).concat(c);
    }

    // 1. Check for 4-flush (draw 1 to make flush)
    for (const [suit, cards] of Object.entries(suitMap)) {
      if (cards.length >= 4 && cards.length < 5) {
        const keepSet = new Set(cards);
        const junk = hand.filter(c => !keepSet.has(c));
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

    // 2. Check for 4-straight (open-ended or gutshot)
    const uniqueRankNums = [...new Set(hand.map(c => RANK_NUM[c.rank] || 0))].sort((a, b) => a - b);
    if (uniqueRankNums.length >= 4) {
      for (let i = 0; i <= uniqueRankNums.length - 4; i++) {
        const sub = uniqueRankNums.slice(i, i + 4);
        const span = sub[3] - sub[0];
        if (span === 3 || span === 4) {
          const straightRanks = new Set(sub);
          const keepCards = hand.filter(c => straightRanks.has(RANK_NUM[c.rank]));
          const junk = hand.filter(c => !keepCards.includes(c));
          const toDiscard = junk.slice(0, 5);
          if (toDiscard.length > 0) {
            return {
              target: "顺子听牌 (4缺1)",
              reason: `保留连点 (${sub.join("-")})，弃掉 ${toDiscard.length} 张边缘散牌以双向抽顺子`,
              cards: toDiscard,
              intent: "straight_draw"
            };
          }
        }
      }
    }

    // 3. Keep pairs/trips, discard uncoordinated low singletons
    const multiCards = [];
    const singleCards = [];
    for (const [rank, cards] of Object.entries(rankMap)) {
      if (cards.length >= 2) multiCards.push(...cards);
      else singleCards.push(...cards);
    }

    if (multiCards.length > 0 && singleCards.length > 0) {
      // Discard lowest singletons
      const toDiscard = singleCards.sort((a, b) => (RANK_VALUES[a.rank] || 0) - (RANK_VALUES[b.rank] || 0)).slice(0, Math.min(5, singleCards.length));
      return {
        target: "对子/三条升级 (博葫芦/四条)",
        reason: `保留成对的核心高分牌，弃掉 ${toDiscard.length} 张低点散牌寻找同点配对`,
        cards: toDiscard,
        intent: "pair_upgrade"
      };
    }

    // 4. Default: Discard lowest 3 uncoordinated cards
    const sortedHand = [...hand].sort((a, b) => (RANK_VALUES[a.rank] || 0) - (RANK_VALUES[b.rank] || 0));
    const toDiscard = sortedHand.slice(0, Math.min(4, sortedHand.length));
    return {
      target: "清理低点底牌",
      reason: `弃除点数最小的 ${toDiscard.length} 张杂牌，刷新手牌深度，等待下一轮高阶牌型`,
      cards: toDiscard,
      intent: "ditch_junk"
    };
  }

  // Stage Decision Arbitration (Play vs Discard)
  function arbitrateAction(state, bestPlay, bestDiscard) {
    if (!bestPlay) return null;

    const blind = state.blind || {};
    const chipsTarget = Number(blind.chips) || 300;
    const chipsScored = Number(blind.scored) || 0;
    const need = Math.max(0, chipsTarget - chipsScored);
    const handsLeft = Number(state.hands_left) || 1;
    const discardsLeft = Number(state.discards_left) || 0;

    // 1. Force Play:
    // - Hand score directly beats remaining blind requirement
    if (bestPlay.totalScore >= need) {
      return {
        verdict: "PLAY",
        confidence: "CERTAIN",
        badgeClass: "arb-play",
        title: "⚡ 推荐立即出牌 (直接通关)",
        reason: `本手预期得分 ${bestPlay.totalScore.toLocaleString()} 分直接超越通关差额 (${need.toLocaleString()} 分)，可直接通关，无需消耗弃牌次数！`,
        cards: bestPlay.cards
      };
    }

    // - No discards left
    if (discardsLeft <= 0) {
      return {
        verdict: "PLAY",
        confidence: "FORCED",
        badgeClass: "arb-play",
        title: "⚡ 必须出牌 (弃牌耗尽)",
        reason: `剩余弃牌次数为 0，必须出牌积累筹码 (本手预计打出 +${bestPlay.totalScore.toLocaleString()} 分)`,
        cards: bestPlay.cards
      };
    }

    // - Last hand left
    if (handsLeft <= 1) {
      return {
        verdict: "PLAY",
        confidence: "CRITICAL",
        badgeClass: "arb-play",
        title: "⚡ 决胜出牌 (最后出牌)",
        reason: `仅剩最后 1 次出牌机会，不可再冒险弃牌，打出最高分组合！`,
        cards: bestPlay.cards
      };
    }

    // 2. High-expected Strategic Discard:
    if (bestDiscard && bestDiscard.cards && bestDiscard.cards.length > 0) {
      if (bestDiscard.intent === "flush_draw") {
        return {
          verdict: "DISCARD",
          confidence: "STRATEGIC",
          badgeClass: "arb-discard",
          title: "🗑️ 战略弃牌 (同花听牌)",
          reason: `手牌已持有 4 张同花，弃除非同花杂牌博取大同花翻盘是胜率最高解！`,
          cards: bestDiscard.cards
        };
      }
      if (bestDiscard.intent === "straight_draw") {
        return {
          verdict: "DISCARD",
          confidence: "STRATEGIC",
          badgeClass: "arb-discard",
          title: "🗑️ 战略弃牌 (顺子听牌)",
          reason: `手牌具有 4 连张顺子胚子，建议战略弃牌寻找成型顺子！`,
          cards: bestDiscard.cards
        };
      }
      if (bestDiscard.intent === "pair_upgrade" && bestPlay.totalScore < need * 0.45) {
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

  // Shop Evaluation System
  function findBestShopPurchases(state) {
    const shop = state.shop || {};
    const cards = shop.cards || [];
    const boosters = shop.boosters || [];
    const money = Number(state.money) || 0;
    const jokersCount = Number(state.jokers_count) || (state.jokers ? state.jokers.length : 0);
    const maxSlots = Number(state.joker_slots) || 5;
    const hasSlot = jokersCount < maxSlots;
    const rerollCost = Number(shop.reroll_cost) || 5;

    const synergy = analyzeSynergy(state.jokers);

    const TOP_TIER = [
      "Cavendish", "Blueprint", "Brainstorm", "Constellation", "Hologram",
      "Card Sharp", "Blackboard", "Baron", "Baseball Card", "Stuntman",
      "Green Joker", "Ride the Bus", "Red Card", "Square Joker", "Supernova",
      "Golden Joker", "Bull", "Bootstraps", "Mail-In Rebate", "Trading Card",
      "Half Joker", "Gros Michel", "Blue Joker", "Banner", "Scary Face", "Odd Todd"
    ];

    const evaluatedCards = [];
    for (const c of cards) {
      let score = 50;
      const cname = c.name || "";
      const cost = Number(c.cost) || 0;
      const canAfford = money >= cost;

      const pillar = JOKER_PILLAR_MAP[cname] || (c.xmult && c.xmult > 1 ? "x_mult" : (c.mult >= 8 ? "flat_mult" : (c.chips >= 30 ? "chips" : "utility")));

      if (TOP_TIER.includes(cname)) score += 35;
      if (pillar === synergy.primaryNeed) score += 30; // 契合四角大厦当前短板加分
      if (c.xmult && c.xmult > 1.0) score += 40;
      if (c.mult && c.mult >= 8) score += 20;
      if (c.chips && c.chips >= 50) score += 20;
      if (c.edition === "poly") score += 30;
      else if (c.edition === "holo") score += 20;
      else if (c.edition === "foil") score += 15;

      // 利息线保护 ($25 黄金利息法则)
      if (canAfford) {
        const remaining = money - cost;
        const curInterest = Math.min(5, Math.floor(money / 5));
        const newInterest = Math.min(5, Math.floor(remaining / 5));
        const interestLoss = curInterest - newInterest;
        if (interestLoss > 0 && !TOP_TIER.includes(cname) && pillar !== synergy.primaryNeed) {
          score -= (interestLoss * 20); // 惩罚跌破利息挡位的平庸小丑采购
        }
      }

      if (!hasSlot) score -= 30;
      if (!canAfford) score -= 50;

      evaluatedCards.push({
        ...c,
        pillar: pillar,
        score: score,
        canAfford: canAfford,
        hasSlot: hasSlot,
        recommendation: score >= 75 ? "强烈推荐" : (score >= 60 ? "值得考虑" : "暂不推荐")
      });
    }

    evaluatedCards.sort((a, b) => b.score - a.score);
    const bestCard = evaluatedCards[0];

    let interestAdvice = "💰 当前资金充足";
    if (money >= 25) {
      interestAdvice = "当前资金已达 $25 吃满每轮 $5 顶额利息，盈余资金可自由采购或刷新";
    } else {
      const nextThreshold = (Math.floor(money / 5) + 1) * 5;
      interestAdvice = `资金 $${money}，差 $${nextThreshold - money} 达 $${nextThreshold} 利息线 (每 $5 结算 $1 利息)`;
    }

    return {
      evaluatedCards: evaluatedCards,
      bestCard: bestCard && bestCard.canAfford && bestCard.score >= 60 ? bestCard : null,
      boosters: boosters,
      interestAdvice: interestAdvice,
      canReroll: money >= rerollCost,
      rerollCost: rerollCost,
      hasSlot: hasSlot,
      synergy: synergy
    };
  }

  // DecideX HUD Renderer
  window.onBalatroStateUpdate = function (state) {
    currentGameState = state;

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
      container.innerHTML = `
        <div class="rec-placeholder">
          <div style="font-weight: bold; margin-bottom: 6px;">🎯 当前阶段: 盲注选择 (Small Blind)</div>
          <button class="btn-act btn-play" style="padding: 6px 14px; margin: 4px auto;" onclick="window.decidexSelectBlind()">▶️ 一键选择小盲注开始</button>
          <div style="font-size: 11px; margin-top: 6px; color: #888;">进入对局后，AI 将自动分析当前手牌的最优出牌与弃牌打法</div>
        </div>`;
      if (candList) candList.innerHTML = `<div style="font-size:11px;color:#888;">盲注选择阶段无需出牌</div>`;
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
    window.currentRecommendation = currentRecommendation;
    window._lastBestPlayCards = currentRecommendation.cards;
    const bestPlay = currentRecommendation;
    const bestDiscard = findBestDiscards(state, bestPlay);
    const arbitration = arbitrateAction(state, bestPlay, bestDiscard);
    window._currentArbitration = arbitration;

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

    // Trigger AutoPilot hook if active
    if (window.AutoPilot && typeof window.AutoPilot.onStateUpdated === "function") {
      window.AutoPilot.onStateUpdated(state);
    }
  };

  // Programmatic execution via Emscripten virtual FS
  window.executePlayAction = function (cards) {
    if (!window.Module || !window.Module.FS) {
      alert("游戏引擎尚未就绪");
      return;
    }
    const indices = (cards || []).map(c => c.index).filter(x => x !== undefined);
    const luaCode = `local fn = (_G.DECIDEX_EXECUTE or (G and G.DECIDEX_EXECUTE)); if fn then fn("play", {indices = {${indices.join(",")}}}) end`;
    window.Module.FS.writeFile("/tmp/balatro_eval.lua", luaCode);
    console.log("[DecideX] Dispatched play action:", luaCode);
  };

  window.executeDiscardAction = function (cards) {
    if (!window.Module || !window.Module.FS) {
      alert("游戏引擎尚未就绪");
      return;
    }
    const indices = (cards || []).map(c => c.index).filter(x => x !== undefined);
    const luaCode = `local fn = (_G.DECIDEX_EXECUTE or (G and G.DECIDEX_EXECUTE)); if fn then fn("discard", {indices = {${indices.join(",")}}}) end`;
    window.Module.FS.writeFile("/tmp/balatro_eval.lua", luaCode);
    console.log("[DecideX] Dispatched discard action:", luaCode);
  };

  window.executeBuyAction = function (index, area) {
    if (!window.Module || !window.Module.FS) {
      alert("游戏引擎尚未就绪");
      return;
    }
    const luaCode = `local fn = (_G.DECIDEX_EXECUTE or (G and G.DECIDEX_EXECUTE)); if fn then fn("buy_card", {index = ${index}, area = "${area || 'shop_jokers'}"}) end`;
    window.Module.FS.writeFile("/tmp/balatro_eval.lua", luaCode);
    console.log("[DecideX] Dispatched buy action:", luaCode);
  };

  window.executeRerollShop = function () {
    if (!window.Module || !window.Module.FS) {
      alert("游戏引擎尚未就绪");
      return;
    }
    const luaCode = `local fn = (_G.DECIDEX_EXECUTE or (G and G.DECIDEX_EXECUTE)); if fn then fn("reroll_shop") end`;
    window.Module.FS.writeFile("/tmp/balatro_eval.lua", luaCode);
    console.log("[DecideX] Dispatched reroll_shop action");
  };

  window.executeToggleShop = function () {
    if (!window.Module || !window.Module.FS) {
      alert("游戏引擎尚未就绪");
      return;
    }
    const luaCode = `local fn = (_G.DECIDEX_EXECUTE or (G and G.DECIDEX_EXECUTE)); if fn then fn("toggle_shop") end`;
    window.Module.FS.writeFile("/tmp/balatro_eval.lua", luaCode);
    console.log("[DecideX] Dispatched toggle_shop action");
  };

  // Utilities
  window.decidexSelectBlind = function () {
    if (window.Module && window.Module.FS) {
      window.Module.FS.writeFile("/tmp/balatro_eval.lua", 'local fn = (_G.DECIDEX_EXECUTE or (G and G.DECIDEX_EXECUTE)); if fn then fn("select_blind") end');
      console.log("[DecideX] Dispatched select_blind action");
    }
  };

  window.decidexCashOut = function () {
    if (window.Module && window.Module.FS) {
      window.Module.FS.writeFile("/tmp/balatro_eval.lua", 'local fn = (_G.DECIDEX_EXECUTE or (G and G.DECIDEX_EXECUTE)); if fn then fn("cash_out") end');
      console.log("[DecideX] Dispatched cash_out action");
    }
  };

  window.decidexSortRank = function () {
    if (window.Module && window.Module.FS) {
      window.Module.FS.writeFile("/tmp/balatro_eval.lua", 'local fn = (_G.DECIDEX_EXECUTE or (G and G.DECIDEX_EXECUTE)); if fn then fn("sort_rank") end');
    }
  };

  window.decidexSortSuit = function () {
    if (window.Module && window.Module.FS) {
      window.Module.FS.writeFile("/tmp/balatro_eval.lua", 'local fn = (_G.DECIDEX_EXECUTE or (G and G.DECIDEX_EXECUTE)); if fn then fn("sort_suit") end');
    }
  };

  window.decidexReEvaluate = function () {
    if (window.Module && window.Module.FS) {
      window.Module.FS.writeFile("/tmp/balatro_eval.lua", 'local fn = (_G.DECIDEX_EXECUTE or (G and G.DECIDEX_EXECUTE)); if fn then fn("force_state_dump") end');
    }
  };

  window.decidexStartRun = function () {
    if (window.Module && window.Module.FS) {
      window.Module.FS.writeFile("/tmp/balatro_eval.lua", 'local fn = (_G.DECIDEX_EXECUTE or (G and G.DECIDEX_EXECUTE)); if fn then fn("start_run") end');
      console.log("[DecideX] Dispatched start_run action");
    }
  };

  // Save Export / Import
  window.decidexExportSave = function () {
    const FS = window.Module && window.Module.FS;
    if (!FS) return;
    try {
      const savePath = "/home/web_user/.local/share/love/Balatro/1/profile.jkr";
      if (!FS.analyzePath(savePath).exists) {
        alert("暂未找到本地存档文件");
        return;
      }
      const data = FS.readFile(savePath);
      const blob = new Blob([data], { type: "application/octet-stream" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "balatro_profile.jkr";
      a.click();
    } catch (e) {
      alert("导出存档失败: " + e.message);
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

    toggle(enable) {
      this.enabled = enable !== undefined ? Boolean(enable) : !this.enabled;
      const chk = document.getElementById("autopilot-toggle");
      if (chk) chk.checked = this.enabled;
      this.updateStatus(this.enabled ? "运行中" : "已暂停", this.enabled);

      console.log(`[AutoPilot] ${this.enabled ? "Enabled" : "Disabled"}`);
      if (this.enabled) {
        this.startWatchdog();
        this.scheduleNextAction(200);
      } else {
        this.stopWatchdog();
        if (this.stepTimeout) {
          clearTimeout(this.stepTimeout);
          this.stepTimeout = null;
        }
        this.busy = false;
        this.pendingState = null;
      }
    },

    startWatchdog() {
      if (this.watchdogTimer) clearInterval(this.watchdogTimer);
      // Fast responsive watchdog (450ms) to ensure zero freeze across states
      this.watchdogTimer = setInterval(() => {
        if (!this.enabled || this.busy) return;
        const state = currentGameState || window._lastDecidexState;
        if (!state || !state.state_name) return;
        const sname = state.state_name;
        // In actionable states, if idle, nudge execution immediately
        if (["SPLASH", "MENU", "BLIND_SELECT", "SELECTING_HAND", "ROUND_EVAL", "SHOP"].includes(sname)) {
          this.executeStep();
        }
      }, 450);
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
      if (sname === "ROUND_EVAL") return 200;
      if (sname === "SHOP") return 250;
      if (sname === "SELECTING_HAND") return 450;
      return 300;
    },

    onStateUpdated(state) {
      if (!this.enabled) return;
      if (this.busy) {
        // State updated while action was in flight; buffer it so we don't drop the transition!
        this.pendingState = state;
        return;
      }
      const sname = state && state.state_name;
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

      this.busy = true;
      try {
        console.log(`[AutoPilot] Step executing for state: ${sname}`);

        if (sname === "SPLASH" || sname === "MENU") {
          this.updateStatus("正在启动新局...", true);
          console.log(`[AutoPilot] In ${sname}, auto starting new run`);
          window.decidexStartRun();
          executedAction = true;
          unlockCooldownMs = 350;
        } else if (sname === "BLIND_SELECT") {
          if (state.can_select_blind) {
            const now = Date.now();
            // Debounce repeated blind selection in the same transition window
            if (now - this.lastBlindSelectTime > 500) {
              this.lastBlindSelectTime = now;
              this.updateStatus("自动选择盲注...", true);
              console.log("[AutoPilot] In BLIND_SELECT, auto selecting blind");
              window.decidexSelectBlind();
              executedAction = true;
              unlockCooldownMs = 400;
            } else {
              this.updateStatus("盲注确认中...", true);
              executedAction = false;
              unlockCooldownMs = 120;
            }
          } else {
            this.updateStatus("等待盲注界面就绪...", false);
            executedAction = false;
            unlockCooldownMs = 100;
            if (window.decidexReEvaluate) {
              window.decidexReEvaluate();
            }
          }
        } else if (sname === "SELECTING_HAND") {
          if (!state.hand || state.hand.length === 0) {
            this.updateStatus("等待发牌...", false);
            executedAction = false;
            unlockCooldownMs = 150;
          } else {
            const topPlays = findBestPlays(state);
            if (topPlays && topPlays.length > 0) {
              const bestPlay = topPlays[0];
              const bestDiscard = findBestDiscards(state, bestPlay);
              const arb = arbitrateAction(state, bestPlay, bestDiscard);

              if (arb && arb.verdict === "DISCARD" && arb.cards && arb.cards.length > 0) {
                this.updateStatus(`自动弃牌 (${arb.cards.length}张)...`, true);
                console.log("[AutoPilot] Executing strategic discard:", arb.reason);
                window.executeDiscardAction(arb.cards);
                executedAction = true;
                unlockCooldownMs = 500;
              } else if (arb && arb.cards && arb.cards.length > 0) {
                this.updateStatus(`自动出牌 (${bestPlay.handName})...`, true);
                console.log("[AutoPilot] Executing play hand:", bestPlay.handName, arb.reason);
                window.executePlayAction(arb.cards);
                executedAction = true;
                unlockCooldownMs = 600;
              }
            }
          }
        } else if (sname === "ROUND_EVAL") {
          if (state.can_cash_out !== false) {
            this.updateStatus("通关结算中...", true);
            console.log("[AutoPilot] Auto cash out");
            window.decidexCashOut();
            executedAction = true;
            unlockCooldownMs = 350;
          } else {
            this.updateStatus("等待结算就绪...", false);
            executedAction = false;
            unlockCooldownMs = 100;
          }
        } else if (sname === "SHOP") {
          if (state.can_toggle_shop !== false) {
            const shopAdv = findBestShopPurchases(state);
            const bestCard = shopAdv && shopAdv.bestCard;
            if (bestCard && bestCard.recommendation === "强烈推荐" && bestCard.canAfford && bestCard.hasSlot) {
              this.updateStatus(`购买小丑: ${bestCard.name}`, true);
              console.log("[AutoPilot] Auto buy top joker:", bestCard.name);
              window.executeBuyAction(bestCard.index, "shop_jokers");
              executedAction = true;
              unlockCooldownMs = 450;
            } else {
              this.updateStatus("离开商店进入下轮...", true);
              console.log("[AutoPilot] Auto toggle shop");
              window.executeToggleShop();
              executedAction = true;
              unlockCooldownMs = 350;
            }
          } else {
            this.updateStatus("等待商店就绪...", false);
            executedAction = false;
            unlockCooldownMs = 150;
          }
        } else if (sname === "GAME_OVER") {
          this.updateStatus("游戏结束", false);
          this.toggle(false);
          executedAction = true;
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

  // Kickoff asset preload
  window.addEventListener("DOMContentLoaded", loadBalatroPackage);
})();
