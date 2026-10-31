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

-- Bulletproof EventManager crash guard:
-- Prevents any buggy animation event (e.g. nil screenwipe, deleted UI box, missing particles)
-- from deadlocking G.E_MANAGER.queues.base and halting game progression.
local function install_emanager_crash_guard()
  if Event and not _G._decidex_event_handle_guarded then
    _G._decidex_event_handle_guarded = true
    local orig_event_handle = Event.handle
    Event.handle = function(self, results)
      local orig_func = self.func
      if orig_func and not self._guarded_func then
        self._guarded_func = true
        self.func = function(...)
          local ok, ret = pcall(orig_func, ...)
          if not ok then
            print("[DecideX Event Guard] Dropping crashed event: " .. tostring(ret))
            return true
          end
          return ret
        end
      end
      local ok, err = pcall(function() orig_event_handle(self, results) end)
      if not ok then
        print("[DecideX Event Guard] Event.handle error caught: " .. tostring(err))
        results.completed = true
        results.time_done = true
        self.complete = true
      end
    end
    print("[DecideX Event Guard] Event.handle crash guard installed")
  end
end
pcall(install_emanager_crash_guard)

local SUIT_MAP = { Spades = "S", Hearts = "H", Diamonds = "D", Clubs = "C" }
local RANK_MAP = {
  [11] = "J", [12] = "Q", [13] = "K", [14] = "A",
  ["11"] = "J", ["12"] = "Q", ["13"] = "K", ["14"] = "A",
  ["Jack"] = "J", ["Queen"] = "Q", ["King"] = "K", ["Ace"] = "A"
}
local SEAL_MAP = { Red = "red", Blue = "blue", Gold = "gold", Purple = "purple" }
local ENH_MAP = {
  ["Bonus"] = "bonus", ["Mult"] = "mult", ["Wild"] = "wild",
  ["Glass"] = "glass", ["Steel"] = "steel", ["Gold"] = "gold", ["Lucky"] = "lucky", ["Stone"] = "stone",
  ["m_bonus"] = "bonus", ["m_mult"] = "mult", ["m_wild"] = "wild",
  ["m_glass"] = "glass", ["m_steel"] = "steel", ["m_gold"] = "gold", ["m_lucky"] = "lucky", ["m_stone"] = "stone"
}
local ENHANCEMENT_MAP = ENH_MAP
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
  if not c then return "{}" end
  local ok, res = pcall(function()
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
  end)
  if ok and res then return res end
  local fallback_cost = tonumber(c.cost or 0) or 0
  local fallback_sell = tonumber(c.sell_cost or 0) or 0
  local fallback_idx = tonumber(idx or 0) or 0
  return string.format('{"name":"Joker","mult":0,"xmult":1,"chips":0,"cost":%d,"sell_cost":%d,"debuff":false,"index":%d}', fallback_cost, fallback_sell, fallback_idx)
end

local function shop_card_json(c, idx)
  if not c then return "{}" end
  local ok, res = pcall(function()
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

    local r = (c.base and c.base.value) or (c.ability and c.ability.value) or nil
    local s = (c.base and c.base.suit) or (c.ability and c.ability.suit) or nil
    if r then r = RANK_MAP[tonumber(r)] or RANK_MAP[r] or r end
    if s then s = SUIT_MAP[s] or s end

    local enh = nil
    if c.ability and c.ability.name and ENH_MAP[c.ability.name] then
      enh = ENH_MAP[c.ability.name]
    elseif c.config and c.config.center and c.config.center.key and ENH_MAP[c.config.center.key] then
      enh = ENH_MAP[c.config.center.key]
    end
    local seal = nil
    if c.seal then
      seal = SEAL_MAP[c.seal] or tostring(c.seal):lower()
    end

    local type_str = "joker"
    if type(set) == "string" and set ~= "" then
      type_str = set:lower()
    end

    local parts = {
      '"name":' .. jstr(name),
      '"type":' .. jstr(type_str),
      '"cost":' .. jstr(cost),
      '"rarity":' .. jstr(rarity),
      '"mult":' .. jstr(mult),
      '"xmult":' .. jstr(x_mult),
      '"chips":' .. jstr(chips),
      '"edition":' .. jstr(edition),
      '"rank":' .. jstr(r),
      '"suit":' .. jstr(s),
      '"enhancement":' .. jstr(enh),
      '"seal":' .. jstr(seal),
      '"extra":' .. serialize_extra(c.ability and c.ability.extra),
      '"index":' .. jstr(idx)
    }
    return "{" .. table.concat(parts, ",") .. "}"
  end)
  if ok and res then return res end
  local fallback_cost = tonumber(c.cost or 0) or 0
  local fallback_idx = tonumber(idx or 0) or 0
  return string.format('{"name":"Card","type":"unknown","cost":%d,"index":%d}', fallback_cost, fallback_idx)
end

