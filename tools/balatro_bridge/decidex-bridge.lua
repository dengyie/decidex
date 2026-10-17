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
    local hlg = (c.highlighted == true) and "true" or "false"
    return string.format('{"rank":"%s","suit":"%s","enhancement":%s,"edition":%s,"seal":%s,"highlighted":%s}',
      rank, suit, jstr(enh), jstr(ed), jstr(seal), hlg)
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

local function shop_card_json(c)
  local ok, out = pcall(function()
    local set = (c.ability and c.ability.set) or "Joker"
    local name = c.ability and c.ability.name or (c.base and c.base.name) or "?"
    local cost = tonumber(c.cost) or 0
    return string.format('{"name":"%s","set":"%s","cost":%s}', tostring(name), tostring(set), jstr(cost))
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
    local bname = b.name and select(2, pcall(function() return string.gsub(b.name, "^bl_", "") end)) or nil
    if G.STATES and G.STATE == G.STATES.BLIND_SELECT then
      local on_deck = (G.GAME and (G.GAME.blind_on_deck or (G.GAME.round_resets and G.GAME.round_resets.blind_on_deck))) or "Small"
      bname = on_deck .. " Blind"
      if on_deck == "Big" then t = "big" elseif on_deck == "Boss" then t = "boss" else t = "small" end
    elseif not bname or bname == "" then
      local on_deck = G.GAME.round_resets and G.GAME.round_resets.blind_on_deck or "Small"
      bname = on_deck .. " Blind"
      if on_deck == "Big" then t = "big" elseif on_deck == "Boss" then t = "boss" else t = "small" end
    end
    local chips = tonumber(b.chips) or 0
    if (chips <= 0 or (G.STATES and G.STATE == G.STATES.BLIND_SELECT)) and G.GAME.round_resets then
      local on_deck = (G.GAME and (G.GAME.blind_on_deck or (G.GAME.round_resets and G.GAME.round_resets.blind_on_deck))) or "Small"
      local choice_key = G.GAME.round_resets and G.GAME.round_resets.blind_choices and G.GAME.round_resets.blind_choices[on_deck]
      local pb = choice_key and G.P_BLINDS and G.P_BLINDS[choice_key]
      if pb and pb.chips then chips = tonumber(pb.chips) or 0 end
      if chips <= 0 then
        chips = (on_deck == "Big") and 450 or (on_deck == "Boss") and 600 or 300
      end
    end
    return {
      name = tostring(bname),
      type = t,
      chips = chips,
      scored = tonumber(G.GAME and G.GAME.chips or (G.GAME.current_round and G.GAME.current_round.score) or 0),
    }
  end)
  return ok and info or { name = "Small Blind", type = "small", chips = 300, scored = 0 }
end

local function find_cash_out_button()
  if not (G and G.STATES and G.STATE == G.STATES.ROUND_EVAL and G.round_eval) then return nil end
  if G.I and G.I.UIBOX then
    for _, uibox in ipairs(G.I.UIBOX) do
      if uibox.config and uibox.config.major == G.round_eval and not uibox.REMOVED then
        local btn = uibox.get_UIE_by_ID and uibox:get_UIE_by_ID('cash_out_button')
        if btn and btn.config and btn.config.button == 'cash_out' then
          return btn
        end
      end
    end
  end
  if G.round_eval.get_UIE_by_ID then
    local btn = G.round_eval:get_UIE_by_ID('cash_out_button')
    if btn and btn.config and btn.config.button == 'cash_out' then
      return btn
    end
  end
  return nil
end

