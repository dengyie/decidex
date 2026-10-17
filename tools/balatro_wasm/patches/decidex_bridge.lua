--[[
  DecideX WASM Bridge for Balatro on love.js
  - Intercepts state changes in SELECTING_HAND and emits [DECIDEX_STATE] JSON via print()
  - Provides G.DECIDEX_EXECUTE for direct in-browser programmatic control
  - Polls /tmp/balatro_eval.lua from Emscripten virtual FS for commands from JS
]]

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
  local parts = {
    '"name":' .. jstr(name),
    '"type":' .. jstr(set:lower()),
    '"index":' .. jstr(idx)
  }
  return "{" .. table.concat(parts, ",") .. "}"
end

local function blind_info()
  local b = G.GAME and G.GAME.blind
  if not b then return { name = "None", type = "small", chips = 0, scored = 0 } end
  local btype = "small"
  if b.name == "Big Blind" or (type(b.key) == "string" and b.key:find("big")) then btype = "big" end
  if b.boss or (type(b.key) == "string" and b.key:find("boss")) then btype = "boss" end
  return {
    name = tostring(b.name or "Blind"),
    type = btype,
    chips = tonumber(b.chips or 0) or 0,
    scored = tonumber(G.GAME and G.GAME.chips or 0) or 0
  }
end

local function area_cards(area)
  if not area or not area.cards then return {} end
  return area.cards
end

local function find_uie_by_id(id)
  if not G then return nil end
  if G.round_eval and G.round_eval.get_UIE_by_ID then
    local res = nil
    pcall(function() res = G.round_eval:get_UIE_by_ID(id) end)
    if res then return res end
  end
  if G.shop and G.shop.get_UIE_by_ID then
    local res = nil
    pcall(function() res = G.shop:get_UIE_by_ID(id) end)
    if res then return res end
  end
  if G.blind_select and G.blind_select.get_UIE_by_ID then
    local res = nil
    pcall(function() res = G.blind_select:get_UIE_by_ID(id) end)
    if res then return res end
  end
  if G.I and G.I.UIBOX then
    for _, uibox in ipairs(G.I.UIBOX) do
      if uibox and uibox.get_UIE_by_ID then
        local res = nil
        pcall(function() res = uibox:get_UIE_by_ID(id) end)
        if res then return res end
      end
    end
  end
  return nil
end

