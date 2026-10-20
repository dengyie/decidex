--[[
  DecideX WASM Bridge for Balatro on love.js
  - Intercepts state changes in SELECTING_HAND and emits [DECIDEX_STATE] JSON via print()
  - Provides G.DECIDEX_EXECUTE for direct in-browser programmatic control
  - Polls /tmp/balatro_eval.lua from Emscripten virtual FS for commands from JS
  - Enforces windowed mode: permanently blocks fullscreen to preserve DecideX HUD and coordinate fidelity
]]

if love and love.window then
  love.window.setFullscreen = function(fullscreen, ftype)
    return false
  end
  if love.window.getFullscreen then
    love.window.getFullscreen = function()
      return false, "windowed"
    end
  end
end

local SUIT_MAP = { Spades = "S", Hearts = "H", Diamonds = "D", Clubs = "C" }
local RANK_MAP = {
  [11] = "J", [12] = "Q", [13] = "K", [14] = "A",
  ["11"] = "J", ["12"] = "Q", ["13"] = "K", ["14"] = "A",
  ["Jack"] = "J", ["Queen"] = "Q", ["King"] = "K", ["Ace"] = "A"
}
local SEAL_MAP = { Red = "red", Blue = "blue", Gold = "gold", Purple = "purple" }
local ENH_MAP = {
  ["Bonus"] = "bonus", ["Mult"] = "mult", ["Wild"] = "wild",
  ["Glass"] = "glass", ["Steel"] = "steel", ["Gold"] = "gold", ["Lucky"] = "lucky"
}
local EDITION_MAP = { foil = "foil", holo = "holo", polychrome = "poly" }

local function jstr(v)
  if v == nil then return "null" end
  if type(v) == "boolean" then return v and "true" or "false" end
  if type(v) == "number" then
    if v ~= v or v == math.huge then return "null" end
    return tostring(v)
  end
  if type(v) == "string" then
    return '"' .. v:gsub('\\', '\\\\'):gsub('"', '\\"'):gsub('[\r\n]', ' '):gsub('[%c]', '') .. '"'
  end
  return "null"
end