local function is_shop_ready()
  if not (G and G.STATES and G.STATE == G.STATES.SHOP and G.shop and G.shop.T and G.shop.VT) then
    return false
  end
  if G.shop_ready then
    return true
  end
  if math.abs(G.shop.T.y - G.shop.VT.y) < 3 then
    local has_cards = (G.shop_jokers and G.shop_jokers.cards and #G.shop_jokers.cards > 0)
      or (G.shop_booster and G.shop_booster.cards and #G.shop_booster.cards > 0)
      or (G.shop_vouchers and G.shop_vouchers.cards and #G.shop_vouchers.cards > 0)
    if has_cards then
      G.shop_ready = true
      return true
    end
  end
  return false
end

local function find_next_round_button()
  if not is_shop_ready() then return nil end
  if G.shop.get_UIE_by_ID then
    local btn = G.shop:get_UIE_by_ID('next_round_button')
    if btn and btn.config and btn.config.button == 'toggle_shop' then return btn end
  end
  if G.I and G.I.UIBOX then
    for _, uibox in ipairs(G.I.UIBOX) do
      if (not uibox.config or uibox.config.major == G.shop) and not uibox.REMOVED then
        local btn = uibox.get_UIE_by_ID and uibox:get_UIE_by_ID('next_round_button')
        if btn and btn.config and btn.config.button == 'toggle_shop' then return btn end
      end
    end
  end
  return nil
end

local function find_select_blind_button()
  if not (G and G.STATES and G.STATE == G.STATES.BLIND_SELECT) then return nil, nil end
  local on_deck = (G.GAME and (G.GAME.blind_on_deck or (G.GAME.round_resets and G.GAME.round_resets.blind_on_deck))) or "Small"
  local opt = G.blind_select_opts and G.blind_select_opts[string.lower(on_deck)]
  if opt and opt.get_UIE_by_ID then
    local btn = opt:get_UIE_by_ID('select_blind_button')
    if btn then return btn, on_deck end
  end
  if G.blind_select and G.blind_select.get_UIE_by_ID then
    local btn = G.blind_select:get_UIE_by_ID('select_blind_button')
    if btn then return btn, on_deck end
  end
  if G.I and G.I.UIBOX then
    for _, uibox in ipairs(G.I.UIBOX) do
      local btn = uibox.get_UIE_by_ID and uibox:get_UIE_by_ID('select_blind_button')
      if btn then return btn, on_deck end
    end
  end
  local choice_key = G.GAME and G.GAME.round_resets and G.GAME.round_resets.blind_choices and G.GAME.round_resets.blind_choices[on_deck]
  local pb = choice_key and G.P_BLINDS and G.P_BLINDS[choice_key]
  if not pb and G.P_BLINDS then
    pb = (on_deck == "Big") and G.P_BLINDS.bl_big or (on_deck == "Boss") and G.P_BLINDS.bl_hook or G.P_BLINDS.bl_small
  end
  if pb then
    return {config = {ref_table = pb}}, on_deck
  end
  return nil, on_deck
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
  parts[#parts + 1] = '"round":' .. jstr(tonumber((G.GAME and G.GAME.round) or cr.round or 1))

  local shop_cards = (G.shop_jokers and G.shop_jokers.cards) or {}
  local reroll_cost = (G.GAME and G.GAME.current_round and G.GAME.current_round.reroll_cost) or 5
  parts[#parts + 1] = '"shop":{"cards":' .. jtable(map(shop_cards, shop_card_json)) .. ',"reroll_cost":' .. jstr(reroll_cost) .. '}'

  local st_name = "unknown"
  if G and G.STATES then
    for k, v in pairs(G.STATES) do if v == G.STATE then st_name = k end end
  end
  local hover_id = ""
  if G and G.CONTROLLER and G.CONTROLLER.hovering and G.CONTROLLER.hovering.target then
    local node = G.CONTROLLER.hovering.target
    local curr = node
    while curr do
      if curr.config and (curr.config.id or curr.config.button or curr.config.func) then
        hover_id = tostring(curr.config.id or curr.config.button or curr.config.func)
        break
      end
      curr = curr.parent
    end
    if hover_id == "" then
      hover_id = tostring(node.ID or "target")
    end
  end
  parts[#parts + 1] = '"state_name":' .. jstr(st_name)
  parts[#parts + 1] = '"hover":' .. jstr(hover_id)

  local can_cash_out = not not (find_cash_out_button() and not G.cashing_out)
  local can_toggle_shop = not not (find_next_round_button() and not (G.CONTROLLER and G.CONTROLLER.locks and G.CONTROLLER.locks.toggle_shop))
  local can_select_blind = not not (find_select_blind_button())
  parts[#parts + 1] = '"can_cash_out":' .. tostring(can_cash_out)
  parts[#parts + 1] = '"can_toggle_shop":' .. tostring(can_toggle_shop)
  parts[#parts + 1] = '"can_select_blind":' .. tostring(can_select_blind)
  parts[#parts + 1] = '"won":' .. tostring(not not (G.GAME and G.GAME.won))

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

local LAST_LIVE_JSON = nil

local function init_seq()
  local ok, items = pcall(function() return love.filesystem.getDirectoryItems("decidex_bridge") end)
  if not ok or not items then return end
  local max_seq = 0
  for _, f in ipairs(items) do
    local num = f:match("%-(%d+)%.json$")
    if num then
      local n = tonumber(num)
      if n and n > max_seq then max_seq = n end
    end
  end
  SEQ = max_seq
end
pcall(init_seq)

local function update_live_state(actual)
  local ok, json_str = pcall(function()
    if not (G and G.GAME) then return nil end
    return state_json(actual)
  end)
  if ok and json_str and json_str ~= LAST_LIVE_JSON then
    LAST_LIVE_JSON = json_str
    pcall(function()
      love.filesystem.createDirectory("decidex_bridge")
      love.filesystem.write("decidex_bridge/live_state.json", json_str)
    end)
  end
end

local function write_state(name, actual)
  local ok, err = pcall(function()
    SEQ = SEQ + 1
    local fname = string.format("decidex_bridge/%s-%04d.json", name, SEQ)
    love.filesystem.createDirectory("decidex_bridge")
    local j = state_json(actual)
    love.filesystem.write(fname, j)
    love.filesystem.write("decidex_bridge/live_state.json", j)
    LAST_LIVE_JSON = j
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

--[[
可选性能节流(env 门控,默认关闭)
=================================
Balatro 主循环默认按 `G.FPS_CAP = G.FPS_CAP or 500`(见游戏 main.lua)跑 500 FPS;
无头 Xvfb 上 vsync 不生效,Mesa 软件光栅会被推满 CPU —— 而本桥只被 2 FPS 采样的
网页读画面,回合制卡牌也不需要高帧率。

设 `DECIDEX_FPS_CAP=<n>`(n>=1)即把帧率压到 n;不设/为 0 则完全不动游戏。
main.lua 每帧执行 `G.FPS_CAP = G.FPS_CAP or 500`:只要非 nil 它就不覆盖,所以
在 love.update 里每帧重申一次即可稳拿控制权(单次赋值,开销可忽略)。纯性能旋钮,
不改任何游戏状态/数值,也不影响快照内容。
]]
local PERF_FPS_CAP = tonumber(os.getenv("DECIDEX_FPS_CAP") or "") or 0
local PERF_DEBUG = os.getenv("DECIDEX_PERF_DEBUG") == "1"
local _perf_frames = 0

--[[
节流开启时顺带滤掉游戏自带的长帧日志洪水。
game.lua 每帧 `if self.real_dt > 0.05 then print('LONG DT @ ...') end`;无头 Xvfb 的
软件光栅让单帧本就更长于 100ms(此处 dt 被钳到 0.1),再叠加帧率节流后 Long DT 成了
每帧必然事件,把 lovely 日志灌到 26MB、journald 涨到数百 MB,纯属噪声。只滤
`LONG DT ` 前缀这一行,不碰其它 print(chunk 名/报错照常)。
]]
if PERF_FPS_CAP >= 1 then
  local _ref_print = print
  print = function(...)
    local first = ...
    if type(first) == "string" and first:sub(1, 8) == "LONG DT " then return end
    return _ref_print(...)
  end
end

local function decidex_execute(action, args)
  if not G then return false, "no G" end
  args = args or {}

  if action == "play" or action == "discard" then
    if G.STATES and G.STATE ~= G.STATES.SELECTING_HAND then
      return false, "not in selecting_hand (current state: " .. tostring(G.STATE) .. ")"
    end
    if not (G.hand and G.hand.cards) then return false, "no hand" end
    if action == "play" then
      local hands_left = tonumber(G.GAME and G.GAME.current_round and G.GAME.current_round.hands_left) or 0
      if hands_left <= 0 then
        return false, "no hands left"
      end
      local chips = tonumber(G.GAME and G.GAME.chips) or 0
      local blind_chips = tonumber(G.GAME and G.GAME.blind and G.GAME.blind.chips) or 0
      if blind_chips > 0 and chips >= blind_chips then
        return false, "round already won, waiting for round evaluation"
      end
    elseif action == "discard" then
      local discards_left = tonumber(G.GAME and G.GAME.current_round and G.GAME.current_round.discards_left) or 0
      if discards_left <= 0 then
        return false, "no discards left"
      end
    end
    if args.indices then
      if G.hand.unhighlight_all then
        G.hand:unhighlight_all()
      else
        G.hand.highlighted = {}
        for _, c in ipairs(G.hand.cards) do c:highlight(false) end
      end
      for _, idx in ipairs(args.indices) do
        local c = G.hand.cards[tonumber(idx) or 0]
        if c then
          if G.hand.add_to_highlighted then
            G.hand:add_to_highlighted(c, true)
          else
            table.insert(G.hand.highlighted, c)
            c:highlight(true)
          end
        end
      end
    end
    local hlg_count = (G.hand.highlighted and #G.hand.highlighted) or 0
    if hlg_count <= 0 then
      return false, "no cards highlighted"
    end
    if action == "play" then
      local limit = (G.GAME and G.GAME.starting_params and G.GAME.starting_params.play_limit) or 5
      if hlg_count > limit then
        return false, "too many cards selected (" .. tostring(hlg_count) .. " > " .. tostring(limit) .. ")"
      end
      if G.FUNCS and G.FUNCS.play_cards_from_highlighted then
        G.FUNCS.play_cards_from_highlighted()
        return true, "played"
      end
    elseif action == "discard" then
      local limit = (G.GAME and G.GAME.starting_params and G.GAME.starting_params.discard_limit) or 5
      if hlg_count > limit then
        return false, "too many cards selected to discard (" .. tostring(hlg_count) .. " > " .. tostring(limit) .. ")"
      end
      if G.FUNCS and G.FUNCS.discard_cards_from_highlighted then
        G.FUNCS.discard_cards_from_highlighted()
        return true, "discarded"
      end
    end
    return false, "func not found"

  elseif action == "toggle_card" then
    if not (G.hand and G.hand.cards) then return false, "no hand" end
    local idx = tonumber(args.index) or 0
    local c = G.hand.cards[idx]
    if not c then return false, "invalid card index " .. tostring(idx) end
    if c.highlighted then
      if G.hand.remove_from_highlighted then
        G.hand:remove_from_highlighted(c, true)
      else
        c:highlight(false)
        for i = #G.hand.highlighted, 1, -1 do
          if G.hand.highlighted[i] == c then table.remove(G.hand.highlighted, i) break end
        end
      end
      return true, "unhighlighted"
    else
      if G.hand.add_to_highlighted then
        G.hand:add_to_highlighted(c, true)
      else
        table.insert(G.hand.highlighted, c)
        c:highlight(true)
      end
      return true, "highlighted"
    end

  elseif action == "select_cards" then
    if not (G.hand and G.hand.cards) then return false, "no hand" end
    if G.hand.unhighlight_all then
      G.hand:unhighlight_all()
    else
      G.hand.highlighted = {}
      for _, c in ipairs(G.hand.cards) do c:highlight(false) end
    end
    if args.indices then
      for _, idx in ipairs(args.indices) do
        local c = G.hand.cards[tonumber(idx) or 0]
        if c then
          if G.hand.add_to_highlighted then
            G.hand:add_to_highlighted(c, true)
          else
            table.insert(G.hand.highlighted, c)
            c:highlight(true)
          end
        end
      end
    end
    return true, "selected cards"

  elseif action == "unhighlight_all" then
    if G.hand then
      if G.hand.unhighlight_all then
        G.hand:unhighlight_all()
      else
        G.hand.highlighted = {}
        for _, c in ipairs(G.hand.cards or {}) do c:highlight(false) end
      end
      return true, "unhighlighted all"
    end
    return false, "no hand"

  elseif action == "sort_rank" then
    if G.FUNCS and G.FUNCS.sort_hand_value then
      G.FUNCS.sort_hand_value()
      return true, "sorted rank"
    elseif G.hand and G.hand.sort then
      G.hand:sort('desc')
      return true, "sorted rank"
    end
    return false, "cannot sort rank"

  elseif action == "sort_suit" then
    if G.FUNCS and G.FUNCS.sort_hand_suit then
      G.FUNCS.sort_hand_suit()
      return true, "sorted suit"
    elseif G.hand and G.hand.sort then
      G.hand:sort('suit desc')
      return true, "sorted suit"
    end
    return false, "cannot sort suit"

  elseif action == "reroll_shop" then
    if G.FUNCS and G.FUNCS.reroll_shop then
      local btn = G.shop and G.shop.get_UIE_by_ID and G.shop:get_UIE_by_ID('reroll_shop_button')
      G.FUNCS.reroll_shop(btn or {config = {}})
      return true, "rerolled shop"
    end
    return false, "no reroll_shop func"

  elseif action == "buy_card" or action == "buy_from_shop" then
    local area_name = args.area or "shop_jokers"
    local area = G.shop_jokers
    if area_name == "shop_booster" or area_name == "shop_boosters" then
      area = G.shop_booster
    elseif area_name == "shop_vouchers" or area_name == "shop_voucher" then
      area = G.shop_vouchers
    elseif G[area_name] then
      area = G[area_name]
    end
    if not (area and area.cards) then return false, "shop area not found: " .. tostring(area_name) end
    local idx = tonumber(args.index) or 0
    local c = area.cards[idx] or area.cards[idx + 1]
    if not c then return false, "card not found at index " .. tostring(idx) end
    if G.FUNCS and G.FUNCS.buy_from_shop then
      G.FUNCS.buy_from_shop({config = {ref_table = c}})
      return true, "bought card"
    end
    return false, "no buy_from_shop func"

  elseif action == "use_card" or action == "sell_card" then
    local area_name = args.area or "consumeables"
    local area = nil
    if area_name == "consumeables" or area_name == "consumables" then
      area = G.consumeables
    elseif area_name == "jokers" or area_name == "joker" then
      area = G.jokers
    elseif area_name == "hand" then
      area = G.hand
    elseif G[area_name] then
      area = G[area_name]
    end
    if not (area and area.cards) then return false, "area not found: " .. tostring(area_name) end
    local idx = tonumber(args.index) or 0
    local c = area.cards[idx] or area.cards[idx + 1]
    if not c then return false, "card not found at index " .. tostring(idx) end
    if action == "use_card" then
      if G.FUNCS and G.FUNCS.use_card then
        G.FUNCS.use_card({config = {ref_table = c}})
        return true, "used card"
      end
      return false, "no use_card func"
    elseif action == "sell_card" then
      if G.FUNCS and G.FUNCS.sell_card then
        G.FUNCS.sell_card({config = {ref_table = c}})
        return true, "sold card"
      end
      return false, "no sell_card func"
    end

  elseif action == "select_blind" then
    if G.STATES and G.STATE ~= G.STATES.BLIND_SELECT then
      return false, "not in blind_select"
    end
    local btn, on_deck = find_select_blind_button()
    if not btn then
      return false, "select_blind_button not ready"
    end
    if G.FUNCS and G.FUNCS.select_blind then
      G.FUNCS.select_blind(btn)
      return true, "selected blind " .. tostring(on_deck)
    end
    return false, "cannot select blind"

  elseif action == "cash_out" then
    if G.STATES and G.STATE ~= G.STATES.ROUND_EVAL then
      return false, "not in round_eval"
    end
    if G.cashing_out then
      return false, "cash_out already in progress"
    end
    local btn = find_cash_out_button()
    if not btn then
      return false, "cash_out_button not ready yet (animating)"
    end
    if G.FUNCS and G.FUNCS.cash_out then
      G.cashing_out = true
      G.FUNCS.cash_out(btn)
      return true, "cashed out"
    end
    return false, "no cash_out func"

  elseif action == "toggle_shop" then
    if G.STATES and G.STATE ~= G.STATES.SHOP then
      return false, "not in shop"
    end
    if not is_shop_ready() then
      return false, "shop entrance animating"
    end
    if G.CONTROLLER and G.CONTROLLER.locks and G.CONTROLLER.locks.toggle_shop then
      return false, "toggle_shop locked"
    end
    local btn = find_next_round_button()
    if not btn then
      return false, "next_round_button not ready"
    end
    if G.FUNCS and G.FUNCS.toggle_shop then
      G.FUNCS.toggle_shop(btn)
      return true, "toggled shop"
    end
    return false, "no toggle_shop func"

  elseif action == "start_run" then
    if G.FUNCS and G.FUNCS.start_run then
      G.FUNCS.start_run(nil, {stake = 1})
      return true, "started run"
    elseif G.start_run then
      G:start_run({stake = 1})
      return true, "started run via G:start_run"
    end
    return false, "no start_run func"
  end

  return false, "unknown action " .. tostring(action)
end

local _backs_unlocked = false
local _event_hooked = false
local _em_hooked = false
local function hook_event_safety()
  if not _event_hooked and Event and Event.handle then
    local _orig_Event_handle = Event.handle
    Event.handle = function(self, _results)
      if self.func and not self._decidex_safe then
        local orig_func = self.func
        self.func = function(...)
          local ok, res = pcall(orig_func, ...)
          if not ok then
            print("[Decidex Bridge] Suppressed unhandled exception in Event: " .. tostring(res))
            return true
          end
          return res
        end
        self._decidex_safe = true
      end
      return _orig_Event_handle(self, _results)
    end
    _event_hooked = true
  end

  if not _em_hooked and EventManager and EventManager.update then
    local _orig_em_update = EventManager.update
    EventManager.update = function(self, dt, forced)
      local count = 0
      while (self.queue_timer >= self.queue_last_processed + self.queue_dt or forced) and count < 8 do
        _orig_em_update(self, dt, forced)
        count = count + 1
        forced = false
      end
      if self.queue_timer > self.queue_last_processed + 0.5 then
        self.queue_last_processed = self.queue_timer - self.queue_dt
      end
    end
    _em_hooked = true
  end
end

local _ref_update = love.update or function(dt) end
function love.update(dt)
  _ref_update(dt)
  hook_event_safety()
  if G then
    G.DECIDEX_EXECUTE = decidex_execute
    if G.STATES and G.STATE ~= G.STATES.ROUND_EVAL then
      G.cashing_out = nil
      if G.I and G.I.UIBOX then
        for i = #G.I.UIBOX, 1, -1 do
          local uibox = G.I.UIBOX[i]
          if uibox and uibox.get_UIE_by_ID and uibox:get_UIE_by_ID('cash_out_button') then
            pcall(function() uibox:remove() end)
          end
        end
      end
    end
    if G.STATES and G.STATE ~= G.STATES.SHOP then
      G.shop_ready = nil
    end
  end
  pcall(function()
    if G.STATES and G.STATE == G.STATES.SPLASH and G.main_menu then
      G:main_menu()
    end
    if G.SETTINGS and not G.SETTINGS.tutorial_complete then
      G.SETTINGS.tutorial_complete = true
      if G.OVERLAY_TUTORIAL then
        if G.OVERLAY_TUTORIAL.Jimbo then G.OVERLAY_TUTORIAL.Jimbo:remove() end
        if G.OVERLAY_TUTORIAL.content then G.OVERLAY_TUTORIAL.content:remove() end
        G.OVERLAY_TUTORIAL:remove()
        G.OVERLAY_TUTORIAL = nil
      end
      G.SETTINGS.paused = false
    end
    if G.SETTINGS and G.SETTINGS.GAMESPEED ~= 4 then
      G.SETTINGS.GAMESPEED = 4
    end
    if G.OVERLAY_MENU and G.FUNCS and G.FUNCS.continue_unlock then
      local function has_continue(e)
        if not e then return false end
        if type(e) == 'table' then
          if e.config and e.config.button == 'continue_unlock' then return true end
          if e.children then
            for _, c in pairs(e.children) do
              if has_continue(c) then return true end
            end
          end
          if e.UIRoot and has_continue(e.UIRoot) then return true end
        end
        return false
      end
      if has_continue(G.OVERLAY_MENU) then
        G.FUNCS.continue_unlock()
      end
    end
    if G.STATES and G.STATE == G.STATES.SELECTING_HAND and G.GAME and G.GAME.blind and G.GAME.chips then
      local blind_chips = tonumber(G.GAME.blind.chips) or 0
      local chips = tonumber(G.GAME.chips) or 0
      local hands_left = tonumber(G.GAME.current_round and G.GAME.current_round.hands_left) or 0
      if (blind_chips > 0 and chips >= blind_chips) or hands_left <= 0 then
        if not G.E_MANAGER or #G.E_MANAGER.queues.base == 0 then
          G.STATE = G.STATES.NEW_ROUND
          G.STATE_COMPLETE = false
        end
      end
    end
    if not _backs_unlocked and G.P_CENTERS then
      for _, v in pairs(G.P_CENTERS) do
        if v.set == 'Back' then v.unlocked = true end
      end
      _backs_unlocked = true
    end
    if G.GAME and G.GAME.viewed_back and G.GAME.viewed_back.effect and G.GAME.viewed_back.effect.center then
      G.GAME.viewed_back.effect.center.unlocked = true
    end
  end)
  pcall(function()
    local f = io.open("/tmp/balatro_eval.lua", "r")
    if f then
      local code = f:read("*a")
      f:close()
      os.remove("/tmp/balatro_eval.lua")
      if code and code ~= "" then
        local chunk, err = loadstring(code)
        if chunk then
          local ok, exec_err = pcall(chunk)
          if not ok then sendDebugMessage("bridge eval run err: " .. tostring(exec_err)) end
        else
          sendDebugMessage("bridge eval parse err: " .. tostring(err))
        end
      end
    end
  end)
  if PERF_FPS_CAP >= 1 and G then G.FPS_CAP = PERF_FPS_CAP end
  if PERF_DEBUG then
    _perf_frames = _perf_frames + 1
    if _perf_frames % 200 == 0 then
      pcall(function()
        love.filesystem.createDirectory("decidex_bridge")
        love.filesystem.write("decidex_bridge/perf.txt", string.format(
          "env=%s G.FPS_CAP=%s love_fps=%d dt=%.4f\n",
          tostring(PERF_FPS_CAP), tostring(G and G.FPS_CAP),
          love.timer.getFPS(), dt or -1))
      end)
    end
  end
  pcall(update_live_state)
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
      local ok, res = pcall(ref_play, e)
      if not ok then
        print("[Decidex Bridge] Handled evaluate_play error safely: " .. tostring(res))
        return true
      end
      return res
    end
  end
  local ref_discard = G.FUNCS.discard_cards_from_highlighted
  if ref_discard then
    G.FUNCS.discard_cards_from_highlighted = function(e)
      pcall(function() write_state("action", { type = "discard", cards = highlighted_indices() }) end)
      local ok, res = pcall(ref_discard, e)
      if not ok then
        print("[Decidex Bridge] Handled discard error safely: " .. tostring(res))
        return true
      end
      return res
    end
  end
end