local function build_state_json(actual)
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
  parts[#parts + 1] = '"state_name":' .. jstr(state_str)
  parts[#parts + 1] = '"actual":' .. (actual and jmap(actual) or "null")

  local can_cash_out = false
  if G.STATE and G.STATES and G.STATE == G.STATES.ROUND_EVAL and G.round_eval then
    if find_uie_by_id('cash_out_button') then
      can_cash_out = true
    end
  end
  parts[#parts + 1] = '"can_cash_out":' .. (can_cash_out and "true" or "false")

  local can_toggle_shop = false
  if G.STATE and G.STATES and G.STATE == G.STATES.SHOP and G.shop then
    if find_uie_by_id('next_round_button') then
      can_toggle_shop = true
    end
  end
  parts[#parts + 1] = '"can_toggle_shop":' .. (can_toggle_shop and "true" or "false")

  local can_select_blind = false
  if G.STATE and G.STATES and G.STATE == G.STATES.BLIND_SELECT then
    can_select_blind = true
  end
  parts[#parts + 1] = '"can_select_blind":' .. (can_select_blind and "true" or "false")

  local won = (G.GAME and G.GAME.won) or false
  parts[#parts + 1] = '"won":' .. (won and "true" or "false")

  return "{" .. table.concat(parts, ",") .. "}"
end

local LAST_STATE_NUM = nil
local LAST_HAND_SIG = ""
local LAST_SHOP_SIG = ""
local LAST_BLIND_SIG = ""
local LAST_EVAL_SIG = ""

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
    local shop_cards = (G.shop_jokers and G.shop_jokers.cards) or {}
    local booster_cards = (G.shop_booster and G.shop_booster.cards) or {}
    local voucher_cards = (G.shop_vouchers and G.shop_vouchers.cards) or {}
    local jokers = (G.jokers and G.jokers.cards) or {}
    local dollars = (G.GAME and G.GAME.dollars) or 0
    local reroll = (G.GAME and G.GAME.current_round and G.GAME.current_round.reroll_cost) or 5
    local can_toggle = (find_uie_by_id('next_round_button') ~= nil)
    local cur_shop_sig = tostring(dollars) .. ":" .. tostring(reroll) .. ":" .. tostring(#shop_cards) .. ":" .. tostring(#booster_cards) .. ":" .. tostring(#voucher_cards) .. ":" .. tostring(#jokers) .. ":" .. tostring(can_toggle)
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
    local can_select = (G.blind_select ~= nil)
    local deck = (G.GAME and G.GAME.blind_on_deck) or ""
    local blind_sig = tostring(can_select) .. ":" .. tostring(deck)
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
    local can_cash = (find_uie_by_id('cash_out_button') ~= nil)
    local eval_sig = tostring(can_cash)
    if eval_sig ~= LAST_EVAL_SIG or state_changed then
      LAST_EVAL_SIG = eval_sig
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
local function decidex_execute(action, args)
  if not G then return false, "no G" end
  args = args or {}

  if action == "play" or action == "discard" then
    if G.STATES and G.STATE ~= G.STATES.SELECTING_HAND then
      return false, "not in SELECTING_HAND"
    end
    if not (G.hand and G.hand.cards) then return false, "no hand" end

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
    end

    if action == "play" then
      if G.FUNCS and G.FUNCS.play_cards_from_highlighted then
        G.FUNCS.play_cards_from_highlighted()
        return true, "played"
      end
    elseif action == "discard" then
      if G.FUNCS and G.FUNCS.discard_cards_from_highlighted then
        G.FUNCS.discard_cards_from_highlighted()
        return true, "discarded"
      end
    end
  elseif action == "sort_rank" then
    if G.FUNCS and G.FUNCS.sort_hand_value then
      G.FUNCS.sort_hand_value()
      return true, "sorted by rank"
    end
  elseif action == "sort_suit" then
    if G.FUNCS and G.FUNCS.sort_hand_suit then
      G.FUNCS.sort_hand_suit()
      return true, "sorted by suit"
    end
  elseif action == "select_blind" then
    if G.STATES and G.STATE == G.STATES.BLIND_SELECT and G.FUNCS and G.FUNCS.select_blind then
      local on_deck = (G.GAME and (G.GAME.blind_on_deck or (G.GAME.round_resets and G.GAME.round_resets.blind_on_deck))) or "Small"
      local opt = G.blind_select_opts and G.blind_select_opts[string.lower(on_deck)]
      local btn = opt and opt.get_UIE_by_ID and opt:get_UIE_by_ID('select_blind_button')
      if not btn and G.blind_select then
        pcall(function()
          btn = G.blind_select.UIRoot.children[1].children[1].config.object:get_UIE_by_ID('select_blind_button')
        end)
      end
      if not btn and G.blind_select and G.blind_select.get_UIE_by_ID then
        btn = G.blind_select:get_UIE_by_ID('select_blind_button')
      end
      if not btn then
        btn = find_uie_by_id('select_blind_button')
      end
      if not btn then
        local choice_key = G.GAME and G.GAME.round_resets and G.GAME.round_resets.blind_choices and G.GAME.round_resets.blind_choices[on_deck]
        local pb = choice_key and G.P_BLINDS and G.P_BLINDS[choice_key]
        if not pb and G.P_BLINDS then
          pb = (on_deck == "Big") and G.P_BLINDS.bl_big or (on_deck == "Boss") and G.P_BLINDS.bl_hook or G.P_BLINDS.bl_small
        end
        if pb then
          btn = { config = { id = 'select_blind_button', ref_table = pb } }
        end
      end
      if btn then
        local ok, err = pcall(G.FUNCS.select_blind, btn)
        return ok, tostring(err or "selected blind")
      end
    end
    return false, "no blind_select or button"
  elseif action == "buy_card" or action == "buy_from_shop" then
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
  elseif action == "reroll_shop" then
    if G.STATES and G.STATE ~= G.STATES.SHOP then
      return false, "not in SHOP"
    end
    if G.FUNCS and G.FUNCS.reroll_shop then
      local btn = find_uie_by_id('reroll_shop_button') or { config = {} }
      local ok, err = pcall(G.FUNCS.reroll_shop, btn)
      return ok, tostring(err or "rerolled shop")
    end
    return false, "no reroll_shop func"
  elseif action == "toggle_shop" then
    if G.STATES and G.STATE ~= G.STATES.SHOP then
      return false, "not in SHOP"
    end
    if G.FUNCS and G.FUNCS.toggle_shop then
      local btn = find_uie_by_id('next_round_button') or { config = {} }
      local ok, err = pcall(G.FUNCS.toggle_shop, btn)
      return ok, tostring(err or "toggled shop")
    end
    return false, "no toggle_shop func"
  elseif action == "cash_out" then
    if G.STATES and G.STATE ~= G.STATES.ROUND_EVAL and G.STATE ~= G.STATES.HAND_PLAYED then
      return false, "not in ROUND_EVAL"
    end
    if G.FUNCS and G.FUNCS.cash_out then
      local btn = find_uie_by_id('cash_out_button') or { config = {} }
      local ok, err = pcall(G.FUNCS.cash_out, btn)
      return ok, tostring(err or "cashed out")
    end
    return false, "no cash_out func"
  elseif action == "use_card" or action == "sell_card" then
    local area_name = args.area or "consumeables"
    local area = G.consumeables
    if area_name == "jokers" or area_name == "joker" then
      area = G.jokers
    elseif G[area_name] then
      area = G[area_name]
    end
    if not (area and area.cards) then return false, "area not found: " .. tostring(area_name) end
    local idx = tonumber(args.index) or 1
    local c = area.cards[idx]
    if not c then return false, "card not found at index " .. tostring(idx) end
    if action == "use_card" and G.FUNCS and G.FUNCS.use_card then
      G.FUNCS.use_card({config = {ref_table = c}})
      return true, "used card"
    elseif action == "sell_card" and G.FUNCS and G.FUNCS.sell_card then
      G.FUNCS.sell_card({config = {ref_table = c}})
      return true, "sold card"
    end
    return false, "no use/sell card func"
  elseif action == "start_run" then
    if G.main_menu then
      pcall(function() G:main_menu() end)
    end
    if G.FUNCS and G.FUNCS.start_run then
      local ok, err = pcall(G.FUNCS.start_run, nil, {stake = 1})
      return ok, tostring(err or "started run")
    end
    return false, "no start_run"
  elseif action == "force_state_dump" then
    LAST_HAND_SIG = ""
    LAST_STATE_NUM = nil
    check_and_emit_state()
    return true, "dumped"
  end

  return false, "unknown action: " .. tostring(action)
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
    if code and code ~= "" then
      local chunk, err = loadstring(code)
      if chunk then
        local ok, run_err = pcall(chunk)
        if not ok then
          print("[DecideX Bridge Error]: " .. tostring(run_err))
        end
      else
        print("[DecideX Bridge Parse Error]: " .. tostring(err))
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