local function jtable(t)
  local parts = {}
  for _, v in ipairs(t) do
    if type(v) == "string" then
      parts[#parts + 1] = v
    else
      parts[#parts + 1] = jstr(v)
    end
  end
  return "[" .. table.concat(parts, ",") .. "]"
end

local function jmap(t)
  local parts = {}
  for k, v in pairs(t) do
    local val_str = type(v) == "string" and (v:sub(1,1) == "{" or v:sub(1,1) == "[") and v or jstr(v)
    parts[#parts + 1] = jstr(k) .. ":" .. val_str
  end
  return "{" .. table.concat(parts, ",") .. "}"
end

local function map(t, fn)
  local res = {}
  for i, v in ipairs(t or {}) do
    res[#res + 1] = fn(v, i)
  end
  return res
end

local function card_json(c, idx)
  local r = c.base and c.base.value or ""
  local s = c.base and c.base.suit or ""
  r = RANK_MAP[tonumber(r)] or RANK_MAP[r] or r
  s = SUIT_MAP[s] or s

  local enh = nil
  if c.ability and c.ability.name then
    enh = ENH_MAP[c.ability.name]
  end

  local edition = nil
  if c.edition and type(c.edition) == "table" then
    for k, v in pairs(EDITION_MAP) do
      if c.edition[k] then edition = v break end
    end
  end

  local seal = nil
  if c.seal then
    seal = SEAL_MAP[c.seal] or tostring(c.seal):lower()
  end

  local parts = {
    '"rank":' .. jstr(r),
    '"suit":' .. jstr(s),
    '"enhancement":' .. jstr(enh),
    '"edition":' .. jstr(edition),
    '"seal":' .. jstr(seal),
    '"debuff":' .. (c.debuff and "true" or "false"),
    '"highlighted":' .. jstr(c.highlighted and true or false),
    '"index":' .. jstr(idx)
  }
  return "{" .. table.concat(parts, ",") .. "}"
end

local function serialize_extra(extra)
  if type(extra) == "number" or type(extra) == "string" or type(extra) == "boolean" then
    return jstr(extra)
  elseif type(extra) == "table" then
    local parts = {}
    for k, v in pairs(extra) do
      if type(k) == "string" and (type(v) == "number" or type(v) == "string" or type(v) == "boolean") then
        parts[#parts + 1] = jstr(k) .. ":" .. jstr(v)
      end
    end
    return "{" .. table.concat(parts, ",") .. "}"
  end
  return "null"
end

local function joker_json(c, idx)
  local name = c.ability and c.ability.name or "Joker"
  local mult = c.ability and c.ability.mult or 0
  local x_mult = c.ability and (c.ability.x_mult or c.ability.xmult) or 1
  local chips = c.ability and (c.ability.chips or c.ability.t_chips) or 0
  local extra_str = serialize_extra(c.ability and c.ability.extra)

  local edition = nil
  if c.edition and type(c.edition) == "table" then
    for k, v in pairs(EDITION_MAP) do
      if c.edition[k] then edition = v break end
    end
  end

  local rarity = (c.config and c.config.center and c.config.center.rarity) or 1
  local cost = tonumber(c.cost or 0) or 0
  local sell_cost = tonumber(c.sell_cost or 0) or 0

  local parts = {
    '"name":' .. jstr(name),
    '"mult":' .. jstr(mult),
    '"xmult":' .. jstr(x_mult),
    '"chips":' .. jstr(chips),
    '"extra":' .. extra_str,
    '"edition":' .. jstr(edition),
    '"rarity":' .. jstr(rarity),
    '"cost":' .. jstr(cost),
    '"sell_cost":' .. jstr(sell_cost),
    '"debuff":' .. jstr(c.debuff and true or false),
    '"index":' .. jstr(idx)
  }
  return "{" .. table.concat(parts, ",") .. "}"
end

local function shop_card_json(c, idx)
  local name = c.ability and c.ability.name or (c.config and c.config.center and c.config.center.name) or "Card"
  local mult = c.ability and c.ability.mult or 0
  local x_mult = c.ability and (c.ability.x_mult or c.ability.xmult) or 1
  local chips = c.ability and (c.ability.chips or c.ability.t_chips) or 0
  local cost = tonumber(c.cost or 0) or 0
  local rarity = (c.config and c.config.center and c.config.center.rarity) or 1
  local set = (c.ability and c.ability.set) or (c.config and c.config.center and c.config.center.set) or "Joker"

  local edition = nil
  if c.edition and type(c.edition) == "table" then
    for k, v in pairs(EDITION_MAP) do
      if c.edition[k] then edition = v break end
    end
  end

  local parts = {
    '"name":' .. jstr(name),
    '"type":' .. jstr(set:lower()),
    '"cost":' .. jstr(cost),
    '"rarity":' .. jstr(rarity),
    '"mult":' .. jstr(mult),
    '"xmult":' .. jstr(x_mult),
    '"chips":' .. jstr(chips),
    '"edition":' .. jstr(edition),
    '"extra":' .. serialize_extra(c.ability and c.ability.extra),
    '"index":' .. jstr(idx)
  }
  return "{" .. table.concat(parts, ",") .. "}"
end

local function consumable_json(c, idx)
  local name = c.ability and c.ability.name or ""
  local set = c.ability and c.ability.set or ""
  local can_use = true
  if c.can_use_consumeable then
    local ok, res = pcall(function() return c:can_use_consumeable() end)
    can_use = not (ok and res == false)
  end
  local parts = {
    '"name":' .. jstr(name),
    '"type":' .. jstr(set:lower()),
    '"index":' .. jstr(idx),
    '"can_use":' .. (can_use and "true" or "false")
  }
  return "{" .. table.concat(parts, ",") .. "}"
end

local function blind_info()
  local b = G.GAME and G.GAME.blind
  if not b then return { name = "None", type = "small", chips = 0, scored = 0 } end
  local btype = "small"
  if b.name == "Big Blind" or (type(b.key) == "string" and b.key:find("big")) then btype = "big" end
  if b.boss or (type(b.key) == "string" and b.key:find("boss")) then btype = "boss" end

  local upcoming_boss = nil
  if G.GAME and G.GAME.round_resets and G.GAME.round_resets.blind_choices and G.GAME.round_resets.blind_choices.Boss then
    local bkey = G.GAME.round_resets.blind_choices.Boss
    if G.P_BLINDS and G.P_BLINDS[bkey] and G.P_BLINDS[bkey].name then
      upcoming_boss = G.P_BLINDS[bkey].name
    else
      upcoming_boss = tostring(bkey)
    end
  end

  local res = {
    name = tostring(b.name or "Blind"),
    type = btype,
    chips = tonumber(b.chips or 0) or 0,
    scored = tonumber(G.GAME and G.GAME.chips or 0) or 0
  }
  if upcoming_boss then
    res.upcoming_boss = upcoming_boss
  end
  if type(b.debuff) == "table" and next(b.debuff) and not b.disabled then
    res.debuff = true
    if b.debuff.suit then res.debuff_suit = tostring(b.debuff.suit) end
    if b.debuff.is_face then res.debuff_face = true end
  else
    res.debuff = false
  end
  return res
end

local function area_cards(area)
  if not area or not area.cards then return {} end
  return area.cards
end

local function find_uie_by_id(id)
  if not G then return nil end
  if G.round_eval and not G.round_eval.REMOVED and G.round_eval.get_UIE_by_ID then
    local res = nil
    pcall(function() res = G.round_eval:get_UIE_by_ID(id) end)
    if res then return res end
  end
  if G.shop and not G.shop.REMOVED and G.shop.get_UIE_by_ID then
    local res = nil
    pcall(function() res = G.shop:get_UIE_by_ID(id) end)
    if res then return res end
  end
  if G.blind_select and not G.blind_select.REMOVED and G.blind_select.get_UIE_by_ID then
    local res = nil
    pcall(function() res = G.blind_select:get_UIE_by_ID(id) end)
    if res then return res end
  end
  return nil
end

local function find_cash_out_button()
  if not (G and G.STATES and G.STATE == G.STATES.ROUND_EVAL) then
    return nil
  end
  local now = (love.timer and love.timer.getTime()) or os.clock()
  if G.cashing_out and _G._last_cash_out_time and (now - _G._last_cash_out_time > 1.8) then
    G.cashing_out = false
  end
  if G.cashing_out then return nil end
  if G.round_eval and not G.round_eval.REMOVED and G.round_eval.get_UIE_by_ID then
    local btn = nil
    pcall(function() btn = G.round_eval:get_UIE_by_ID('cash_out_button') end)
    if btn and btn.config and btn.config.button == 'cash_out' then
      return btn
    end
  end
  if G.I and G.I.UIBOX then
    for _, uibox in ipairs(G.I.UIBOX) do
      if not uibox.REMOVED and uibox.get_UIE_by_ID then
        local btn = nil
        pcall(function() btn = uibox:get_UIE_by_ID('cash_out_button') end)
        if btn and btn.config and btn.config.button == 'cash_out' then
          return btn
        end
      end
    end
  end
  return nil
end

local function force_advance_to_shop()
  if not (G and G.STATES and G.STATE == G.STATES.ROUND_EVAL) then return false end
  pcall(function()
    if G.round_eval then
      G.round_eval:remove()
      G.round_eval = nil
    end
    if G.I and G.I.UIBOX then
      for i = #G.I.UIBOX, 1, -1 do
        local uibox = G.I.UIBOX[i]
        if uibox and not uibox.REMOVED and uibox.get_UIE_by_ID then
          local btn = nil
          pcall(function() btn = uibox:get_UIE_by_ID('cash_out_button') end)
          if btn then
            pcall(function() uibox:remove() end)
          end
        end
      end
    end
    if G.GAME and G.GAME.current_round then
      G.GAME.current_round.jokers_purchased = 0
      local resets = G.GAME.round_resets or {}
      local bonus = G.GAME.round_bonus or {}
      G.GAME.current_round.discards_left = math.max(0, (resets.discards or 3) + (bonus.discards or 0))
      G.GAME.current_round.hands_left = math.max(1, (resets.hands or 4) + (bonus.next_hands or 0))
      if G.GAME.current_round.dollars and G.GAME.current_round.dollars > 0 then
        if ease_dollars then ease_dollars(G.GAME.current_round.dollars) end
      end
    end
    G.STATE = G.STATES.SHOP
    if G.GAME then
      G.GAME.shop_free = nil
      G.GAME.shop_d6ed = nil
    end
    G.STATE_COMPLETE = false
    G.cashing_out = false
    _G._round_eval_enter_time = nil
  end)
  return true
end

local function find_next_round_button()
  if not (G and G.shop and not G.shop.REMOVED) then return nil end
  if G.CONTROLLER and G.CONTROLLER.locks and G.CONTROLLER.locks.toggle_shop then
    local now = (love.timer and love.timer.getTime()) or os.clock()
    if not _G._toggle_shop_lock_time then _G._toggle_shop_lock_time = now end
    if (now - _G._toggle_shop_lock_time > 2.5) then
      G.CONTROLLER.locks.toggle_shop = nil
      _G._toggle_shop_lock_time = nil
    else
      return nil
    end
  else
    _G._toggle_shop_lock_time = nil
  end
  if G.shop.get_UIE_by_ID then
    local btn = nil
    pcall(function() btn = G.shop:get_UIE_by_ID('next_round_button') end)
    if btn and btn.config and btn.config.button == 'toggle_shop' then return btn end
    if btn then return btn end
  end
  return { config = { id = 'next_round_button', button = 'toggle_shop' } }
end

local function find_select_blind_button()
  if not (G and G.STATES and G.STATE == G.STATES.BLIND_SELECT) then return nil end
  if G.GAME and G.GAME.facing_blind then return nil end
  if not (G.blind_select and not G.blind_select.REMOVED) then return nil end

  local on_deck = (G.GAME and (G.GAME.blind_on_deck or (G.GAME.round_resets and G.GAME.round_resets.blind_on_deck))) or "Small"
  local opt = G.blind_select_opts and G.blind_select_opts[string.lower(on_deck)]
  if opt and opt.get_UIE_by_ID then
    local btn = nil
    pcall(function() btn = opt:get_UIE_by_ID('select_blind_button') end)
    if btn and btn.config and btn.config.ref_table then return btn, on_deck end
  end
  if G.blind_select and G.blind_select.get_UIE_by_ID then
    local btn = nil
    pcall(function() btn = G.blind_select:get_UIE_by_ID('select_blind_button') end)
    if btn and btn.config and btn.config.ref_table then return btn, on_deck end
  end

  local choice_key = G.GAME and G.GAME.round_resets and G.GAME.round_resets.blind_choices and G.GAME.round_resets.blind_choices[on_deck]
  local pb = choice_key and G.P_BLINDS and G.P_BLINDS[choice_key]
  if not pb and G.P_BLINDS then
    pb = (on_deck == "Big") and G.P_BLINDS.bl_big or (on_deck == "Boss") and G.P_BLINDS.bl_hook or G.P_BLINDS.bl_small
  end
  if pb then
    return { config = { id = 'select_blind_button', ref_table = pb } }, on_deck
  end
  return nil, on_deck
end

local function find_skip_blind_button()
  if not (G and G.STATES and G.STATE == G.STATES.BLIND_SELECT) then return nil end
  if G.GAME and G.GAME.facing_blind then return nil end
  local on_deck = (G.GAME and (G.GAME.blind_on_deck or (G.GAME.round_resets and G.GAME.round_resets.blind_on_deck))) or "Small"
  if on_deck == "Boss" then return nil end

  local opt = G.blind_select_opts and G.blind_select_opts[string.lower(on_deck)]
  if not opt then return nil end

  local skip_btn = nil
  if opt.get_UIE_by_ID then
    pcall(function() skip_btn = opt:get_UIE_by_ID('skip_blind_button') end)
    if skip_btn then return skip_btn, on_deck end
    if opt.tag and opt.tag.key then
      pcall(function() skip_btn = opt:get_UIE_by_ID('tag_' .. opt.tag.key) end)
      if skip_btn then return skip_btn, on_deck end
    end
  end

  local function search_btn(node)
    if not node then return nil end
    if node.config and (node.config.button == 'skip_blind' or node.config.id == 'skip_blind_button') then
      return node
    end
    if node.children then
      for _, child in ipairs(node.children) do
        local found = search_btn(child)
        if found then return found end
      end
    end
    return nil
  end
  pcall(function() skip_btn = search_btn(opt) end)
  if skip_btn then return skip_btn, on_deck end

  local tag_obj = opt.tag or (opt.config and opt.config.tag)
  return { config = { id = 'skip_blind_button', button = 'skip_blind', ref_table = tag_obj or opt } }, on_deck
end

local function get_blind_tag_info()
  local on_deck = (G.GAME and (G.GAME.blind_on_deck or (G.GAME.round_resets and G.GAME.round_resets.blind_on_deck))) or "Small"
  if on_deck == "Boss" then return nil end
  local opt = G.blind_select_opts and G.blind_select_opts[string.lower(on_deck)]
  local tag = nil
  if opt then
    tag = opt.tag or (opt.config and opt.config.tag)
  end
  if not tag and G.GAME and G.GAME.round_resets and G.GAME.round_resets.blind_tags then
    tag = G.GAME.round_resets.blind_tags[on_deck] or G.GAME.round_resets.blind_tags[string.lower(on_deck)]
  end
  if tag then
    local tname = (type(tag) == "table" and (tag.name or (tag.ability and tag.ability.name))) or (type(tag) == "string" and tag) or ""
    local tkey = (type(tag) == "table" and (tag.key or (tag.config and tag.config.type))) or (type(tag) == "string" and tag) or ""
    return { name = tostring(tname), key = tostring(tkey) }
  end
  return nil
end

local _HAND_HISTORY = {}
local _HAND_HISTORY_ORDER = {}
local _HAND_HISTORY_KEY = nil

local function hand_history_key()
  local game = G and G.GAME or {}
  local round = game.round or (game.current_round and game.current_round.round) or 0
  local ante = game.round_resets and game.round_resets.ante or 0
  local blind = game.blind and ((game.blind.name) or (game.blind.config and game.blind.config.blind and game.blind.config.blind.name)) or ""
  local deck = game.blind_on_deck or (game.round_resets and game.round_resets.blind_on_deck) or ""
  return table.concat({tostring(ante), tostring(round), tostring(deck), tostring(blind)}, ":")
end

local function reset_hand_history_if_needed()
  local key = hand_history_key()
  if key ~= _HAND_HISTORY_KEY then
    _HAND_HISTORY_KEY = key
    _HAND_HISTORY = {}
    _HAND_HISTORY_ORDER = {}
  end
end

local function record_hand_type(hand_type)
  if not hand_type or hand_type == "" then return end
  reset_hand_history_if_needed()
  if not _HAND_HISTORY[hand_type] then
    _HAND_HISTORY[hand_type] = 0
    _HAND_HISTORY_ORDER[#_HAND_HISTORY_ORDER + 1] = hand_type
  end
  _HAND_HISTORY[hand_type] = _HAND_HISTORY[hand_type] + 1
end

local function game_hand_stats()
  local hands = G and G.GAME and G.GAME.hands
  if type(hands) ~= "table" then return nil end
  local has_round_counts = false
  for _, entry in pairs(hands) do
    if type(entry) == "table" and entry.played_this_round ~= nil then
      has_round_counts = true
      break
    end
  end
  return hands, has_round_counts
end

local function current_round_hand_history()
  reset_hand_history_if_needed()
  local hands, has_round_counts = game_hand_stats()
  local played = {}
  if hands and has_round_counts then
    -- The engine's per-hand counter is authoritative: it also covers hands
    -- played manually, not just those dispatched through this bridge.
    for name, entry in pairs(hands) do
      local count = tonumber(entry.played_this_round) or 0
      for _ = 1, count do played[#played + 1] = tostring(name) end
    end
    table.sort(played)
  else
    for _, name in ipairs(_HAND_HISTORY_ORDER) do
      local count = _HAND_HISTORY[name] or 0
      for _ = 1, count do played[#played + 1] = name end
    end
  end
  local most, count = nil, 0
  local tally = {}
  for _, name in ipairs(played) do tally[name] = (tally[name] or 0) + 1 end
  for name, value in pairs(tally) do
    if value > count then most, count = name, value end
  end
  return played, most
end

local function run_most_played_hand()
  local hands = game_hand_stats()
  local most, count = nil, 0
  if hands then
    for name, entry in pairs(hands) do
      local value = tonumber(entry.played) or 0
      if value > count then most, count = tostring(name), value end
    end
  end
  if most then return most end
  local _, round_most = current_round_hand_history()
  return round_most
end

local function classify_selected_hand(cards)
  local n = #(cards or {})
  if n == 0 then return nil end
  local counts, suits = {}, {}
  for _, c in ipairs(cards) do
    local rank = tostring(c.base and c.base.value or "")
    counts[rank] = (counts[rank] or 0) + 1
    local suit = tostring(c.base and c.base.suit or "")
    suits[suit] = (suits[suit] or 0) + 1
  end
  local groups = {}
  for _, count in pairs(counts) do groups[#groups + 1] = count end
  table.sort(groups, function(a, b) return a > b end)
  local flush = false
  for _, count in pairs(suits) do if count == n and n >= 5 then flush = true break end end
  local values, seen = {}, {}
  local rank_values = { ["2"] = 2, ["3"] = 3, ["4"] = 4, ["5"] = 5, ["6"] = 6, ["7"] = 7, ["8"] = 8, ["9"] = 9, ["10"] = 10, J = 11, Q = 12, K = 13, A = 14 }
  for rank in pairs(counts) do
    local value = rank_values[rank] or tonumber(rank)
    if value and not seen[value] then seen[value] = true; values[#values + 1] = value end
  end
  table.sort(values)
  local straight = false
  if #values >= 5 then
    for i = 1, #values - 4 do
      if values[i + 4] - values[i] == 4 then straight = true; break end
    end
    if not straight and seen[14] and seen[2] and seen[3] and seen[4] and seen[5] then straight = true end
  end
  if straight and flush then return "Straight Flush" end
  if groups[1] == 5 then return "Five of a Kind" end
  if groups[1] == 4 then return "Four of a Kind" end
  if groups[1] == 3 and groups[2] == 2 then return "Full House" end
  if flush then return "Flush" end
  if straight then return "Straight" end
  if groups[1] == 3 then return "Three of a Kind" end
  if groups[1] == 2 and groups[2] == 2 then return "Two Pair" end
  if groups[1] == 2 then return "Pair" end
  return "High Card"
end

local function build_state_json(actual)
  reset_hand_history_if_needed()
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

  local played_hands = current_round_hand_history()
  local unique_played, seen_played = {}, {}
  for _, name in ipairs(played_hands) do
    if not seen_played[name] then seen_played[name] = true; unique_played[#unique_played + 1] = name end
  end
  parts[#parts + 1] = '"played_hands":' .. jtable(map(unique_played, jstr))
  parts[#parts + 1] = '"most_played_hand":' .. jstr(run_most_played_hand())

  local deck_count = (G.deck and G.deck.cards and #G.deck.cards) or 0
  local discard_count = (G.discard and G.discard.cards and #G.discard.cards) or 0
  local joker_limit = (G.jokers and G.jokers.config and G.jokers.config.card_limit) or 5
  parts[#parts + 1] = '"deck_count":' .. jstr(deck_count)
  parts[#parts + 1] = '"discard_count":' .. jstr(discard_count)
  parts[#parts + 1] = '"joker_slots":' .. jstr(joker_limit)
  parts[#parts + 1] = '"jokers_count":' .. jstr(#jokers)

  local shop_cards = area_cards(G.shop_jokers)
  local booster_cards = area_cards(G.shop_booster)
  local voucher_cards = area_cards(G.shop_vouchers)
  local reroll_cost = (G.GAME and G.GAME.current_round and G.GAME.current_round.reroll_cost) or 5
  local shop_parts = {
    '"cards":' .. jtable(map(shop_cards, shop_card_json)),
    '"boosters":' .. jtable(map(booster_cards, shop_card_json)),
    '"vouchers":' .. jtable(map(voucher_cards, shop_card_json)),
    '"reroll_cost":' .. jstr(reroll_cost)
  }
  parts[#parts + 1] = '"shop":{' .. table.concat(shop_parts, ",") .. '}'

  local state_str = "UNKNOWN"
  if G.STATE and G.STATES then
    for k, v in pairs(G.STATES) do
      if G.STATE == v then state_str = k break end
    end
  end
  if G.GAME and G.GAME.won then
    state_str = "GAME_WON"
  end
  parts[#parts + 1] = '"state_name":' .. jstr(state_str)
  parts[#parts + 1] = '"actual":' .. (actual and jmap(actual) or "null")

  local now = (love.timer and love.timer.getTime()) or os.clock()
  local eval_elapsed = _G._round_eval_enter_time and (now - _G._round_eval_enter_time) or 0
  local has_cash_btn = (find_cash_out_button() ~= nil)
  local can_cash_out = has_cash_btn or (G.STATE == G.STATES.ROUND_EVAL and eval_elapsed >= 5.0)
  parts[#parts + 1] = '"can_cash_out":' .. (can_cash_out and "true" or "false")

  local can_toggle_shop = false
  if G.STATE and G.STATES and G.STATE == G.STATES.SHOP and G.shop then
    if find_next_round_button() then
      local shop_cards_count = (G.shop_jokers and G.shop_jokers.cards and #G.shop_jokers.cards) or 0
      local shop_boosters_count = (G.shop_booster and G.shop_booster.cards and #G.shop_booster.cards) or 0
      local now = (love.timer and love.timer.getTime()) or os.clock()
      if not _G._shop_enter_time then _G._shop_enter_time = now end
      if (shop_cards_count > 0 or shop_boosters_count > 0 or (now - _G._shop_enter_time >= 0.8)) then
        can_toggle_shop = true
      end
    else
      _G._shop_enter_time = nil
    end
  else
    _G._shop_enter_time = nil
  end
  parts[#parts + 1] = '"can_toggle_shop":' .. (can_toggle_shop and "true" or "false")

  local can_select_blind = not not (find_select_blind_button())
  parts[#parts + 1] = '"can_select_blind":' .. (can_select_blind and "true" or "false")

  local can_skip_blind = not not (find_skip_blind_button())
  parts[#parts + 1] = '"can_skip_blind":' .. (can_skip_blind and "true" or "false")

  local tag_info = get_blind_tag_info()
  if tag_info then
    parts[#parts + 1] = '"blind_tag":{"name":' .. jstr(tag_info.name) .. ',"key":' .. jstr(tag_info.key) .. '}'
  else
    parts[#parts + 1] = '"blind_tag":null'
  end

  local is_pack_state = false
  if G.STATE and G.STATES then
    is_pack_state = (G.STATE == G.STATES.TAROT_PACK or G.STATE == G.STATES.PLANET_PACK or 
                     G.STATE == G.STATES.SPECTRAL_PACK or G.STATE == G.STATES.STANDARD_PACK or 
                     G.STATE == G.STATES.BUFFOON_PACK)
  end
  local pack_cards = is_pack_state and area_cards(G.pack_cards) or {}
  parts[#parts + 1] = '"pack_cards":' .. jtable(map(pack_cards, shop_card_json))
  local can_skip_booster = not not (is_pack_state and (find_uie_by_id('skip_booster') or (G.FUNCS and G.FUNCS.can_skip_booster)))
  parts[#parts + 1] = '"can_skip_booster":' .. (can_skip_booster and "true" or "false")

  local won = (G.GAME and G.GAME.won) or false
  parts[#parts + 1] = '"won":' .. (won and "true" or "false")

  return "{" .. table.concat(parts, ",") .. "}"
end

local LAST_STATE_NUM = nil
local LAST_HAND_SIG = ""
local LAST_SHOP_SIG = ""
local LAST_BLIND_SIG = ""
local LAST_EVAL_SIG = ""
local LAST_PACK_SIG = ""

local function check_and_emit_state()
  if not G or not G.STATE or not G.STATES then return end

  local state_changed = (G.STATE ~= LAST_STATE_NUM)
  if state_changed then
    LAST_STATE_NUM = G.STATE
  end

  if G.STATE == G.STATES.SELECTING_HAND then
    if not G.hand or not G.hand.cards then return end
    local hand = G.hand.cards
    local sig_parts = {}
    for i, c in ipairs(hand) do
      local r = c.base and c.base.value or ""
      local s = c.base and c.base.suit or ""
      local enh = (c.ability and c.ability.name) or ""
      local ed = (c.edition and type(c.edition) == "table" and (c.edition.foil and "f" or c.edition.holo and "h" or c.edition.polychrome and "p")) or ""
      local seal = tostring(c.seal or "")
      sig_parts[#sig_parts + 1] = r .. ":" .. s .. ":" .. enh .. ":" .. ed .. ":" .. seal .. ":" .. (c.highlighted and "1" or "0")
    end

    -- Add jokers signature so selling, acquiring or debuffing jokers immediately triggers update
    local jokers = (G.jokers and G.jokers.cards) or {}
    for i, j in ipairs(jokers) do
      local jname = (j.ability and j.ability.name) or ""
      local jmult = tostring(j.ability and j.ability.mult or 0)
      local jdebuff = j.debuff and "1" or "0"
      sig_parts[#sig_parts + 1] = "J" .. i .. ":" .. jname .. ":" .. jmult .. ":" .. jdebuff
    end

    -- Add consumables count signature
    local cons = (G.consumeables and G.consumeables.cards) or {}
    sig_parts[#sig_parts + 1] = "C" .. tostring(#cons)

    local cr = G.GAME and G.GAME.current_round or {}
    sig_parts[#sig_parts + 1] = "H" .. tostring(cr.hands_left or 0)
    sig_parts[#sig_parts + 1] = "D" .. tostring(cr.discards_left or 0)
    sig_parts[#sig_parts + 1] = "S" .. tostring(G.GAME and G.GAME.chips or 0)
    sig_parts[#sig_parts + 1] = "M" .. tostring(G.GAME and G.GAME.dollars or 0)
    local history = current_round_hand_history()
    table.sort(history)
    sig_parts[#sig_parts + 1] = "P" .. table.concat(history, ",")
    sig_parts[#sig_parts + 1] = "R" .. tostring(run_most_played_hand() or "")

    local cur_sig = table.concat(sig_parts, "|")
    if cur_sig ~= LAST_HAND_SIG or state_changed then
      LAST_HAND_SIG = cur_sig
      local ok, json = pcall(build_state_json, nil)
      if ok and json then
        print("[DECIDEX_STATE] " .. json)
      else
        print("[DecideX State Build Error]: " .. tostring(json))
      end
    end
  elseif G.STATE == G.STATES.SHOP then
    G.cashing_out = false
    local shop_cards = (G.shop_jokers and G.shop_jokers.cards) or {}
    local booster_cards = (G.shop_booster and G.shop_booster.cards) or {}
    local voucher_cards = (G.shop_vouchers and G.shop_vouchers.cards) or {}
    local jokers = (G.jokers and G.jokers.cards) or {}
    local dollars = (G.GAME and G.GAME.dollars) or 0
    local reroll = (G.GAME and G.GAME.current_round and G.GAME.current_round.reroll_cost) or 5
    local locked = (G.CONTROLLER and G.CONTROLLER.locks and G.CONTROLLER.locks.toggle_shop) and "1" or "0"
    local can_toggle = (find_next_round_button() ~= nil) and "1" or "0"
    local shop_items_sig = {}
    for _, group in ipairs({shop_cards, booster_cards, voucher_cards}) do
      for _, card in ipairs(group) do
        local key = card.config and card.config.center and card.config.center.key or ""
        local name = card.ability and card.ability.name or ""
        local set = card.ability and card.ability.set or ""
        local cost = tonumber(card.cost or 0) or 0
        local extra = serialize_extra(card.ability and card.ability.extra)
        local edition = card.edition and table.concat(map({"foil", "holo", "polychrome"}, function(k)
          return card.edition[k] and k or ""
        end), ",") or ""
        shop_items_sig[#shop_items_sig + 1] = table.concat({key, name, set, tostring(cost), tostring(extra), edition}, ":")
      end
    end
    local cur_shop_sig = tostring(dollars) .. ":" .. tostring(reroll) .. ":" .. table.concat(shop_items_sig, "|") .. ":" .. tostring(#jokers) .. ":" .. can_toggle .. ":" .. locked
    if cur_shop_sig ~= LAST_SHOP_SIG or state_changed then
      LAST_SHOP_SIG = cur_shop_sig
      local ok, json = pcall(build_state_json, nil)
      if ok and json then
        print("[DECIDEX_STATE] " .. json)
      else
        print("[DecideX State Build Error]: " .. tostring(json))
      end
    end
  elseif G.STATE == G.STATES.BLIND_SELECT then
    G.toggling_shop = false
    G.cashing_out = false
    local btn_ready = (find_select_blind_button() ~= nil)
    local deck = (G.GAME and (G.GAME.blind_on_deck or (G.GAME.round_resets and G.GAME.round_resets.blind_on_deck))) or ""
    local facing = (G.GAME and G.GAME.facing_blind) and "1" or "0"
    local blind_sig = (btn_ready and "1" or "0") .. ":" .. tostring(deck) .. ":" .. facing
    if blind_sig ~= LAST_BLIND_SIG or state_changed then
      LAST_BLIND_SIG = blind_sig
      local ok, json = pcall(build_state_json, nil)
      if ok and json then
        print("[DECIDEX_STATE] " .. json)
      else
        print("[DecideX State Build Error]: " .. tostring(json))
      end
    end
  elseif G.STATE == G.STATES.ROUND_EVAL then
    local now = (love.timer and love.timer.getTime()) or os.clock()
    if not _G._round_eval_enter_time then _G._round_eval_enter_time = now end
    if state_changed then
      G.cashing_out = false
      _G._round_eval_enter_time = now
    end
    if G.cashing_out and _G._last_cash_out_time and (now - _G._last_cash_out_time > 1.8) then
      G.cashing_out = false
    end
    if not G.round_eval or G.round_eval.REMOVED then
      if now - _G._round_eval_enter_time > 1.0 then
        force_advance_to_shop()
      end
    elseif now - _G._round_eval_enter_time > 6.0 then
      force_advance_to_shop()
    end
    local can_cash = (find_cash_out_button() ~= nil) or (_G._round_eval_enter_time and (now - _G._round_eval_enter_time > 5.0))
    local eval_sig = can_cash and "1" or "0"
    if eval_sig ~= LAST_EVAL_SIG or state_changed then
      LAST_EVAL_SIG = eval_sig
      local ok, json = pcall(build_state_json, nil)
      if ok and json then
        print("[DECIDEX_STATE] " .. json)
      else
        print("[DecideX State Build Error]: " .. tostring(json))
      end
    end
  elseif G.STATE == G.STATES.TAROT_PACK or G.STATE == G.STATES.PLANET_PACK or 
         G.STATE == G.STATES.SPECTRAL_PACK or G.STATE == G.STATES.STANDARD_PACK or 
         G.STATE == G.STATES.BUFFOON_PACK then
    local p_cards = (G.pack_cards and G.pack_cards.cards) or {}
    local pack_sig_parts = { tostring(#p_cards) }
    for i, c in ipairs(p_cards) do
      local cname = (c.ability and c.ability.name) or ""
      pack_sig_parts[#pack_sig_parts + 1] = i .. ":" .. cname
    end
    local cur_pack_sig = table.concat(pack_sig_parts, "|")
    if cur_pack_sig ~= LAST_PACK_SIG or state_changed then
      LAST_PACK_SIG = cur_pack_sig
      local ok, json = pcall(build_state_json, nil)
      if ok and json then
        print("[DECIDEX_STATE] " .. json)
      else
        print("[DecideX State Build Error]: " .. tostring(json))
      end
    end
  elseif state_changed then
    local ok, json = pcall(build_state_json, nil)
    if ok and json then
      print("[DECIDEX_STATE] " .. json)
    else
      print("[DecideX State Build Error]: " .. tostring(json))
    end
  end
end

-- Programmatic execution of actions
local function decidex_execute_inner(action, args)
  if not G then return false, "no G" end
  args = args or {}

  if action == "play" or action == "discard" then
    if not (G and G.STATES and G.STATE == G.STATES.SELECTING_HAND) then
      return false, "not in SELECTING_HAND"
    end
    if G.play and G.play.cards and #G.play.cards > 0 then
      return false, "cards already in play"
    end
    if not (G.hand and G.hand.cards and #G.hand.cards > 0) then
      return false, "no hand cards"
    end

    local now = (love.timer and love.timer.getTime()) or os.clock()
    if _G._last_hand_action_time and (now - _G._last_hand_action_time < 0.25) then
      return false, "hand action debounced"
    end

    -- Select specified cards if provided
    if args.indices then
      if G.hand.unhighlight_all then
        G.hand:unhighlight_all()
      else
        for _, c in ipairs(G.hand.cards) do c.highlighted = false end
        G.hand.highlighted = {}
      end
      for _, idx in ipairs(args.indices) do
        local c = G.hand.cards[idx]
        if c then
          if G.hand.add_to_highlighted then
            G.hand:add_to_highlighted(c, true)
          else
            c.highlighted = true
            table.insert(G.hand.highlighted, c)
          end
        end
      end
      if G.hand.parse_highlighted then
        pcall(function() G.hand:parse_highlighted() end)
      end
    end

    local num_hl = (G.hand and G.hand.highlighted and #G.hand.highlighted) or 0
    if num_hl <= 0 then
      return false, "no cards highlighted"
    end
    if num_hl > 5 then
      return false, "too many cards highlighted"
    end

    if action == "play" then
      if G.GAME and G.GAME.blind and G.GAME.blind.block_play then
        return false, "blind blocks play"
      end
      if G.GAME and G.GAME.current_round and G.GAME.current_round.hands_left and G.GAME.current_round.hands_left <= 0 then
        return false, "no hands left"
      end
      if G.FUNCS and G.FUNCS.play_cards_from_highlighted then
        local selected = {}
        for _, card in ipairs(G.hand.highlighted or {}) do selected[#selected + 1] = card end
        _G._last_hand_action_time = now
        local ok, err = pcall(G.FUNCS.play_cards_from_highlighted, {})
        if ok then
          record_hand_type(classify_selected_hand(selected))
          return true, "played"
        else
          return false, "play error: " .. tostring(err)
        end
      end
    elseif action == "discard" then
      if G.GAME and G.GAME.current_round and G.GAME.current_round.discards_left and G.GAME.current_round.discards_left <= 0 then
        return false, "no discards left"
      end
      if G.FUNCS and G.FUNCS.discard_cards_from_highlighted then
        _G._last_hand_action_time = now
        local ok, err = pcall(G.FUNCS.discard_cards_from_highlighted, {})
        if ok then
          return true, "discarded"
        else
          return false, "discard error: " .. tostring(err)
        end
      end
    end
    return false, "action failed"
  end

  if action == "sort_rank" then
    if G.FUNCS and G.FUNCS.sort_hand_value then
      G.FUNCS.sort_hand_value()
      return true, "sorted by rank"
    end
    return false, "no sort_hand_value"
  end

  if action == "sort_suit" then
    if G.FUNCS and G.FUNCS.sort_hand_suit then
      G.FUNCS.sort_hand_suit()
      return true, "sorted by suit"
    end
    return false, "no sort_hand_suit"
  end

  if action == "select_blind" then
    if not (G and G.STATES and G.STATE == G.STATES.BLIND_SELECT) then
      return false, "not in BLIND_SELECT"
    end
    if G.GAME and G.GAME.facing_blind then
      return false, "blind already selected (animating)"
    end
    local now = (love.timer and love.timer.getTime()) or os.clock()
    if _G._last_select_blind_time and (now - _G._last_select_blind_time < 0.8) then
      return false, "select_blind debounced"
    end
    local btn, on_deck = find_select_blind_button()
    if not btn then
      return false, "blind_select UI not ready"
    end
    _G._last_select_blind_time = now

    -- Ensure blind_prompt_box and blind_select are fully crash-proof for G.E_MANAGER animations
    if G.blind_prompt_box then
      if not G.blind_prompt_box.alignment then
        G.blind_prompt_box.alignment = { offset = { y = 0, x = 0 } }
      elseif not G.blind_prompt_box.alignment.offset then
        G.blind_prompt_box.alignment.offset = { y = 0, x = 0 }
      end
      if G.blind_prompt_box.get_UIE_by_ID then
        local orig_get_uie = G.blind_prompt_box.get_UIE_by_ID
        G.blind_prompt_box.get_UIE_by_ID = function(self, id)
          local uie = orig_get_uie(self, id)
          if not uie then
            uie = { config = { object = { pop_delay = 0, pop_out = function() end } } }
          elseif uie.config and not uie.config.object then
            uie.config.object = { pop_delay = 0, pop_out = function() end }
          end
          return uie
        end
      end
    end
    if G.blind_select then
      if not G.blind_select.alignment then
        G.blind_select.alignment = { offset = { y = 0, x = 0 } }
      elseif not G.blind_select.alignment.offset then
        G.blind_select.alignment.offset = { y = 0, x = 0 }
      end
    end

    if G.FUNCS and G.FUNCS.select_blind then
      local prev_facing = G.GAME and G.GAME.facing_blind
      local ok, err = pcall(G.FUNCS.select_blind, btn)
      if not ok then
        if G.GAME then G.GAME.facing_blind = prev_facing end
        return false, "select_blind error: " .. tostring(err)
      end
      return true, "selected blind " .. tostring(on_deck)
    end
    return false, "no select_blind function"
  end

  if action == "skip_blind" then
    if not (G and G.STATES and G.STATE == G.STATES.BLIND_SELECT) then
      return false, "not in BLIND_SELECT"
    end
    if G.GAME and G.GAME.facing_blind then
      return false, "blind already selected (animating)"
    end
    local btn, on_deck = find_skip_blind_button()
    if not btn then
      return false, "skip_blind button not found (boss or not ready)"
    end
    if G.FUNCS and G.FUNCS.skip_blind then
      local ok, err = pcall(G.FUNCS.skip_blind, btn)
      if not ok then
        return false, "skip_blind error: " .. tostring(err)
      end
      return true, "skipped blind " .. tostring(on_deck)
    end
    return false, "no skip_blind function"
  end

  if action == "buy_card" or action == "buy_from_shop" then
    if G.STATES and G.STATE ~= G.STATES.SHOP then
      return false, "not in SHOP"
    end
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
    local idx = tonumber(args.index) or 1
    local c = area.cards[idx]
    if not c then return false, "card not found at index " .. tostring(idx) end
    if G.FUNCS and G.FUNCS.buy_from_shop then
      G.FUNCS.buy_from_shop({config = {ref_table = c}})
      return true, "bought card"
    end
    return false, "no buy_from_shop func"
  end

  if action == "reroll_shop" then
    if G.STATES and G.STATE ~= G.STATES.SHOP then
      return false, "not in SHOP"
    end
    if G.FUNCS and G.FUNCS.reroll_shop then
      local btn = find_uie_by_id('reroll_shop_button') or { config = {} }
      local ok, err = pcall(G.FUNCS.reroll_shop, btn)
      return ok, tostring(err or "rerolled shop")
    end
    return false, "no reroll_shop func"
  end

  if action == "toggle_shop" then
    if G.STATES and G.STATE ~= G.STATES.SHOP then
      if G.STATES and G.STATE == G.STATES.BLIND_SELECT then
        return true, "already in BLIND_SELECT"
      end
      return false, "not in SHOP"
    end
    if not (G and G.shop and not G.shop.REMOVED) then
      G.STATE = G.STATES.BLIND_SELECT
      G.STATE_COMPLETE = false
      if G.CONTROLLER and G.CONTROLLER.locks then
        G.CONTROLLER.locks.toggle_shop = nil
      end
      return true, "shop already removed, transitioned to BLIND_SELECT"
    end
    local now = (love.timer and love.timer.getTime()) or os.clock()
    if G.CONTROLLER and G.CONTROLLER.locks and G.CONTROLLER.locks.toggle_shop then
      if not _G._toggle_shop_lock_time then _G._toggle_shop_lock_time = now end
      if (now - _G._toggle_shop_lock_time > 2.0) then
        G.CONTROLLER.locks.toggle_shop = nil
        _G._toggle_shop_lock_time = nil
      else
        return false, "toggle_shop locked"
      end
    else
      _G._toggle_shop_lock_time = nil
    end
    if _G._last_toggle_shop_time and (now - _G._last_toggle_shop_time < 0.6) then
      return false, "toggle_shop debounced"
    end
    local btn = find_next_round_button() or { config = { button = 'toggle_shop' } }
    if G.FUNCS and G.FUNCS.toggle_shop then
      _G._last_toggle_shop_time = now
      local ok, err = pcall(G.FUNCS.toggle_shop, btn)
      return ok, tostring(err or "toggled shop")
    end
    return false, "no toggle_shop func"
  end

  if action == "cash_out" then
    if G.STATES and G.STATE ~= G.STATES.ROUND_EVAL and G.STATE ~= G.STATES.HAND_PLAYED then
      return false, "not in ROUND_EVAL"
    end
    if G.GAME and G.GAME.won then
      return false, "game already won"
    end
    local now = (love.timer and love.timer.getTime()) or os.clock()
    if G.cashing_out and _G._last_cash_out_time and (now - _G._last_cash_out_time > 1.8) then
      G.cashing_out = false
    end
    if _G._last_cash_out_time and (now - _G._last_cash_out_time < 0.4) then
      return false, "cash_out debounced"
    end
    _G._last_cash_out_time = now
    local btn = find_cash_out_button()
    if btn and G.FUNCS and G.FUNCS.cash_out and G.round_eval and not G.round_eval.REMOVED then
      G.cashing_out = true
      local ok, err = pcall(G.FUNCS.cash_out, btn)
      if ok then return true, "cashed out via G.FUNCS.cash_out" end
    end
    local eval_elapsed = _G._round_eval_enter_time and (now - _G._round_eval_enter_time) or 0
    if eval_elapsed > 4.5 or (not G.round_eval or G.round_eval.REMOVED) then
      force_advance_to_shop()
      return true, "force advanced to shop after timeout"
    end
    return false, "cash_out button not ready yet"
  end

  if action == "continue_endless" then
    if G.FUNCS and G.FUNCS.exit_overlay_menu then
      local ok, err = pcall(G.FUNCS.exit_overlay_menu)
      return ok, tostring(err or "continued endless")
    end
    return false, "no exit_overlay_menu func"
  end

  if action == "skip_booster" then
    if G.FUNCS and G.FUNCS.skip_booster then
      local btn = find_uie_by_id('skip_booster') or { config = { button = 'skip_booster' } }
      local ok, err = pcall(G.FUNCS.skip_booster, btn)
      return ok, tostring(err or "skipped booster")
    end
    return false, "no skip_booster func"
  end

  local function prepare_and_highlight_targets(c, target_indices)
    if not (target_indices and G.hand and G.hand.cards) then return true end
    if G.hand.unhighlight_all then G.hand:unhighlight_all() end
    local is_zero_based = false
    for _, idx in ipairs(target_indices) do
      if idx == 0 then is_zero_based = true break end
    end
    local selected_cards = {}
    for _, t_idx in ipairs(target_indices) do
      local lua_idx = is_zero_based and (t_idx + 1) or t_idx
      local tc = G.hand.cards[lua_idx]
      if tc then
        table.insert(selected_cards, tc)
      end
    end

    -- Guarantee for Death (Tarot: converts LEFT card into RIGHT card)
    -- target_indices[1] is victim (to be overwritten), target_indices[2] is donor (to be cloned)
    local card_name = (c.ability and c.ability.name) or c.name or ""
    if (card_name == "Death" or (c.label and string.find(c.label, "Death"))) and #selected_cards == 2 then
      local victim = selected_cards[1]
      local donor = selected_cards[2]
      local v_idx, d_idx
      for k, card in ipairs(G.hand.cards) do
        if card == victim then v_idx = k end
        if card == donor then d_idx = k end
      end
      if v_idx and d_idx and v_idx > d_idx then
        -- Swap them in G.hand.cards so victim is to the left of donor!
        G.hand.cards[v_idx], G.hand.cards[d_idx] = G.hand.cards[d_idx], G.hand.cards[v_idx]
        if G.hand.set_ranks then pcall(function() G.hand:set_ranks() end) end
        if G.hand.align_cards then pcall(function() G.hand:align_cards() end) end
        if victim.T and donor.T and victim.T.x >= donor.T.x then
          victim.T.x, donor.T.x = donor.T.x - 0.1, donor.T.x
        end
      end
    end

    for _, tc in ipairs(selected_cards) do
      if G.hand.add_to_highlighted then G.hand:add_to_highlighted(tc, true) else tc.highlighted = true end
    end
    return true
  end

  if action == "select_pack_card" then
    if not (G.pack_cards and G.pack_cards.cards) then
      return false, "no pack_cards"
    end
    local idx = tonumber(args.index) or 1
    local c = G.pack_cards.cards[idx]
    if not c then return false, "pack card not found at index " .. tostring(idx) end

    -- Check if card is a Joker and whether slots are available
    if c.ability and c.ability.set == "Joker" and G.jokers and G.jokers.cards then
      local max_jokers = (G.jokers.config and G.jokers.config.card_limit) or 5
      if #G.jokers.cards >= max_jokers and not (c.edition and c.edition.negative) then
        return false, "cannot select joker: joker slots full"
      end
    end

    -- Target selection for targeted tarots/spectrals inside booster pack
    if args.target_indices then
      prepare_and_highlight_targets(c, args.target_indices)
    end

    if G.FUNCS and G.FUNCS.use_card then
      local ok, err = pcall(G.FUNCS.use_card, {config = {ref_table = c}})
      return ok, tostring(err or "selected pack card")
    end
    return false, "no use_card func for pack card"
  end

  if action == "use_card" or action == "sell_card" then
    local area_name = args.area or "consumeables"
    local area = G.consumeables
    if area_name == "jokers" or area_name == "joker" then
      area = G.jokers
    elseif area_name == "pack_cards" or area_name == "pack" then
      area = G.pack_cards
    elseif G[area_name] then
      area = G[area_name]
    end
    if not (area and area.cards) then return false, "area not found: " .. tostring(area_name) end
    local idx = tonumber(args.index) or 1
    local c = area.cards[idx]
    if not c then return false, "card not found at index " .. tostring(idx) end
    if action == "use_card" and G.FUNCS and G.FUNCS.use_card then
      if args.target_indices then
        prepare_and_highlight_targets(c, args.target_indices)
      end
      local ok, err = pcall(G.FUNCS.use_card, {config = {ref_table = c}})
      return ok, tostring(err or "used card")
    elseif action == "sell_card" and G.FUNCS and G.FUNCS.sell_card then
      local ok, err = pcall(G.FUNCS.sell_card, {config = {ref_table = c}})
      return ok, tostring(err or "sold card")
    end
    return false, "no use/sell card func"
  end

  if action == "reorder_jokers" then
    if not (G.jokers and G.jokers.cards) then
      return false, "no G.jokers"
    end
    local order = args and args.order
    if not order or #order == 0 then
      return false, "no order provided"
    end
    local is_zero_based = false
    for _, idx in ipairs(order) do
      if idx == 0 then is_zero_based = true break end
    end
    local new_cards = {}
    for _, idx in ipairs(order) do
      local lua_idx = is_zero_based and (idx + 1) or idx
      local card = G.jokers.cards[lua_idx]
      if card then
        table.insert(new_cards, card)
      end
    end
    if #new_cards == #G.jokers.cards then
      G.jokers.cards = new_cards
      if G.jokers.set_ranks then pcall(function() G.jokers:set_ranks() end) end
      if G.jokers.align_cards then pcall(function() G.jokers:align_cards() end) end
      return true, "reordered jokers"
    end
    return false, "mismatched joker card count"
  end

  if action == "start_run" then
    if G then
      G.F_SKIP_TUTORIAL = true
      if G.SETTINGS then
        G.SETTINGS.tutorial_complete = true
        G.SETTINGS.tutorial_progress = nil
      end
    end
    local now = (love.timer and love.timer.getTime()) or os.clock()
    if _G._last_start_run_time and (now - _G._last_start_run_time < 3.0) then
      return false, "start_run already in progress"
    end
    _G._last_start_run_time = now

    if G.STAGE and G.STAGES and G.STAGE == G.STAGES.RUN and G.STATE ~= G.STATES.SPLASH and G.STATE ~= G.STATES.MENU then
      return false, "already in run"
    end

    if G.FUNCS and G.FUNCS.start_run then
      local ok, err = pcall(G.FUNCS.start_run, nil, {stake = 1})
      return ok, tostring(err or "started run")
    end
    return false, "no start_run"
  end

  if action == "force_state_dump" then
    LAST_HAND_SIG = ""
    LAST_STATE_NUM = nil
    check_and_emit_state()
    return true, "dumped"
  end

  return false, "unknown action: " .. tostring(action)
end

local function decidex_execute(action, args)
  local ok, info = decidex_execute_inner(action, args)
  _G._decidex_last_action_ok = not not ok
  _G._decidex_last_action_info = tostring(info or "")
  print(string.format("[DECIDEX_ACTION_RESULT] action=%s ok=%s info=%s", tostring(action), tostring(ok), tostring(info)))
  return ok, info
end

local function write_action_ack(sequence, ok, info)
  if not sequence then return end
  local safe_info = tostring(info or ""):gsub("[\r\n|]", " ")
  local f = io.open("/tmp/balatro_eval_ack.txt", "w")
  if f then
    f:write(tostring(sequence), "|", ok and "ok" or "error", "|", safe_info)
    f:close()
  else
    print("[DecideX Ack Error]: unable to open acknowledgement file")
  end
end

_G.DECIDEX_EXECUTE = decidex_execute
rawset(_G, "DECIDEX_EXECUTE", decidex_execute)

local function decidex_step(dt)
  if G and not G.DECIDEX_EXECUTE then
    G.DECIDEX_EXECUTE = decidex_execute
  end
  if G and G.SETTINGS and G.SETTINGS.GAMESPEED and G.SETTINGS.GAMESPEED < 4 then
    G.SETTINGS.GAMESPEED = 4
  end

  -- Auto-suppress tutorial popups, locks and pauses, and disable heavy synchronous saves
  if G then
    G.F_SKIP_TUTORIAL = true
    G.F_NO_SAVING = true
    if G.FUNCS and not G.FUNCS._tutorial_part_patched then
      G.FUNCS._tutorial_part_patched = true
      G.FUNCS.tutorial_part = function(_part)
        if G.SETTINGS then
          G.SETTINGS.tutorial_complete = true
          G.SETTINGS.paused = false
          if G.SETTINGS.tutorial_progress then
            G.SETTINGS.tutorial_progress.completed_parts = G.SETTINGS.tutorial_progress.completed_parts or {}
            G.SETTINGS.tutorial_progress.completed_parts[_part] = true
          end
        end
        return 1
      end
    end
    if G.SETTINGS then
      G.SETTINGS.tutorial_complete = true
      if G.SETTINGS.paused and (G.OVERLAY_TUTORIAL or not G.OVERLAY_MENU) then
        G.SETTINGS.paused = false
      end
      if G.SETTINGS.tutorial_progress then
        G.SETTINGS.tutorial_progress.completed_parts = G.SETTINGS.tutorial_progress.completed_parts or {}
        local t_parts = {'first_hand', 'first_hand_2', 'first_hand_3', 'first_hand_4', 'first_hand_section', 'small_blind', 'big_blind', 'second_hand', 'shop_1'}
        for _, part in ipairs(t_parts) do
          G.SETTINGS.tutorial_progress.completed_parts[part] = true
        end
      end
    end
    if G.OVERLAY_TUTORIAL then
      if G.OVERLAY_TUTORIAL.Jimbo then pcall(function() G.OVERLAY_TUTORIAL.Jimbo:remove() end) end
      if G.OVERLAY_TUTORIAL.content then pcall(function() G.OVERLAY_TUTORIAL.content:remove() end) end
      pcall(function() G.OVERLAY_TUTORIAL:remove() end)
      G.OVERLAY_TUTORIAL = nil
      if G.E_MANAGER and G.E_MANAGER.clear_queue then
        pcall(function() G.E_MANAGER:clear_queue('tutorial') end)
      end
      if G.SETTINGS then G.SETTINGS.paused = false end
    end
  end

  -- Defensive cycle & depth guard for UIBox traversal in WASM
  if not _G._uibox_guard_installed and UIBox and UIBox.get_UIE_by_ID then
    _G._uibox_guard_installed = true
    local orig_get_uie = UIBox.get_UIE_by_ID
    local function safe_get_uie(self, id, node, depth, visited)
      depth = depth or 0
      if depth > 10 then return nil end
      visited = visited or {}
      node = node or self.UIRoot
      if not node or visited[node] then return nil end
      visited[node] = true
      if node.config and node.config.id == id then return node end
      if node.children then
        for _, v in pairs(node.children) do
          local res = safe_get_uie(self, id, v, depth + 1, visited)
          if res then return res end
          if v.config and v.config.object and v.config.object.get_UIE_by_ID and not visited[v.config.object] then
            visited[v.config.object] = true
            res = safe_get_uie(v.config.object, id, nil, depth + 1, visited)
            if res then return res end
          end
        end
      end
      return nil
    end
    UIBox.get_UIE_by_ID = safe_get_uie
    print("[DecideX Web] UIBox:get_UIE_by_ID cycle/depth guard installed")
  end

  if not _G._decidex_step_logged then
    _G._decidex_step_logged = true
    print("[DecideX Web] DECIDEX_STEP actively ticking in game loop")
  end

  -- 1. Check for incoming commands from JS via virtual FS
  local f = io.open("/tmp/balatro_eval.lua", "r")
  if f then
    local code = f:read("*a")
    f:close()
    os.remove("/tmp/balatro_eval.lua")
    if not code or code == "" then
      -- empty file ignored
    else
      local sequence = code:match("^%-%- DECIDEX_SEQ:(%d+)")
      local body = code:gsub("^%-%- DECIDEX_SEQ:%d+%s*\n?", "")
      if not sequence then
        write_action_ack(nil, false, "missing sequence header")
      elseif not body or body == "" then
        write_action_ack(sequence, false, "empty command")
      else
        local chunk, err = loadstring(body or "")
      if not chunk then
        print("[DecideX Bridge Parse Error]: " .. tostring(err))
        write_action_ack(sequence, false, "parse error: " .. tostring(err))
      else
        _G._decidex_last_action_ok = nil
        _G._decidex_last_action_info = nil
        local ok, run_err = pcall(chunk)
        if not ok then
          print("[DecideX Bridge Error]: " .. tostring(run_err))
          write_action_ack(sequence, false, "runtime error: " .. tostring(run_err))
        elseif _G._decidex_last_action_ok == false then
          write_action_ack(sequence, false, _G._decidex_last_action_info)
        elseif _G._decidex_last_action_ok == nil then
          write_action_ack(sequence, false, "command did not report an action result")
        else
          print("[DecideX Executed]: " .. tostring(body:sub(1, 80)))
          write_action_ack(sequence, true, _G._decidex_last_action_info or "ok")
        end
      end
    end
  end
  end

  -- 2. Emit state if changed
  local ok, err = pcall(check_and_emit_state)
  if not ok and not _G._decidex_err_logged then
    _G._decidex_err_logged = true
    print("[DecideX State Error]: " .. tostring(err))
  end
end

_G.DECIDEX_STEP = decidex_step

-- Also hook love.update as fallback
local orig_update = love.update
love.update = function(dt)
  if orig_update then orig_update(dt) end
  decidex_step(dt)
end

print("[DecideX Web] decidex_bridge.lua initialized")
return true
