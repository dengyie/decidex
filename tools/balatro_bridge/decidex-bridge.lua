--- STEAMODDED HEADER
--- MOD_NAME: DecideX Bridge
--- MOD_ID: decidex-bridge
--- MOD_AUTHOR: [mango]
--- MOD_DESCRIPTION: Read-only Balatro state dumps for the DecideX advisor. Observes only - never plays for you.

--[[
DecideX Balatro 状态桥(只读)
============================
- 在每个"决策点"(进入选牌状态 / 出牌前 / 弃牌前)把手牌、小丑、盲注、资源
  dump 成 JSON 到存档目录 decidex_bridge/ 下(Windows: %APPDATA%/Balatro/decidex_bridge/)。
- "pre" 快照(actual=null)是给顾问看决策点;出牌/弃牌瞬间会再 dump 一张带
  actual 的快照,供网页端对比"顾问 vs 玩家"。
- 红线:只观察,不模拟点击、不自动出牌、不改任何游戏数值。
- 依赖 Steamodded(smods)。所有逻辑都在 pcall 里,桥挂了不影响游戏。
]]

local SEQ = 0
local LAST_SIG = nil

local SUIT_MAP = { Spades = "S", Hearts = "H", Diamonds = "D", Clubs = "C" }
local RANK_MAP = { [11] = "J", [12] = "Q", [13] = "K", [14] = "A" }
local SEAL_MAP = { Red = "red", Blue = "blue", Gold = "gold", Purple = "purple" }
local ENH_MAP = { ["Bonus"] = "bonus", ["Mult"] = "mult", ["Wild"] = "wild",
                  ["Glass"] = "glass", ["Steel"] = "steel", ["Gold"] = "gold", ["Lucky"] = "lucky" }
local EDITION_MAP = { foil = "foil", holo = "holo", polychrome = "poly" }

local function jstr(v)
  if v == nil then return "null" end
  if type(v) == "boolean" then return v and "true" or "false" end
  if type(v) == "number" then
    if v ~= v or v == math.huge then return "null" end
    local s = tostring(v)
    return s
  end
  if type(v) == "string" then
    return '"' .. v:gsub('\\', '\\\\'):gsub('"', '\\"'):gsub('[\r\n]', ' '):gsub('[%c]', '') .. '"'
  end
  return "null"
end