local function consumable_json(c, idx)
  local name = c.ability and c.ability.name or ""
  local set = c.ability and c.ability.set or ""
  local can_use = false
  if c.can_use_consumeable then
    local ok, res = pcall(function() return c:can_use_consumeable(true, true) end)
    if ok and res then
      can_use = true
    end
  end

  if not can_use then
    -- Targeted cards requiring hand cards to be highlighted
    if c.ability and c.ability.consumeable and c.ability.consumeable.max_highlighted then
      local min_needed = c.ability.consumeable.min_highlighted or 1
      local hand_count = (G.hand and G.hand.cards and #G.hand.cards) or 0
      local valid_state = (G.STATE == G.STATES.SELECTING_HAND or G.STATE == G.STATES.TAROT_PACK or G.STATE == G.STATES.SPECTRAL_PACK or G.STATE == G.STATES.PLANET_PACK)
      if valid_state and hand_count >= min_needed then
        can_use = true
      end
    -- Non-targeted consumables usable across SHOP, BLIND_SELECT, ROUND_EVAL, and SELECTING_HAND
    elseif set:lower() == "planet" or (c.ability and c.ability.consumeable and c.ability.consumeable.hand_type) or name == "Black Hole" then
      can_use = true
    elseif name == "The Hermit" or name == "Temperance" then
      can_use = true
    elseif name == "The Wheel of Fortune" then
      local jokers = (G.jokers and G.jokers.cards) or {}
      for _, j in ipairs(jokers) do
        if not j.edition then can_use = true break end
      end
    elseif name == "The Emperor" or name == "The High Priestess" then
      local max_c = (G.consumeables and G.consumeables.config and G.consumeables.config.card_limit) or 2
      local cur_c = (G.consumeables and G.consumeables.cards and #G.consumeables.cards) or 0
      if cur_c < max_c or (c.area == G.consumeables) then can_use = true end
    elseif name == "Judgement" or name == "The Soul" or name == "Wraith" then
      local max_j = (G.jokers and G.jokers.config and G.jokers.config.card_limit) or 5
      local cur_j = (G.jokers and G.jokers.cards and #G.jokers.cards) or 0
      if cur_j < max_j then can_use = true end
    elseif name == "The Fool" then
      if G.GAME and G.GAME.last_tarot_planet and G.GAME.last_tarot_planet ~= "c_fool" then
        can_use = true
      end
    elseif name == "Ankh" or name == "Hex" then
      local j_count = (G.jokers and G.jokers.cards and #G.jokers.cards) or 0
      if j_count > 0 then can_use = true end
    end
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

-- Name of the game's current state, for diagnostics. A guard that only says
-- "not in BLIND_SELECT" hides which state the game is actually in, which is
-- exactly the information needed to debug a state-machine desync.
local function state_name_of(state_num)
  if not (G and G.STATES) then return "NOSTATE" end
  if state_num == nil then return "NONE" end
  for k, v in pairs(G.STATES) do
    if state_num == v then return tostring(k) end
  end
  return "UNKNOWN(" .. tostring(state_num) .. ")"
end

local function current_state_name()
  if not (G and G.STATE) then return "NOSTATE" end
  return state_name_of(G.STATE)
end

local function find_uie_by_id(id)
  if not G then return nil end
  local roots = { G.round_eval, G.shop, G.blind_select, G.booster_pack }
  for _, root in ipairs(roots) do
    if root and not root.REMOVED and root.get_UIE_by_ID then
      local res = nil
      pcall(function() res = root:get_UIE_by_ID(id) end)
      if res then return res end
    end
  end
  return nil
end

-- Walks a UIBox's element tree looking for a node whose config.button matches.
-- Several game buttons (shop reroll, booster pack skip) are declared with a
-- `button` field but no `id`, so get_UIE_by_ID can never find them.
local function find_button_by_button_name(root, button_name)
  if not root then return nil end
  local function walk(node, depth, visited)
    if not node or depth > 24 then return nil end
    if visited[node] then return nil end
    visited[node] = true
    if node.config and node.config.button == button_name then return node end
    if node.children then
      for _, child in ipairs(node.children) do
        local found = walk(child, depth + 1, visited)
        if found then return found end
      end
    end
    return nil
  end
  local start = root.UIRoot or root
  local found = nil
  pcall(function() found = walk(start, 0, {}) end)
  return found
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

--[[
  True when the game is in the middle of a run, i.e. a state the autopilot
  is actively driving. Modal overlays that the game raises *during* a run
  (unlock popup, achievement card) are safe to dismiss; the game-over screen
  and the run-setup menu are not, and must be left to the autopilot.
]]
local function is_active_run_state()
  if not (G and G.STATES and G.STATE) then return false end
  local S = G.STATES
  return G.STATE == S.SELECTING_HAND or G.STATE == S.DRAW_TO_HAND or
         G.STATE == S.HAND_PLAYED or G.STATE == S.NEW_ROUND or
         G.STATE == S.ROUND_EVAL or G.STATE == S.BLIND_SELECT or
         G.STATE == S.SHOP or G.STATE == S.PLAY_TAROT or
         G.STATE == S.TAROT_PACK or G.STATE == S.PLANET_PACK or
         G.STATE == S.SPECTRAL_PACK or G.STATE == S.STANDARD_PACK or
         G.STATE == S.BUFFOON_PACK
end

--[[
  Dismiss a modal overlay that the game raised on its own.

  The game pauses itself when it shows an unlock/achievement overlay
  (create_unlock_overlay -> G.SETTINGS.paused = true + G.FUNCS.overlay_menu).
  While paused, Game:update sets dt = 0, which freezes G.TIMERS.TOTAL, so
  every pending 'after' event stops firing; and Event:handle returns
  'pause_skip' for events that were created while unpaused, so even
  'immediate' events (the one that builds the booster-pack UIBox) never run.

  The DecideX autopilot never clicks the overlay's "continue" button, so the
  run used to freeze forever whenever a new card centre was discovered
  mid-pack. G.FUNCS.continue_unlock is the game's own dismissal path: it
  closes the overlay, unpauses, and force-flushes the event queue so the next
  unlock overlay can appear.

  Only overlays raised during an active run are dismissed; the game-over
  screen and the run-setup menu are left alone.

  Returns true when an overlay was actually dismissed.
]]
local function dismiss_blocking_overlay()
  if not (G and G.OVERLAY_MENU) then return false end
  if not is_active_run_state() then return false end

  -- The card/deck unlock overlay has a "continue" button that runs
  -- continue_unlock; prefer it so queued unlock overlays keep draining.
  if G.FUNCS and G.FUNCS.continue_unlock then
    local ok = pcall(G.FUNCS.continue_unlock)
    if ok then
      if G.SETTINGS then G.SETTINGS.paused = false end
      return true
    end
  end

  if G.FUNCS and G.FUNCS.exit_overlay_menu then
    local ok = pcall(G.FUNCS.exit_overlay_menu)
    if ok then
      if G.SETTINGS then G.SETTINGS.paused = false end
      return true
    end
  end

  -- Last resort: drop the overlay by hand and unpause.
  pcall(function()
    if G.OVERLAY_MENU and G.OVERLAY_MENU.remove then G.OVERLAY_MENU:remove() end
    G.OVERLAY_MENU = nil
    if G.SETTINGS then G.SETTINGS.paused = false end
  end)
  return true
end

--[[
  True when the current game state is one of the booster-pack states.
]]
local function is_pack_state()
  if not (G and G.STATES and G.STATE) then return false end
  return G.STATE == G.STATES.TAROT_PACK or G.STATE == G.STATES.PLANET_PACK or
         G.STATE == G.STATES.SPECTRAL_PACK or G.STATE == G.STATES.STANDARD_PACK or
         G.STATE == G.STATES.BUFFOON_PACK
end

--[[
  Rebuild the booster-pack UIBox without going through the event queue.

  Game:update_<pack> only creates the pack UI once, behind an 'immediate'
  event, and only while G.STATE_COMPLETE is false. If that event is lost
  (paused overlay, dropped event, queue head-of-line block) the run wedges:
  G.STATE stays on the pack state, G.booster_pack and G.pack_cards stay nil,
  G.STATE_COMPLETE stays true, and the autopilot loops forever.

  The factory functions themselves are pure enough to call directly:
  create_UIBox_buffoon_pack() and friends assign G.pack_cards as their very
  first statement, then return a UIBox definition. We call the factory and
  build the UIBox the same way game.lua does, so G.pack_cards comes back.

  Returns true when a pack UI was rebuilt.
]]
local function rebuild_pack_ui()
  if not is_pack_state() then return false end
  if G.pack_cards and G.booster_pack then return false end

  local factory = nil
  if G.STATE == G.STATES.TAROT_PACK then factory = create_UIBox_arcana_pack
  elseif G.STATE == G.STATES.PLANET_PACK then factory = create_UIBox_celestial_pack
  elseif G.STATE == G.STATES.SPECTRAL_PACK then factory = create_UIBox_spectral_pack
  elseif G.STATE == G.STATES.STANDARD_PACK then factory = create_UIBox_standard_pack
  elseif G.STATE == G.STATES.BUFFOON_PACK then factory = create_UIBox_buffoon_pack end
  if not factory then return false end

  -- create_UIBox_* reads G.GAME.pack_size; Card:open() sets it, but a pack
  -- entered through any other path may not have.
  if not (G.GAME and G.GAME.pack_size and G.GAME.pack_size > 0) then
    if G.GAME then G.GAME.pack_size = 2 end
  end

  local ok = pcall(function()
    if G.booster_pack then
      pcall(function() G.booster_pack:remove() end)
      G.booster_pack = nil
    end
    -- The factory reassigns G.pack_cards itself; do NOT remove the old
    -- area (see abandon_pack — vanilla semantics keep it alive and empty).
    local definition = factory()
    G.booster_pack = UIBox{
      definition = definition,
      config = {align = "tmi", offset = {x = 0, y = G.ROOM.T.y + 9}, major = G.hand, bond = 'Weak'}
    }
    G.booster_pack.alignment.offset.y = -2.2
  end)

  if not ok then
    print("[DecideX Pack Watchdog] rebuild_pack_ui failed")
    return false
  end
  if not G.pack_cards then
    print("[DecideX Pack Watchdog] rebuild_pack_ui produced no G.pack_cards")
    return false
  end
  print("[DecideX Pack Watchdog] rebuilt pack UI, G.pack_cards restored")
  return true
end

--[[
  Abandon the current booster pack and return to the shop.

  Used as the last-resort escape from a wedged pack: the run keeps going
  instead of hanging. The player loses the pack contents, which is strictly
  better than an automation run that never advances again.
]]
local function abandon_pack()
  if not is_pack_state() then return false end
  local ok = pcall(function()
    local back = (G.GAME and G.GAME.PACK_INTERRUPT) or G.STATES.SHOP
    if G.booster_pack then
      pcall(function() G.booster_pack:remove() end)
      G.booster_pack = nil
    end
    -- Every later step must run even if an earlier cleanup line errored:
    -- a half-abandoned pack wedges both the game and the autopilot.
    local function step(name, fn)
      local s_ok, s_err = pcall(fn)
      if not s_ok then
        print("[DecideX Pack Watchdog] abandon step " .. name .. " failed: " .. tostring(s_err))
      end
    end
    -- Drop the reference WITHOUT removing the area: vanilla never removes
    -- G.pack_cards, and a removed area (.cards = nil) poisons every queued
    -- use_card restore event that still holds it (button_callbacks.lua:2283
    -- crashes + the can_skip_booster stale-global). An alive empty area
    -- updates cheaply and harmlessly; the align_to_major guard covers any
    -- layout fallout.
    step("pack_cards_ref", function() G.pack_cards = nil end)
    step("pack_interrupt", function() G.GAME.PACK_INTERRUPT = nil end)
    step("tarot_interrupt", function() G.TAROT_INTERRUPT = nil end)
    step("state", function()
      G.STATE = back
      G.STATE_COMPLETE = false
    end)
    step("locks", function()
      if G.CONTROLLER and G.CONTROLLER.locks then
        G.CONTROLLER.locks.use = false
      end
    end)
    step("unpause", function()
      if G.SETTINGS then G.SETTINGS.paused = false end
      if G.OVERLAY_MENU then dismiss_blocking_overlay() end
    end)
    -- use_card pushes the shop UI out of the way and stashes the original
    -- offset in .py; put it back so the shop is visible again.
    step("offsets", function()
      if G.shop and G.shop.alignment and G.shop.alignment.offset.py then
        G.shop.alignment.offset.y = G.shop.alignment.offset.py
        G.shop.alignment.offset.py = nil
      end
      if G.blind_select and G.blind_select.alignment and G.blind_select.alignment.offset.py then
        G.blind_select.alignment.offset.y = G.blind_select.alignment.offset.py
        G.blind_select.alignment.offset.py = nil
      end
      if G.round_eval and G.round_eval.alignment and G.round_eval.alignment.offset.py then
        G.round_eval.alignment.offset.y = G.round_eval.alignment.offset.py
        G.round_eval.alignment.offset.py = nil
      end
    end)
    _G._pack_stall_time = nil
    -- Exit latch: a stuck select_pack_card retry landing right after the
    -- abandon re-enters the pack state with another fresh empty area
    -- (the cards are gone) and re-wedges it. Pack actions are refused
    -- for a short window after an abandon.
    local now3 = (love.timer and love.timer.getTime()) or os.clock()
    _G._decidex_pack_exit_until = now3 + 3.0
  end)
  if ok then
    print("[DecideX Pack Watchdog] abandoned wedged pack, returned to state " .. tostring(G.STATE))
  end
  return ok
end

--[[
  Dump the state of the event queues. Used by the pack watchdog to explain
  *why* the pack UI never appeared, so the production logs carry the answer
  instead of just the symptom.
]]
local function event_queue_diagnostics()
  local parts = {}
  if G and G.E_MANAGER and G.E_MANAGER.queues then
    for name, q in pairs(G.E_MANAGER.queues) do
      local n = 0
      if type(q) == 'table' then n = #q end
      parts[#parts + 1] = tostring(name) .. "=" .. tostring(n)
    end
  end
  table.sort(parts)
  return table.concat(parts, ",")
end

--[[
  Repair a nil G.STATE mid-run.

  Vanilla restore events can assign nil to G.STATE: use_card queues
  `G.STATE = prev_state` (button_callbacks.lua:2248) and end_consumeable
  queues `G.STATE = G.GAME.PACK_INTERRUPT` (button_callbacks.lua:2597) —
  when those delayed events fire AFTER a DecideX recovery path already
  cleared the captured field (abandon_pack nils PACK_INTERRUPT), G.STATE
  becomes nil. Game:update dispatches on G.STATE, so a nil state matches
  no branch: nothing ever sets it again, check_and_emit_state returns
  early (no snapshot), and the autopilot freezes on the last stale
  snapshot with the blind-select UI stuck on screen.

  Repair by inferring the state from whatever UI is still visible. Only
  nil is repaired; delete_run's deliberate -1 limbo is left alone.
]]
local function repair_nil_state()
  if not (G and G.GAME and G.STATES) then return false end
  if G.STATE ~= nil then return false end
  local target = nil
  if G.blind_select and not G.blind_select.REMOVED then
    target = G.STATES.BLIND_SELECT
  elseif G.shop and not G.shop.REMOVED then
    target = G.STATES.SHOP
  elseif G.round_eval and not G.round_eval.REMOVED then
    target = G.STATES.ROUND_EVAL
  elseif G.booster_pack and not G.booster_pack.REMOVED then
    target = G.GAME.PACK_INTERRUPT or G.STATES.SHOP
  elseif G.hand and G.hand.cards and #G.hand.cards > 0 then
    target = G.STATES.SELECTING_HAND
  end
  if not target then
    -- No visible UI matched: the run was mid-restore (the nil write came
    -- from a restore event whose captured field was already consumed).
    -- The restore target itself is the best guess, else the shop.
    target = G.TAROT_INTERRUPT or (G.GAME and G.GAME.PACK_INTERRUPT) or G.STATES.SHOP
  end
  G.STATE = target
  G.STATE_COMPLETE = false
  print("[DecideX Frame Guard] repaired nil G.STATE -> " .. state_name_of(target))
  return true
end

--[[
  Guard G.FUNCS.can_skip_booster (button_callbacks.lua:2118).

  The pack UI polls this button predicate EVERY FRAME. The vanilla body
  indexes G.pack_cards.cards unguarded, and CardArea:remove() sets
  .cards = nil — so as soon as the global points at a removed area (a
  DecideX recovery raced a queued vanilla event), the predicate errors
  every frame for as long as the UI lives: observed 3600+ consecutive
  failed frames (~1 minute of frozen pack UI). Self-heal the stale global
  and dump its identity once, so the true writer can be traced from the
  production logs.
]]
local function install_can_skip_booster_guard()
  if not (G and G.FUNCS and G.FUNCS.can_skip_booster) then return false end
  if _G._decidex_can_skip_guarded then return false end
  _G._decidex_can_skip_guarded = true
  local orig = G.FUNCS.can_skip_booster
  G.FUNCS.can_skip_booster = function(e)
    if G.pack_cards and (not G.pack_cards.cards or G.pack_cards.REMOVED) then
      local registered = false
      pcall(function()
        for _, v in pairs(G.I.CARDAREA or {}) do
          if v == G.pack_cards then registered = true break end
        end
      end)
      print(string.format(
        "[DecideX Frame Guard] self-healed stale G.pack_cards (removed=%s, cards_nil=%s, in_cardarea_list=%s) — vanilla :2118 would have errored this frame",
        tostring(G.pack_cards.REMOVED), tostring(G.pack_cards.cards == nil), tostring(registered)))
      G.pack_cards = nil
    end
    return orig(e)
  end
  print("[DecideX Frame Guard] can_skip_booster stale-area guard installed")
  return true
end

--[[
  Guard Moveable.align_to_major (engine/moveable.lua:138-165).

  A UIE/Moveable whose role.major was removed keeps erroring here EVERY
  FRAME ("attempt to perform arithmetic on field 'y' (a nil value)") — and
  each error aborts the whole G:update mid-frame, freezing every other
  animation with it (observed 2700+ consecutive failed frames). On the
  first error, detach the dead bond: the orphan has nothing valid to
  align to anyway, and line 138 returns early once role.major is nil.
]]
--[[
  De-duplicate G.FUNCS.end_consumeable.

  end_consumeable queues the delayed state-restore event
  (`G.STATE = G.GAME.PACK_INTERRUPT` at +0.4s) and clears PACK_INTERRUPT
  inside it. The autopilot can legitimately race "use last pack card"
  against "skip pack", queueing end_consumeable twice; the second
  restore event then reads the already-cleared PACK_INTERRUPT and writes
  nil into G.STATE (observed: repair_nil_state firing 10+ times over 15s
  as staggered duplicate restores kept re-nil-ing it).
]]
local function install_end_consumeable_dedup()
  if not (G and G.FUNCS and G.FUNCS.end_consumeable) then return false end
  if _G._decidex_end_consumeable_dedup then return false end
  _G._decidex_end_consumeable_dedup = true
  local orig = G.FUNCS.end_consumeable
  G.FUNCS.end_consumeable = function(e, delayfac)
    local now = (love.timer and love.timer.getTime()) or os.clock()
    if _G._decidex_last_end_consumeable and (now - _G._decidex_last_end_consumeable) < 1.5 then
      return nil
    end
    _G._decidex_last_end_consumeable = now
    return orig(e, delayfac)
  end
  print("[DecideX Frame Guard] end_consumeable dedup installed")
  return true
end

local function install_align_to_major_guard()
  if not (Moveable and Moveable.align_to_major) then return false end
  if _G._decidex_align_guarded then return false end
  _G._decidex_align_guarded = true
  local orig = Moveable.align_to_major
  local err_count = 0
  Moveable.align_to_major = function(self, ...)
    local ok, err = pcall(orig, self, ...)
    if not ok then
      err_count = err_count + 1
      if self.role then self.role.major = nil end
      if err_count <= 5 or err_count % 300 == 0 then
        print(string.format(
          "[DecideX Frame Guard] align_to_major error #%d healed (bond detached): %s",
          err_count, tostring(err)))
      end
    end
    return ok
  end
  print("[DecideX Frame Guard] align_to_major dead-bond guard installed")
  return true
end

--[[
  Pack-UI invariant sweep.

  Two invariants the vanilla code relies on but our recovery paths can
  violate through queued-event races:

  1. A pack UI (G.booster_pack / G.pack_cards) never exists outside a pack
     state. end_consumeable removes the UIBox 0.2s BEFORE restoring the
     state, so a DecideX abandon that beat it left a window where a queued
     vanilla factory event could build a fresh pack UI after we already
     returned to the shop.
  2. During a pack state there is exactly ONE pack UIBox. The queued
     vanilla factory event can fire after our watchdog rebuilt the UI;
     the game then overwrites G.booster_pack with a second UIBox whose
     fresh G.pack_cards is EMPTY (the card-creation events fired long ago
     into the previous area) — re-wedging the pack we just healed. The
     orphaned duplicate is still drawn and still polls its buttons, so
     remove it and keep only the live one.
]]
local function sweep_pack_ui_invariants()
  if not (G and G.I and G.I.UIBOX) then return end
  if is_pack_state() then
    if G.booster_pack and not G.booster_pack.REMOVED then
      for i = #G.I.UIBOX, 1, -1 do
        local ub = G.I.UIBOX[i]
        if ub and ub ~= G.booster_pack and not ub.REMOVED then
          local has_skip = false
          pcall(function() has_skip = find_button_by_button_name(ub, 'skip_booster') ~= nil end)
          if has_skip then
            pcall(function() ub:remove() end)
            print("[DecideX Pack Watchdog] removed duplicate (orphaned) pack UIBox")
          end
        end
      end
    end
  elseif G.booster_pack or G.pack_cards then
    if G.booster_pack then
      pcall(function() G.booster_pack:remove() end)
      G.booster_pack = nil
    end
    if G.pack_cards then
      pcall(function() G.pack_cards:remove() end)
      G.pack_cards = nil
    end
    print("[DecideX Pack Watchdog] removed pack UI outside a pack state")
  end
end

--[[
  Watchdog for the booster-pack states.

  Detects "we are in a pack state but the pack UI never appeared" and tries,
  in order:
    1. dismiss a modal overlay that paused the game (this is the common
       cause: an unlock popup freezes the game mid-pack),
    2. rebuild the pack UIBox directly, bypassing the event queue,
    3. abandon the pack and go back to the shop.

  Returns true when it took a recovery action.
]]
local function pack_stall_watchdog()
  if not is_pack_state() then
    _G._pack_stall_time = nil
    _G._pack_stall_handled = nil
    _G._pack_rebuild_count = nil
    return false
  end

  local now = (love.timer and love.timer.getTime()) or os.clock()

  -- The pack UI is healthy as soon as G.pack_cards exists AND holds the
  -- pack's cards. An EMPTY area for >12s is a dead end, not health: when
  -- the watchdog rebuild raced the queued card-creation events, those
  -- events fired into the nil global and the pack's cards are gone for
  -- good — no amount of waiting or re-rebuilding fills it again. Escape
  -- by abandoning the pack so the run keeps moving.
  if G.pack_cards then
    _G._pack_stall_time = nil
    _G._pack_stall_handled = nil
    local now2 = (love.timer and love.timer.getTime()) or os.clock()
    local empty = not (G.pack_cards.cards and G.pack_cards.cards[1])
    if not empty then
      _G._pack_empty_time = nil
      _G._pack_empty_handled = nil
      return false
    end
    if not _G._pack_empty_time then
      _G._pack_empty_time = now2
      return false
    end
    if now2 - _G._pack_empty_time > 12.0 and not _G._pack_empty_handled then
      _G._pack_empty_handled = true
      print("[DecideX Pack Watchdog] pack area empty for 12s (creation events lost); abandoning")
      abandon_pack()
      _G._pack_empty_time = nil
      return true
    end
    return false
  end
  _G._pack_empty_time = nil
  _G._pack_empty_handled = nil

  if not _G._pack_stall_time then
    _G._pack_stall_time = now
    return false
  end

  local stalled_for = now - _G._pack_stall_time
  if stalled_for < 4.0 then return false end

  -- Only act once per stall episode.
  if _G._pack_stall_handled then return false end
  _G._pack_stall_handled = true

  print("[DecideX Pack Watchdog] pack stalled for " .. string.format("%.1f", stalled_for) ..
        "s (state=" .. tostring(G.STATE) .. ", paused=" .. tostring(G.SETTINGS and G.SETTINGS.paused) ..
        ", overlay=" .. tostring(not not G.OVERLAY_MENU) ..
        ", state_complete=" .. tostring(G.STATE_COMPLETE) ..
        ", pack_size=" .. tostring(G.GAME and G.GAME.pack_size) ..
        ", queues=" .. event_queue_diagnostics() .. ")")

  -- 1. A modal overlay pauses the game and freezes every timer.
  if G.OVERLAY_MENU or (G.SETTINGS and G.SETTINGS.paused) then
    dismiss_blocking_overlay()
    if G.SETTINGS then G.SETTINGS.paused = false end
    -- Give the game a few frames to recover before forcing anything.
    _G._pack_stall_time = now
    _G._pack_stall_handled = nil
    return true
  end

  -- 2. Rebuild the pack UI directly — but only ONCE per pack episode.
  -- The card-creation events fire into whatever area is current at their
  -- run time, so a SECOND rebuild produces a permanently empty area and
  -- the pack re-wedges forever (each rebuild also races the queued
  -- vanilla factory event, which then replaces the rebuild with yet
  -- another empty area). After the first rebuild fails to stick, the
  -- only sane move is to abandon the pack.
  if (_G._pack_rebuild_count or 0) < 1 then
    if rebuild_pack_ui() then
      _G._pack_rebuild_count = (_G._pack_rebuild_count or 0) + 1
      _G._pack_stall_time = nil
      _G._pack_stall_handled = nil
      return true
    end
  else
    print("[DecideX Pack Watchdog] rebuild already tried this episode; abandoning instead")
  end

  -- 3. Give up on this pack and keep the run alive.
  abandon_pack()
  _G._pack_stall_handled = nil
  _G._pack_stall_time = nil
  return true
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
  if G.CONTROLLER and G.CONTROLLER.locks then
    G.CONTROLLER.locks.toggle_shop = nil
  end
  _G._toggle_shop_lock_time = nil
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

  local now = (love.timer and love.timer.getTime()) or os.clock()
  if _G._last_select_blind_time and (now - _G._last_select_blind_time > 4.0) then
    _G._select_blind_in_flight = nil
  end

  if _G._select_blind_in_flight then return nil end

  if _G._last_select_blind_time and (now - _G._last_select_blind_time < 1.2) then
    return nil -- Still animating previous select
  end

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

  local now = (love.timer and love.timer.getTime()) or os.clock()
  if _G._last_select_blind_time and (now - _G._last_select_blind_time > 4.0) then
    _G._select_blind_in_flight = nil
  end

  if _G._select_blind_in_flight then return nil end

  if _G._last_select_blind_time and (now - _G._last_select_blind_time < 1.2) then
    return nil
  end
  if not (G.blind_select and not G.blind_select.REMOVED) then return nil end
  -- The game holds this lock for 2.5s after a skip; respect it so we do not
  -- dispatch a second skip while the blind-select transition is animating.
  if G.CONTROLLER and G.CONTROLLER.locks and G.CONTROLLER.locks.skip_blind then
    return nil
  end
  local on_deck = (G.GAME and (G.GAME.blind_on_deck or (G.GAME.round_resets and G.GAME.round_resets.blind_on_deck))) or "Small"
  if on_deck == "Boss" then return nil end

  local opt = G.blind_select_opts and G.blind_select_opts[string.lower(on_deck)]
  if not opt then return nil end

  -- G.FUNCS.skip_blind(e) dereferences e.UIBox:get_UIE_by_ID('tag_container')
  -- (functions/button_callbacks.lua:2739), so e MUST be the real UIElement.
  -- The clickable node is the tag_<blind> node's second child, whose
  -- config.button the game sets to 'skip_blind' for the on-deck blind only
  -- (functions/button_callbacks.lua:2641). Handing the game a synthetic
  -- table here crashes with "attempt to index field 'UIBox' (a nil value)".
  local tag_node = nil
  if opt.get_UIE_by_ID then
    pcall(function() tag_node = opt:get_UIE_by_ID('tag_' .. on_deck) end)
  end
  if not tag_node then
    pcall(function()
      tag_node = find_button_by_button_name(opt, 'skip_blind')
    end)
  end
  if not tag_node then return nil, on_deck end

  -- tag_node is the 'tag_<blind>' container; its clickable child carries the
  -- 'skip_blind' button. Accept either the container's child or the node
  -- itself when the button is attached directly.
  local skip_btn = nil
  if tag_node.config and tag_node.config.button == 'skip_blind' then
    skip_btn = tag_node
  elseif tag_node.children then
    for _, child in ipairs(tag_node.children) do
      if child.config and child.config.button == 'skip_blind' then
        skip_btn = child
        break
      end
    end
  end
  if not skip_btn then return nil, on_deck end

  -- The game resolves 'tag_container' through e.UIBox, so the node must
  -- belong to a live UIBox that owns the tag container.
  if not (skip_btn.UIBox and skip_btn.UIBox.get_UIE_by_ID) then
    return nil, on_deck
  end
  local tag_container = nil
  pcall(function() tag_container = skip_btn.UIBox:get_UIE_by_ID('tag_container') end)
  if not tag_container then return nil, on_deck end

  return skip_btn, on_deck
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
  -- Expose the blind currently on deck so the client can tell a successful
  -- skip (Small -> Big) apart from a still-running transition, without
  -- waiting for the generic 4.5s self-heal timeout.
  local blind_on_deck = (G.GAME and (G.GAME.blind_on_deck or (G.GAME.round_resets and G.GAME.round_resets.blind_on_deck))) or ""
  parts[#parts + 1] = '"blind_on_deck":' .. jstr(tostring(blind_on_deck))
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
      if (shop_cards_count > 0 or shop_boosters_count > 0 or (now - _G._shop_enter_time >= 5.0)) then
        can_toggle_shop = true
      end
    else
      _G._shop_enter_time = nil
    end
  else
    _G._shop_enter_time = nil
  end
  parts[#parts + 1] = '"can_toggle_shop":' .. (can_toggle_shop and "true" or "false")

  if not (G.STATE and G.STATES and G.STATE == G.STATES.BLIND_SELECT) then
    _G._select_blind_in_flight = nil
    _G._blind_select_enter_time = nil
  end

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
  -- Mirror G.FUNCS.can_skip_booster(): the pack skip button only exists when
  -- the pack has cards and the current pack allows skipping. For Tarot and
  -- Spectral packs a card must be picked first, so skipping is not offered.
  local can_skip_booster = false
  if is_pack_state then
    local skip_btn = find_button_by_button_name(G.booster_pack, 'skip_booster')
    if skip_btn and skip_btn.config and skip_btn.config.button == 'skip_booster' then
      can_skip_booster = true
    else
      local pack_has_cards = (G.pack_cards and G.pack_cards.cards and G.pack_cards.cards[1]) ~= nil
      local state_allows_skip = (G.STATE == G.STATES.PLANET_PACK or G.STATE == G.STATES.STANDARD_PACK or
        G.STATE == G.STATES.BUFFOON_PACK or (G.hand and (G.hand.cards[1] or (G.hand.config.card_limit <= 0))))
      can_skip_booster = not not (pack_has_cards and state_allows_skip)
    end
  end
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

  local now = (love.timer and love.timer.getTime()) or os.clock()

  -- Hand play & discard lifecycle trackers
  if G.STATE == G.STATES.HAND_PLAYED then
    _G._hand_play_saw_hand_played = true
    _G._draw_to_hand_enter_time = nil
    if not _G._hand_played_enter_time then
      _G._hand_played_enter_time = now
      _G._last_base_queue_len = nil
      _G._last_base_queue_progress = now
    end

    local base_len = (G.E_MANAGER and G.E_MANAGER.queues and G.E_MANAGER.queues.base and #G.E_MANAGER.queues.base) or 0
    if base_len ~= _G._last_base_queue_len then
      _G._last_base_queue_len = base_len
      _G._last_base_queue_progress = now
    end

    local time_since_progress = now - (_G._last_base_queue_progress or now)

    -- If the event queue has completely drained, ensure G.STATE_COMPLETE is cleared so Game:update_hand_played progresses natively
    if base_len == 0 then
      if not _G._hand_played_empty_time then _G._hand_played_empty_time = now end
      if (now - _G._hand_played_empty_time > 0.4) and G.STATE_COMPLETE then
        print("[DecideX Bridge] HAND_PLAYED event queue empty, clearing G.STATE_COMPLETE to advance state")
        G.STATE_COMPLETE = false
      end
    else
      _G._hand_played_empty_time = nil
    end

    -- Only if event queue has made ZERO progress for > 15s (deadlocked/crashed animation) do we intervene
    if time_since_progress > 15.0 and (now - _G._hand_played_enter_time > 15.0) then
      print("[DecideX Bridge] Watchdog: advancing genuinely deadlocked HAND_PLAYED after 15s no queue progress")
      if G.E_MANAGER and G.E_MANAGER.clear_queue then
        pcall(function() G.E_MANAGER:clear_queue('base') end)
      end
      if G.FUNCS and G.FUNCS.draw_from_play_to_discard then
        pcall(G.FUNCS.draw_from_play_to_discard)
      end
      if G.play and G.play.cards and #G.play.cards > 0 and G.discard then
        for i = #G.play.cards, 1, -1 do
          local c = G.play.cards[i]
          if c then
            pcall(function()
              G.play:remove_card(c)
              G.discard:emplace(c)
            end)
          end
        end
      end
      G.STATE_COMPLETE = false
      if G.GAME and G.GAME.chips and G.GAME.blind and (G.GAME.chips >= G.GAME.blind.chips or (G.GAME.current_round and G.GAME.current_round.hands_left and G.GAME.current_round.hands_left < 1)) then
        G.STATE = G.STATES.NEW_ROUND
      else
        G.STATE = G.STATES.DRAW_TO_HAND
      end
      _G._hand_played_enter_time = now
      _G._last_base_queue_progress = now
    end
  else
    _G._hand_played_enter_time = nil
    _G._hand_played_empty_time = nil
    _G._last_base_queue_len = nil
    _G._last_base_queue_progress = nil
  end

  if G.STATE == G.STATES.DRAW_TO_HAND then
    _G._hand_discard_saw_draw = true
    if not _G._draw_to_hand_enter_time then
      _G._draw_to_hand_enter_time = now
      _G._last_draw_queue_len = nil
      _G._last_draw_queue_progress = now
    end

    local base_len = (G.E_MANAGER and G.E_MANAGER.queues and G.E_MANAGER.queues.base and #G.E_MANAGER.queues.base) or 0
    if base_len ~= _G._last_draw_queue_len then
      _G._last_draw_queue_len = base_len
      _G._last_draw_queue_progress = now
    end

    local time_since_progress = now - (_G._last_draw_queue_progress or now)
    if base_len == 0 then
      if not _G._draw_empty_time then _G._draw_empty_time = now end
      if (now - _G._draw_empty_time > 0.4) and G.STATE_COMPLETE then
        G.STATE_COMPLETE = false
      end
    else
      _G._draw_empty_time = nil
    end

    if time_since_progress > 12.0 and (now - _G._draw_to_hand_enter_time > 12.0) then
      if G.hand and G.hand.cards and #G.hand.cards > 0 then
        print("[DecideX Bridge] Watchdog: advancing stuck DRAW_TO_HAND to SELECTING_HAND after 12s")
        G.STATE = G.STATES.SELECTING_HAND
        G.STATE_COMPLETE = false
        if G.GAME and G.GAME.blind and G.GAME.blind.drawn_to_hand then
          pcall(function() G.GAME.blind:drawn_to_hand() end)
        end
        _G._draw_to_hand_enter_time = now
        _G._last_draw_queue_progress = now
      elseif G.deck and G.deck.cards and #G.deck.cards > 0 then
        print("[DecideX Bridge] Watchdog: re-triggering draw_from_deck_to_hand in DRAW_TO_HAND")
        pcall(function() G.FUNCS.draw_from_deck_to_hand(nil) end)
        _G._draw_to_hand_enter_time = now - 5.0
        _G._last_draw_queue_progress = now
      end
    end
  else
    _G._draw_to_hand_enter_time = nil
    _G._draw_empty_time = nil
    _G._last_draw_queue_len = nil
    _G._last_draw_queue_progress = nil
  end

  -- Lifecycle reset rules: Only release in-flight combat locks when safely back in SELECTING_HAND
  -- and play area is empty, or when exiting combat into a non-combat state.
  if G.STATE == G.STATES.SELECTING_HAND then
    local play_has_cards = (G.play and G.play.cards and #G.play.cards > 0)
    if play_has_cards and not _G._hand_play_in_flight then
      -- Auto-heal orphan cards left in G.play
      for i = #G.play.cards, 1, -1 do
        local c = G.play.cards[i]
        if c then
          pcall(function()
            G.play:remove_card(c)
            if G.discard then G.discard:emplace(c) end
          end)
        end
      end
      play_has_cards = (G.play and G.play.cards and #G.play.cards > 0)
    end
    if not play_has_cards and _G._hand_play_saw_hand_played then
      _G._hand_play_in_flight = nil
      _G._hand_play_saw_hand_played = nil
    end
    if _G._hand_discard_saw_draw then
      local expected = 8
      if G.hand and G.hand.config and G.hand.config.card_limit then
        expected = G.hand.config.card_limit
      end
      local deck_count = (G.deck and G.deck.cards and #G.deck.cards) or 0
      local hand_count = (G.hand and G.hand.cards and #G.hand.cards) or 0
      local target = math.min(expected, hand_count + deck_count)
      local elapsed = _G._hand_discard_start_time and (now - _G._hand_discard_start_time) or 0
      if hand_count >= target or elapsed >= 3.0 then
        _G._hand_discard_in_flight = nil
        _G._hand_discard_saw_draw = nil
        _G._last_discard_finish_time = now
      end
    end

    -- Self-heal incomplete hand if hand cards did not refill after 2.5s
    local expected = 8
    if G.hand and G.hand.config and G.hand.config.card_limit then
      expected = G.hand.config.card_limit
    end
    local deck_count = (G.deck and G.deck.cards and #G.deck.cards) or 0
    local hand_count = (G.hand and G.hand.cards and #G.hand.cards) or 0
    local target = math.min(expected, hand_count + deck_count)
    if deck_count > 0 and hand_count < target and not _G._hand_discard_in_flight and not _G._hand_play_in_flight then
      if not _G._incomplete_hand_time then _G._incomplete_hand_time = now end
      if now - _G._incomplete_hand_time > 2.5 then
        print("[DecideX Bridge] Incomplete hand in SELECTING_HAND (" .. hand_count .. "/" .. target .. ") >2.5s, healing via draw_from_deck_to_hand")
        if G.FUNCS and G.FUNCS.draw_from_deck_to_hand then
          pcall(function() G.FUNCS.draw_from_deck_to_hand(target - hand_count) end)
        end
        _G._incomplete_hand_time = now
      end
    else
      _G._incomplete_hand_time = nil
    end
  elseif G.STATE == G.STATES.NEW_ROUND or G.STATE == G.STATES.ROUND_EVAL or G.STATE == G.STATES.SHOP or G.STATE == G.STATES.BLIND_SELECT or G.STATE == G.STATES.GAME_OVER or G.STATE == G.STATES.GAME_WON then
    _G._hand_play_in_flight = nil
    _G._hand_play_saw_hand_played = nil
    _G._hand_discard_in_flight = nil
    _G._hand_discard_saw_draw = nil
  end

  -- Watchdog fallback to prevent any permanent lock (generous windows for animations)
  if _G._hand_play_in_flight and _G._hand_play_start_time and (now - _G._hand_play_start_time > 15.0) then
    _G._hand_play_in_flight = nil
    _G._hand_play_saw_hand_played = nil
  end
  if _G._hand_discard_in_flight and _G._hand_discard_start_time and (now - _G._hand_discard_start_time > 8.0) then
    _G._hand_discard_in_flight = nil
    _G._hand_discard_saw_draw = nil
  end

  local state_changed = (G.STATE ~= LAST_STATE_NUM)
  if state_changed then
    print("[DecideX State] " .. state_name_of(LAST_STATE_NUM) .. " -> " .. current_state_name() ..
      " (state_complete=" .. tostring(G.STATE_COMPLETE) ..
      ", on_deck=" .. tostring(G.GAME and G.GAME.blind_on_deck) ..
      ", facing=" .. tostring(G.GAME and G.GAME.facing_blind) .. ")")
    LAST_STATE_NUM = G.STATE
  end

  if G.STATE == G.STATES.SELECTING_HAND then
    LAST_SHOP_SIG = ""
    if G.GAME and G.GAME.current_round and G.GAME.current_round.hands_left and G.GAME.current_round.hands_left <= 0 then
      if G.GAME.chips and G.GAME.blind and G.GAME.chips < G.GAME.blind.chips then
        if not _G._zero_hands_time then _G._zero_hands_time = now end
        if now - _G._zero_hands_time > 2.0 then
          print("[DecideX Bridge] Hands depleted and blind not met in SELECTING_HAND, advancing to NEW_ROUND/GAME_OVER")
          G.STATE = G.STATES.NEW_ROUND
          G.STATE_COMPLETE = false
          _G._zero_hands_time = nil
        end
      end
    else
      _G._zero_hands_time = nil
    end

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
        local edition = ""
        if card.edition and type(card.edition) == "table" then
          for k, v in pairs(EDITION_MAP) do
            if card.edition[k] then edition = v break end
          end
        end
        shop_items_sig[#shop_items_sig + 1] = table.concat({key, name, set, tostring(cost), tostring(extra), edition}, ":")
      end
    end
    local cur_shop_sig = tostring(dollars) .. ":" .. tostring(reroll) .. ":" .. tostring(#shop_cards) .. ":" .. tostring(#booster_cards) .. ":" .. tostring(#voucher_cards) .. ":" .. table.concat(shop_items_sig, "|") .. ":" .. tostring(#jokers) .. ":" .. can_toggle .. ":" .. locked
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
    LAST_SHOP_SIG = ""
    G.toggling_shop = false
    G.cashing_out = false
    local now = (love.timer and love.timer.getTime()) or os.clock()
    if not _G._blind_select_enter_time then _G._blind_select_enter_time = now end
    if state_changed then
      _G._blind_select_enter_time = now
    end
    local wipe_locked = (G.CONTROLLER and G.CONTROLLER.locks and (G.CONTROLLER.locks.wipe or G.CONTROLLER.locks.load))
    local facing = G.GAME and G.GAME.facing_blind
    if not wipe_locked and not facing and not _G._select_blind_in_flight and (now - _G._blind_select_enter_time > 3.0) then
      if not (G.blind_select and not G.blind_select.REMOVED) then
        if create_UIBox_blind_select and UIBox then
          pcall(function()
            G.blind_select = UIBox{
              definition = create_UIBox_blind_select(),
              config = {align="bmi", offset = {x=0,y=G.ROOM.T.y + 29},major = G.hand, bond = 'Weak'}
            }
          end)
        end
      end
    end
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
  elseif G.STATE == G.STATES.GAME_OVER or G.STATE == G.STATES.GAME_WON then
    local cur_sig = tostring(G.STATE) .. ":" .. tostring(G.GAME and G.GAME.round or 0) .. ":" .. tostring(G.GAME and G.GAME.dollars or 0)
    if cur_sig ~= _G._last_game_over_sig or state_changed then
      _G._last_game_over_sig = cur_sig
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
      if not _G._hand_play_in_flight then
        for i = #G.play.cards, 1, -1 do
          local c = G.play.cards[i]
          if c then
            pcall(function()
              G.play:remove_card(c)
              if G.discard then G.discard:emplace(c) end
            end)
          end
        end
      end
      if G.play and G.play.cards and #G.play.cards > 0 then
        return false, "cards already in play"
      end
    end
    if not (G.hand and G.hand.cards and #G.hand.cards > 0) then
      return false, "no hand cards"
    end

    local now = (love.timer and love.timer.getTime()) or os.clock()

    if action == "play" then
      if _G._hand_play_in_flight then
        if _G._hand_play_start_time and (now - _G._hand_play_start_time < 15.0) then
          return false, "hand play already in progress (animating)"
        else
          _G._hand_play_in_flight = nil
          _G._hand_play_saw_hand_played = nil
        end
      end
      if G.play and G.play.cards and #G.play.cards > 0 then
        if not _G._hand_play_in_flight then
          for i = #G.play.cards, 1, -1 do
            local c = G.play.cards[i]
            if c then
              pcall(function()
                G.play:remove_card(c)
                if G.discard then G.discard:emplace(c) end
              end)
            end
          end
        end
        if G.play and G.play.cards and #G.play.cards > 0 then
          return false, "cards already in play"
        end
      end
      if G.STATE and G.STATES and G.STATE == G.STATES.HAND_PLAYED then
        return false, "state is HAND_PLAYED"
      end
      if G.GAME and G.GAME.blind and G.GAME.blind.block_play then
        return false, "blind blocks play"
      end
      if G.GAME and G.GAME.current_round and G.GAME.current_round.hands_left and G.GAME.current_round.hands_left <= 0 then
        return false, "no hands left"
      end
    elseif action == "discard" then
      if _G._hand_discard_in_flight then
        if _G._hand_discard_start_time and (now - _G._hand_discard_start_time < 5.0) then
          return false, "hand discard already in progress (animating)"
        else
          _G._hand_discard_in_flight = nil
          _G._hand_discard_saw_draw = nil
        end
      end
      if _G._last_discard_finish_time and (now - _G._last_discard_finish_time < 1.5) then
        return false, "discard debounced (drawing cards)"
      end
      if G.GAME and G.GAME.current_round and G.GAME.current_round.discards_left and G.GAME.current_round.discards_left <= 0 then
        return false, "no discards left"
      end
    end

    if _G._last_hand_action_time and (now - _G._last_hand_action_time < 0.5) then
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
      local max_cards = #G.hand.cards
      local valid_count = 0
      for _, idx in ipairs(args.indices) do
        local n_idx = tonumber(idx)
        if n_idx and n_idx >= 1 and n_idx <= max_cards and valid_count < 5 then
          local c = G.hand.cards[n_idx]
          if c and not c.highlighted then
            if G.hand.add_to_highlighted then
              G.hand:add_to_highlighted(c, true)
            else
              c.highlighted = true
              table.insert(G.hand.highlighted, c)
            end
            valid_count = valid_count + 1
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
      if G.FUNCS and G.FUNCS.play_cards_from_highlighted then
        local selected = {}
        for _, card in ipairs(G.hand.highlighted or {}) do selected[#selected + 1] = card end
        _G._last_hand_action_time = now
        _G._hand_play_in_flight = true
        _G._hand_play_start_time = now
        _G._hand_play_saw_hand_played = false
        local ok, err = pcall(G.FUNCS.play_cards_from_highlighted, {})
        if ok then
          record_hand_type(classify_selected_hand(selected))
          return true, "played"
        else
          _G._hand_play_in_flight = nil
          _G._hand_play_saw_hand_played = nil
          return false, "play error: " .. tostring(err)
        end
      end
    elseif action == "discard" then
      if G.FUNCS and G.FUNCS.discard_cards_from_highlighted then
        _G._last_hand_action_time = now
        _G._hand_discard_in_flight = true
        _G._hand_discard_start_time = now
        _G._hand_discard_saw_draw = false
        local ok, err = pcall(G.FUNCS.discard_cards_from_highlighted, {})
        if ok then
          return true, "discarded"
        else
          _G._hand_discard_in_flight = nil
          _G._hand_discard_saw_draw = nil
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
      return false, "not in BLIND_SELECT (actual=" .. current_state_name() ..
        ", facing=" .. tostring(G.GAME and G.GAME.facing_blind) ..
        ", blind_select_ui=" .. tostring(G.blind_select ~= nil and not G.blind_select.REMOVED) ..
        ", state_complete=" .. tostring(G.STATE_COMPLETE) .. ")"
    end
    local now = (love.timer and love.timer.getTime()) or os.clock()
    if _G._last_select_blind_time and (now - _G._last_select_blind_time > 4.0) then
      _G._select_blind_in_flight = nil
    end
    if (G.GAME and G.GAME.facing_blind) or _G._select_blind_in_flight then
      return true, "blind already selected (animating)"
    end
    if _G._last_select_blind_time and (now - _G._last_select_blind_time < 1.0) then
      return true, "select_blind debounced"
    end
    local btn, on_deck = find_select_blind_button()
    if not btn and not (G.blind_select and not G.blind_select.REMOVED) then
      if create_UIBox_blind_select and UIBox then
        pcall(function()
          G.blind_select = UIBox{
            definition = create_UIBox_blind_select(),
            config = {align="bmi", offset = {x=0,y=G.ROOM.T.y + 29},major = G.hand, bond = 'Weak'}
          }
        end)
        btn, on_deck = find_select_blind_button()
      end
    end
    if not btn then
      return false, "blind_select UI not ready"
    end
    _G._last_select_blind_time = now
    _G._select_blind_in_flight = true

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
        _G._select_blind_in_flight = nil
        if G.GAME then G.GAME.facing_blind = prev_facing end
        return false, "select_blind error: " .. tostring(err)
      end
      if not (G.GAME and G.GAME.facing_blind) then
        _G._select_blind_in_flight = nil
        return false, "select_blind no-op (facing_blind not set)"
      end
      return true, "selected blind " .. tostring(on_deck)
    end
    _G._select_blind_in_flight = nil
    return false, "no select_blind function"
  end

  if action == "skip_blind" then
    if not (G and G.STATES and G.STATE == G.STATES.BLIND_SELECT) then
      return false, "not in BLIND_SELECT"
    end
    if (G.GAME and G.GAME.facing_blind) or _G._select_blind_in_flight then
      return false, "blind already selected (animating)"
    end
    local btn, on_deck = find_skip_blind_button()
    if not btn then
      return false, "skip_blind button not found (boss or not ready)"
    end
    -- Stamp the time so the 4s staleness reset in find_*_blind_button can
    -- clear this latch. Without it the latch would stay true forever and
    -- wedge can_select_blind / can_skip_blind.
    _G._last_select_blind_time = (love.timer and love.timer.getTime()) or os.clock()
    _G._select_blind_in_flight = true
    if G.FUNCS and G.FUNCS.skip_blind then
      local ok, err = pcall(G.FUNCS.skip_blind, btn)
      if not ok then
        _G._select_blind_in_flight = nil
        return false, "skip_blind error: " .. tostring(err)
      end
      -- The game's skip_blind does NOT set G.GAME.facing_blind; it advances
      -- blind_on_deck instead. Release the latch right away and let the
      -- blind_on_deck change drive the next decision.
      _G._select_blind_in_flight = nil
      return true, "skipped blind " .. tostring(on_deck)
    end
    _G._select_blind_in_flight = nil
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

    -- Handle Booster pack purchase and opening.
    -- The shop's "open" button is wired to button='open_booster', but no
    -- G.FUNCS.open_booster exists: can_open() rewrites config.button to
    -- 'use_card' at runtime. So use_card is the only real dispatch path.
    -- buy_from_shop must NOT be used here: check_for_buy_space() rejects
    -- Booster cards (ability.set is neither Voucher/Enhanced/Default/Joker
    -- nor consumeable), and its fallback branch would emplace the pack into
    -- G.jokers instead of opening it.
    if area_name == "shop_booster" or area_name == "shop_boosters" or (c.ability and c.ability.set == "Booster") then
      local available_money = (G.GAME and G.GAME.dollars and (G.GAME.dollars - (G.GAME.bankrupt_at or 0))) or 0
      if c.cost and c.cost > 0 and c.cost > available_money then
        return false, "cannot afford booster pack: cost=" .. tostring(c.cost) .. ", available=" .. tostring(available_money)
      end
      if G.FUNCS and G.FUNCS.use_card then
        -- Card:open() charges the cost itself via ease_dollars(-self.cost)
        -- (card.lua:1716); charging here as well would double-charge.
        local btn = { config = { ref_table = c, button = 'use_card' } }
        local ok, err = pcall(G.FUNCS.use_card, btn)
        if ok then return true, "opened booster pack via use_card" end
        return false, "use_card failed for booster: " .. tostring(err)
      end
      if c.open then
        local ok, err = pcall(function() c:open() end)
        if ok then return true, "opened booster pack directly" end
        return false, "booster open failed: " .. tostring(err)
      end
      return false, "failed to open booster pack"
    end

    -- Handle Voucher purchase and redemption
    if area_name == "shop_vouchers" or area_name == "shop_voucher" or (c.ability and c.ability.set == "Voucher") then
      local available_money = (G.GAME and G.GAME.dollars and (G.GAME.dollars - (G.GAME.bankrupt_at or 0))) or 0
      if c.cost and c.cost > 0 and c.cost > available_money then
        return false, "cannot afford voucher: cost=" .. tostring(c.cost) .. ", available=" .. tostring(available_money)
      end
      local btn = { config = { ref_table = c, button = 'use_card' } }
      if G.FUNCS and G.FUNCS.use_card then
        local ok, err = pcall(G.FUNCS.use_card, btn)
        if ok then return true, "redeemed voucher via use_card" end
      end
      if c.redeem then
        local ok, err = pcall(function() c:redeem() end)
        if ok then return true, "redeemed voucher directly" end
      end
      return false, "failed to redeem voucher"
    end

    -- Standard card / joker / consumable purchase
    if G.FUNCS and G.FUNCS.buy_from_shop then
      local btn = { config = { ref_table = c, id = 'buy_button' } }
      local ok, res = pcall(G.FUNCS.buy_from_shop, btn)
      -- buy_from_shop returns false when check_for_buy_space() refuses the
      -- purchase (joker/consumable slots are full). pcall alone would treat
      -- that as success.
      if not ok then return false, "buy_from_shop error: " .. tostring(res) end
      if res == false then return false, "no space to buy " .. tostring(c.name or "card") end
      return true, "bought card"
    end
    return false, "no buy_from_shop func"
  end

  if action == "reroll_shop" then
    if G.STATES and G.STATE ~= G.STATES.SHOP then
      return false, "not in SHOP"
    end
    -- The shop reroll button is declared with button='reroll_shop' but has
    -- no id, so get_UIE_by_ID can never find it. Locate it by button name
    -- so can_reroll() can gate it properly.
    local btn = find_button_by_button_name(G.shop, 'reroll_shop')
    if not btn then
      btn = find_uie_by_id('reroll_shop_button')
    end
    -- can_reroll() disables the button (config.button = nil) when the player
    -- cannot afford the reroll. Reproduce that gate here: without it the
    -- bridge happily rerolls into negative money.
    local reroll_cost = (G.GAME and G.GAME.current_round and G.GAME.current_round.reroll_cost) or 0
    local available = (G.GAME and G.GAME.dollars and (G.GAME.dollars - (G.GAME.bankrupt_at or 0))) or 0
    if reroll_cost > 0 and available < reroll_cost then
      return false, "cannot afford reroll: cost=" .. tostring(reroll_cost) .. ", available=" .. tostring(available)
    end
    if G.CONTROLLER and G.CONTROLLER.locks and G.CONTROLLER.locks.shop_reroll then
      return false, "shop reroll already in progress"
    end
    if G.FUNCS and G.FUNCS.reroll_shop then
      if not btn then btn = { config = {} } end
      local ok, err = pcall(G.FUNCS.reroll_shop, btn)
      if not ok then return false, "reroll_shop error: " .. tostring(err) end
      return true, "rerolled shop"
    end
    return false, "no reroll_shop func"
  end

  if action == "toggle_shop" then
    if G.STATES and G.STATE ~= G.STATES.SHOP then
      if G.STATES and G.STATE == G.STATES.BLIND_SELECT then
        return true, "already in BLIND_SELECT"
      end
      return false, "not in SHOP (actual=" .. current_state_name() .. ")"
    end
    local now = (love.timer and love.timer.getTime()) or os.clock()
    _G._shop_toggle_count = (_G._shop_toggle_count or 0) + 1

    -- Self-healing: if toggle_shop is called repeatedly (>=3 times) or shop UI is already removed or stuck,
    -- force transition to BLIND_SELECT immediately!
    if not (G and G.shop and not G.shop.REMOVED) or _G._shop_toggle_count >= 3 then
      pcall(function()
        if G.shop then G.shop:remove(); G.shop = nil end
        if G.SHOP_SIGN then G.SHOP_SIGN:remove(); G.SHOP_SIGN = nil end
      end)
      G.STATE = G.STATES.BLIND_SELECT
      G.STATE_COMPLETE = false
      G.toggling_shop = nil
      if G.CONTROLLER and G.CONTROLLER.locks then
        G.CONTROLLER.locks.toggle_shop = nil
      end
      _G._shop_toggle_count = 0
      _G._toggle_shop_lock_time = nil
      return true, "force transitioned from SHOP to BLIND_SELECT"
    end

    -- Always clear stale locks and toggling flags before calling toggle_shop
    G.toggling_shop = nil
    if G.CONTROLLER and G.CONTROLLER.locks then
      G.CONTROLLER.locks.toggle_shop = nil
    end

    if _G._last_toggle_shop_time and (now - _G._last_toggle_shop_time < 0.6) then
      return true, "toggle_shop debounced"
    end
    local btn = find_next_round_button() or { config = { button = 'toggle_shop' } }
    if G.FUNCS and G.FUNCS.toggle_shop then
      _G._last_toggle_shop_time = now
      local ok, err = pcall(G.FUNCS.toggle_shop, btn)
      return ok, tostring(err or "toggled shop")
    end
    return false, "no toggle_shop func"
  end

  if action == "force_leave_pack" then
    -- Escape hatch for a booster pack that never produced its UI. The
    -- autopilot calls this when the pack has been stuck for too long.
    if not is_pack_state() then
      return false, "not in a pack state"
    end
    if G.OVERLAY_MENU or (G.SETTINGS and G.SETTINGS.paused) then
      dismiss_blocking_overlay()
    end
    if rebuild_pack_ui() then
      _G._pack_stall_time = nil
      _G._pack_stall_handled = nil
      return true, "rebuilt pack UI"
    end
    if abandon_pack() then
      return true, "abandoned wedged pack"
    end
    return false, "could not leave pack"
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
    -- The pack skip button is declared with button='skip_booster' but has
    -- no id, so find it by button name inside G.booster_pack.
    local btn = find_button_by_button_name(G.booster_pack, 'skip_booster')
    if not btn then btn = find_uie_by_id('skip_booster') end
    if G.FUNCS and G.FUNCS.skip_booster then
      if not btn then btn = { config = { button = 'skip_booster' } } end
      local ok, err = pcall(G.FUNCS.skip_booster, btn)
      if ok then return true, "skipped booster via G.FUNCS.skip_booster" end
      return false, "skip_booster error: " .. tostring(err)
    end
    if G.FUNCS and G.FUNCS.end_consumeable then
      local ok, err = pcall(G.FUNCS.end_consumeable)
      if ok then return true, "skipped booster via G.FUNCS.end_consumeable" end
      return false, "end_consumeable error: " .. tostring(err)
    end
    return false, "no skip_booster or end_consumeable func"
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
    local now_gate = (love.timer and love.timer.getTime()) or os.clock()
    if _G._decidex_pack_exit_until and now_gate < _G._decidex_pack_exit_until then
      return false, "pack exit in progress"
    end
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
      local ok, err = pcall(G.FUNCS.use_card, {config = {ref_table = c, button = 'use_card'}})
      if ok then return true, "selected pack card: " .. tostring(c.ability and c.ability.name or idx) end
      return false, "use_card failed: " .. tostring(err)
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
      local ok, err = pcall(G.FUNCS.use_card, {config = {ref_table = c, button = 'use_card'}})
      return ok, tostring(err or "used card")
    elseif action == "sell_card" and G.FUNCS and G.FUNCS.sell_card then
      local ok, err = pcall(G.FUNCS.sell_card, {config = {ref_table = c, button = 'sell_card'}})
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
    if _G._last_start_run_time and (now - _G._last_start_run_time < 6.0) then
      return true, "start_run already in progress"
    end
    local wipe_locked = (G.CONTROLLER and G.CONTROLLER.locks and (G.CONTROLLER.locks.wipe or G.CONTROLLER.locks.load))
    if wipe_locked then
      return true, "start_run screen wipe transition in progress"
    end
    _G._last_start_run_time = now

    local is_game_over = (G.STATE and G.STATES and (G.STATE == G.STATES.GAME_OVER or G.STATE == G.STATES.GAME_WON)) or (G.GAME and G.GAME.won)
    if is_game_over then
      if G.FUNCS and G.FUNCS.go_to_menu then
        pcall(G.FUNCS.go_to_menu)
        return true, "resetting run from game over to menu"
      end
    end

    if G.STAGE and G.STAGES and G.STAGE == G.STAGES.RUN and G.STATE ~= G.STATES.SPLASH and G.STATE ~= G.STATES.MENU then
      return false, "already in run"
    end

    if G.FUNCS and G.FUNCS.start_run then
      local ok, err = pcall(G.FUNCS.start_run, nil, {stake = 1})
      return ok, tostring(err or "started run")
    end
    return false, "no start_run"
  end

  if action == "go_to_menu" then
    if G.FUNCS and G.FUNCS.go_to_menu then
      local ok, err = pcall(G.FUNCS.go_to_menu)
      return ok, tostring(err or "went to menu")
    end
    return false, "no go_to_menu func"
  end

  if action == "force_state_dump" then
    LAST_HAND_SIG = ""
    LAST_SHOP_SIG = ""
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

--[[
  Frame-loop crash guard.

  Any Lua error that escapes into love.update — the game's own update code,
  a half-removed UIBox, the overlay dismissal path — used to kill the whole
  WASM mainloop: the game froze on the current frame, and every JS->Lua eval
  timed out with no diagnostics ("Lua action ... acknowledgement timed out").
  Suppress the error, report it at a throttled rate, and keep the frame loop
  and the bridge eval channel alive so the watchdogs can still drive a
  recovery.
]]
local _decidex_frame_err_count = 0
local function report_frame_error(tag, err)
  _decidex_frame_err_count = _decidex_frame_err_count + 1
  local n = _decidex_frame_err_count
  if n <= 5 or n % 300 == 0 then
    print(string.format("[DecideX Frame Guard] %s error #%d suppressed: %s", tag, n, tostring(err)))
  end
end

local function decidex_maintenance()
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
      -- A modal overlay (unlock popup, achievement card, ...) pauses the
      -- game. The autopilot never clicks through it, and a paused game
      -- freezes every timer, so the run would hang forever. Dismiss it
      -- ourselves and resume.
      if G.SETTINGS.paused and G.OVERLAY_MENU and not G.OVERLAY_TUTORIAL then
        dismiss_blocking_overlay()
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

  if not _G._decidex_event_handle_guarded and Event then
    pcall(install_emanager_crash_guard)
  end

  -- can_skip_booster is polled every frame by the pack UI; a stale
  -- G.pack_cards global used to error it 3600 frames in a row.
  pcall(install_can_skip_booster_guard)

  -- A Moveable aligned to a dead major aborts G:update every frame.
  pcall(install_align_to_major_guard)

  -- Duplicate pack exits queue duplicate state-restore events that write
  -- nil into G.STATE (staggered over seconds).
  pcall(install_end_consumeable_dedup)

  -- A nil G.STATE mid-run freezes the whole state machine (see
  -- repair_nil_state); infer it back from the visible UI.
  pcall(repair_nil_state)

  -- Pack UI invariants (no pack UI outside pack states, no duplicate
  -- pack UIBoxes during one).
  pcall(sweep_pack_ui_invariants)

  -- Booster-pack watchdog: recover from a pack whose UI never appeared.
  pcall(pack_stall_watchdog)

  if not _G._decidex_step_logged then
    _G._decidex_step_logged = true
    print("[DecideX Web] DECIDEX_STEP actively ticking in game loop")
  end
end

local function decidex_step(dt)
  if G and not G.DECIDEX_EXECUTE then
    G.DECIDEX_EXECUTE = decidex_execute
  end
  if G and G.SETTINGS and G.SETTINGS.GAMESPEED and G.SETTINGS.GAMESPEED < 4 then
    G.SETTINGS.GAMESPEED = 4
  end

  -- Maintenance must never block the eval channel below: an error here
  -- (e.g. a broken overlay mid-dismissal) used to stop the bridge from
  -- consuming commands, and every JS dispatch then timed out.
  local ok_maint, err_maint = pcall(decidex_maintenance)
  if not ok_maint then report_frame_error("maintenance", err_maint) end

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

local function safe_decidex_step(dt)
  local ok, err = pcall(decidex_step, dt)
  if not ok then report_frame_error("step", err) end
end

-- Expose the guarded wrapper (also used by the game-loop hook injected by
-- build.py): a bridge error must never be able to kill the frame.
_G.DECIDEX_STEP = safe_decidex_step

-- Also hook love.update as fallback
local orig_update = love.update
love.update = function(dt)
  if orig_update then
    local ok, err = pcall(orig_update, dt)
    if not ok then report_frame_error("game update", err) end
  end
  safe_decidex_step(dt)
end

print("[DecideX Web] decidex_bridge.lua initialized")
return true