local function jtable(t)
  -- card_json/joker_json 等返回的已是 JSON 字符串,原样拼接;其余按字面量编码
  local parts = {}
  for _, v in ipairs(t) do
    if type(v) == "string" then parts[#parts + 1] = v
    else parts[#parts + 1] = jstr(v) end
  end
  return "[" .. table.concat(parts, ",") .. "]"
end

local function jmap(t)
  local parts = {}
  for k, v in pairs(t) do parts[#parts + 1] = jstr(k) .. ":" .. jstr(v) end
  return "{" .. table.concat(parts, ",") .. "}"
end

local function card_json(c)
  local ok, out = pcall(function()
    local rank = RANK_MAP[c.base.id] or tostring(c.base.id)
    local suit = SUIT_MAP[c.base.suit] or "?"
    local enh = nil
    if c.ability and c.ability.set == "Enhanced" and c.ability.effect then
      enh = ENH_MAP[c.ability.effect]
    end
    local ed = nil
    if c.edition then
      for k, v in pairs(EDITION_MAP) do if c.edition[k] then ed = v end end
    end
    local seal = c.seal and SEAL_MAP[c.seal] or nil
    return string.format('{"rank":"%s","suit":"%s","enhancement":%s,"edition":%s,"seal":%s}',
      rank, suit, jstr(enh), jstr(ed), jstr(seal))
  end)
  return ok and out or "null"
end

local function joker_json(c)
  local ok, out = pcall(function()
    local a = c.ability or {}
    local extra = {}
    if a.extra and type(a.extra) == "table" then
      for k, v in pairs(a.extra) do
        if type(v) == "number" or type(v) == "string" or type(v) == "boolean" then extra[k] = v end
      end
    end
    return string.format('{"name":"%s","chips":%s,"mult":%s,"xmult":%s,"extra":%s}',
      tostring(a.name or "?"),
      jstr(tonumber(a.chips) or 0), jstr(tonumber(a.mult) or 0),
      jstr(tonumber(a.x_mult) or 1), jmap(extra))
  end)
  return ok and out or "null"
end

local function consumable_json(c)
  local ok, out = pcall(function()
    local set = (c.ability and c.ability.set) or "Tarot"
    local t = "tarot"
    if set == "Planet" then t = "planet" elseif set == "Spectral" then t = "spectral" end
    return string.format('{"name":"%s","type":"%s"}', tostring(c.ability and c.ability.name or "?"), t)
  end)
  return ok and out or "null"
end

local function area_cards(area)
  local out = {}
  if area and area.cards then
    for _, c in ipairs(area.cards) do out[#out + 1] = c end
  end
  return out
end

local function blind_info()
  local b = G.GAME and G.GAME.blind
  if not b then return { name = "Small Blind", type = "small", chips = 300, scored = 0 } end
  local ok, info = pcall(function()
    local t = "small"
    if b.get_type then
      local bt = b:get_type()
      if bt == "Boss" then t = "boss" elseif bt == "Big" then t = "big" end
    end
    return {
      name = tostring(b.name and select(2, pcall(function() return string.gsub(b.name, "^bl_", "") end)) or "blind"),
      type = t,
      chips = tonumber(b.chips) or 300,
      scored = tonumber(G.GAME.current_round and G.GAME.current_round.score or 0),
    }
  end)
  return ok and info or { name = "blind", type = "small", chips = 300, scored = 0 }
end

local function state_json(actual)
  local hand = area_cards(G.hand)
  local jokers = area_cards(G.jokers)
  local cons = area_cards(G.consumeables)
  local cr = G.GAME and G.GAME.current_round or {}
  local b = blind_info()
  local parts = {}
  parts[#parts + 1] = '"hand":' .. jtable(map(hand, card_json))
  parts[#parts + 1] = '"blind":' .. jmap(b)
  parts[#parts + 1] = '"jokers":' .. jtable(map(jokers, joker_json))
  parts[#parts + 1] = '"consumables":' .. jtable(map(cons, consumable_json))
  parts[#parts + 1] = '"hands_left":' .. jstr(tonumber(cr.hands_left) or 0)
  parts[#parts + 1] = '"discards_left":' .. jstr(tonumber(cr.discards_left) or 0)
  parts[#parts + 1] = '"hand_size":' .. jstr(tonumber(G.GAME and G.GAME.hand_size or 8))
  parts[#parts + 1] = '"money":' .. jstr(tonumber(G.GAME and G.GAME.dollars or 0))
  parts[#parts + 1] = '"ante":' .. jstr(tonumber(G.GAME and G.GAME.round_resets and G.GAME.round_resets.ante or 1))
  parts[#parts + 1] = '"round":' .. jstr(tonumber(cr.round or 1))
  if actual then
    parts[#parts + 1] = '"actual":{"type":"' .. actual.type .. '","cards":' .. jtable(actual.cards) .. '}'
  else
    parts[#parts + 1] = '"actual":null'
  end
  return "{" .. table.concat(parts, ",") .. "}"
end

function map(t, f)
  local out = {}
  for _, v in ipairs(t) do out[#out + 1] = f(v) end
  return out
end

local function write_state(name, actual)
  local ok, err = pcall(function()
    SEQ = SEQ + 1
    local fname = string.format("decidex_bridge/%s-%04d.json", name, SEQ)
    love.filesystem.createDirectory("decidex_bridge")
    love.filesystem.write(fname, state_json(actual))
  end)
  if not ok then sendDebugMessage("DecideX bridge write failed: " .. tostring(err)) end
end

local function hand_signature()
  local sig = {}
  for _, c in ipairs(area_cards(G.hand)) do
    sig[#sig + 1] = tostring(c.base.id) .. ":" .. tostring(c.base.suit)
  end
  local cr = G.GAME and G.GAME.current_round or {}
  sig[#sig + 1] = tostring(cr.hands_left or 0) .. ":" .. tostring(cr.discards_left or 0)
  return table.concat(sig, ",")
end

-- 决策点:进入选牌状态时(手牌已发好),dump pre 快照。
local function maybe_dump_pre()
  if not (G and G.STATE == G.STATES.SELECTING_HAND) then return end
  if not (G.GAME and G.GAME.current_round) then return end
  local sig = hand_signature()
  if sig == LAST_SIG then return end
  LAST_SIG = sig
  write_state("state", nil)
end

local _ref_update = love.update or function(dt) end
function love.update(dt)
  _ref_update(dt)
  pcall(maybe_dump_pre)
end

-- 玩家实际操作:出牌 / 弃牌瞬间,记下选中的牌再放行。
local function highlighted_indices()
  local idx = {}
  local pos = {}
  for i, c in ipairs(area_cards(G.hand)) do pos[c] = i end
  for _, c in ipairs(area_cards(G.hand.highlighted or {})) do
    if pos[c] then idx[#idx + 1] = pos[c] end
  end
  table.sort(idx)
  return idx
end

if G.FUNCS then
  local ref_play = G.FUNCS.evaluate_play
  if ref_play then
    G.FUNCS.evaluate_play = function(e)
      pcall(function() write_state("action", { type = "play", cards = highlighted_indices() }) end)
      return ref_play(e)
    end
  end
  local ref_discard = G.FUNCS.discard_cards_from_highlighted
  if ref_discard then
    G.FUNCS.discard_cards_from_highlighted = function(e)
      pcall(function() write_state("action", { type = "discard", cards = highlighted_indices() }) end)
      return ref_discard(e)
    end
  end
end
