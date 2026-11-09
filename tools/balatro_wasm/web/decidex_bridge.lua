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
            -- Rate-limit per error signature. The known-broken vanilla
            -- close chain (button_callbacks.lua:2283, focus restore on a
            -- removed pack CardArea) and zombie tarot transform events
            -- (card.lua, queued before a round ended) fire hundreds of
            -- times per session. First occurrence prints in full, then a
            -- suppressed count every 10s keeps observability without
            -- flooding the console mirror (45min soak: 334 drops).
            local msg = tostring(ret)
            local now = (love.timer and love.timer.getTime()) or os.clock()
            if not _G._decidex_guard_throttle then _G._decidex_guard_throttle = {} end
            local bucket = _G._decidex_guard_throttle[msg]
            if not bucket then
              _G._decidex_guard_throttle[msg] = { t = now, n = 0 }
              print("[DecideX Event Guard] Dropping crashed event: " .. msg)
            elseif now - bucket.t > 10 then
              print("[DecideX Event Guard] Dropping crashed event: " .. msg ..
                    " (+" .. tostring(bucket.n) .. " suppressed in last 10s)")
              _G._decidex_guard_throttle[msg] = { t = now, n = 0 }
            else
              bucket.n = bucket.n + 1
            end
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

-- Forward declaration: auto_close_consumed_pack resets this to force a
-- fresh [DECIDEX_STATE] emit carrying pack_closing=true (see its comment).
local LAST_PACK_SIG = ""

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
      '"index":' .. jstr(idx),
      '"highlighted":' .. (c.highlighted and "true" or "false")
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
  -- The rebuilt area is EMPTY (the factory only creates the CardArea+UIBox;
  -- vanilla fills it from queued events that may already be dead). Record
  -- when this happened for populate_empty_pack, and force a fresh emit: the
  -- new area identity bumps pack_epoch, which resets the client's
  -- saw-cards-this-episode latch so it waits for the refill instead of
  -- treating the pack as consumed.
  _G._pack_rebuilt_at = (love.timer and love.timer.getTime()) or os.clock()
  _G._pack_populated = nil
  LAST_PACK_SIG = ""
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
    local dying_area = G.pack_cards
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
    step("pack_cards_zombie", function()
      -- v137: booster_pack:remove() cascades into the pack_cards
      -- CardArea (a UI child) and CardArea:remove() nils .cards. A
      -- stale use_card restore event queued before this abandon still
      -- holds the area object and indexes area.cards[1] at fire time
      -- (button_callbacks.lua:2283 — 61 crashes per 40min soak, still
      -- firing whenever a congested queue delivers the restore late).
      -- Give the zombie an empty living table so the late restore
      -- evaluates area.cards[1] as falsy and skips gracefully.
      if dying_area and dying_area.cards == nil then
        dying_area.cards = {}
      end
    end)
    step("pack_interrupt", function()
      -- v137: leave a VALID state value (where we are returning to)
      -- instead of nil. Vanilla's queued end_consumeable tail
      -- (button_callbacks.lua:2625) reads G.GAME.PACK_INTERRUPT at FIRE
      -- time and writes it straight into G.STATE; after this abandon
      -- nil'd the field that tail wrote nil and the nil-STATE repair
      -- fired 10+ times per episode. With a valid value the same tail
      -- re-writes the state we already set, then self-clears the field.
      G.GAME.PACK_INTERRUPT = back
    end)
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
  v131c: mark every pack-UI factory call (crash or not) and surface the real
  crash message.

  The factory event is the ONLY thing that can open a pack NATIVELY (with
  the real open animation and the vanilla card-creation events). Knowing
  whether it ran — and with what error — separates "never queued" (the
  update_<pack> STATE_COMPLETE gate was skipped; fixable by re-arming) from
  "ran and crashed" (needs rebuild_pack_ui). Without this wrapper the crash
  only shows up as a rate-limited Event Guard drop, and a first-occurrence
  print is easily consumed by an earlier same-signature crash.

  The wrapper preserves vanilla failure semantics: on crash it re-raises so
  the Event Guard drops the host event exactly as before.
]]
local function install_pack_factory_markers()
  if _G._decidex_factory_markers then return false end
  local names = { "create_UIBox_arcana_pack", "create_UIBox_celestial_pack",
                  "create_UIBox_spectral_pack", "create_UIBox_standard_pack",
                  "create_UIBox_buffoon_pack" }
  local installed = 0
  for _, name in ipairs(names) do
    local orig = rawget(_G, name)
    if type(orig) == "function" then
      rawset(_G, name, function(...)
        _G._pack_factory_seen = (G and G.STATE) or true
        _G._pack_factory_at = (love.timer and love.timer.getTime()) or os.clock()
        local ok, def = pcall(orig, ...)
        if not ok then
          print("[DecideX Pack Diag] factory " .. tostring(name) ..
                " crashed in state " .. tostring(G and G.STATE) .. ": " .. tostring(def))
          error(def, 2)
        end
        return def
      end)
      installed = installed + 1
    end
  end
  if installed > 0 then
    _G._decidex_factory_markers = true
    print("[DecideX Pack Diag] factory markers installed on " ..
          tostring(installed) .. "/5 pack factories")
  end
  return installed > 0
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
--[[
  Refuse to build the blind-select UI outside BLIND_SELECT.

  Game:update_blind_select queues the builder inside two nested events
  (game.lua:3254-3290). A booster pack opened around the transition keeps the
  queue blocked past the state change, so the builder lands while the pack is
  on screen. It then creates a blind-select box and rewrites
  blind_states[on_deck] to 'Select' -- un-defeating the blind just beaten --
  and its own follow-up event crashes on the prompt box
  (UI_definitions.lua:1435). Vanilla only ever shows this box in
  BLIND_SELECT, so refusing the build there is safe and leaves the pack UI
  alone; the box is built normally when BLIND_SELECT is actually entered.
]]
local function install_blind_select_build_guard()
  if not (G and G.UIT and create_UIBox_blind_select) then return false end
  if _G._decidex_blind_build_guarded then return false end
  _G._decidex_blind_build_guarded = true
  local orig = create_UIBox_blind_select
  create_UIBox_blind_select = function(...)
    if G.STATES and G.STATE ~= G.STATES.BLIND_SELECT then
      print("[DecideX Blind Guard] refused blind-select build in " ..
        current_state_name())
      return {n = G.UIT.ROOT, config = {align = 'cm', colour = G.C.CLEAR, padding = 0.03}, nodes = {}}
    end
    return orig(...)
  end
  return true
end

local function install_end_consumeable_dedup()
  if not (G and G.FUNCS and G.FUNCS.end_consumeable) then return false end
  if _G._decidex_end_consumeable_dedup then return false end
  _G._decidex_end_consumeable_dedup = true
  local orig = G.FUNCS.end_consumeable  G.FUNCS.end_consumeable = function(e, delayfac)
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
      -- Drop the reference WITHOUT removing the area, same semantics as
      -- abandon_pack: CardArea:remove() nils .cards, and any queued
      -- use_card restore event still holding this area then crashes on
      -- area.cards[1] (button_callbacks.lua:2283) — 18+ Event Guard drops
      -- per soak traced exactly here. An alive empty area is harmless.
      G.pack_cards = nil
    end
    print("[DecideX Pack Watchdog] removed pack UI outside a pack state")
  end
end

--[[
  v131: synchronous revive of the vanilla event queue.

  A pack whose factory event never ran (see pack_stall_watchdog) is stuck
  because SOME event ahead of it in G.E_MANAGER.queues.base never
  completes, and EventManager:update's blocked-head rule
  (engine/event.lua:194-197) then skips every later blockable event
  forever. One blind `E_MANAGER:update(1/60, true)` kick is not enough —
  the stuck event survives every pass (probe v131: kick fired, queue
  unchanged, packs still fell through to rebuild).

  This walks the base queue MANUALLY with vanilla's exact per-event
  semantics, so the poison is identified precisely:
    * every event is handled exactly as a natural pass would (pcall +
      the Event Guard inside Event:handle still apply);
    * an event whose handle() completes is REMOVED, like vanilla;
    * a blocking IMMEDIATE event that fails to complete in a synchronous
      handle is permanent poison — an immediate either completes or never
      does — and is evicted from the queue (its effects are lost, but an
      event stuck for 1000+s was already doing nothing; probe v131: the
      queue head was an immediate created at TOTAL=4.77s, still there at
      TOTAL=1014s);
    * everything else (delayed events not yet due, the by-design eternal
      splash-spin non-blocking immediate at game.lua:2276) is LEFT ALONE.

  Returns 'revived' if the walk made G.pack_cards appear, 'evicted' if a
  poison event was removed (the chain behind it gets the next pass),
  'stuck' otherwise.
]]
local function kick_pack_queue()
  local q = G.E_MANAGER and G.E_MANAGER.queues and G.E_MANAGER.queues.base
  if not q then return "stuck" end
  G.ARGS.event_manager_update = G.ARGS.event_manager_update or {}
  local results = G.ARGS.event_manager_update
  local blocked = false
  local evicted = false
  local i = 1
  while i <= #q do
    if G.pack_cards then return "revived" end
    local ev = q[i]
    if not blocked or not ev.blockable then
      results.blocking, results.completed, results.time_done, results.pause_skip = false, false, false, false
      local ok = pcall(ev.handle, ev, results)
      if ok and not results.pause_skip then
        if not blocked and results.blocking then blocked = true end
        if results.completed and results.time_done then
          table.remove(q, i)
        else
          if results.blocking and ev.trigger == "immediate" then
            -- Permanent poison: a blocking immediate that just failed to
            -- complete synchronously will fail forever (vanilla's own
            -- immediate branch has no retry semantics). Evict it.
            print("[DecideX Pack Watchdog] evicted stuck blocking immediate" ..
                  " (created_at=" .. tostring(ev.time) ..
                  ", TOTAL=" .. tostring(G.TIMERS and G.TIMERS.TOTAL) ..
                  ", blockable=" .. tostring(ev.blockable) .. ")")
            table.remove(q, i)
            evicted = true
          else
            i = i + 1
          end
        end
      else
        i = i + 1
      end
    else
      i = i + 1
    end
  end
  if G.pack_cards then return "revived" end
  if evicted then return "evicted" end
  return "stuck"
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
    _G._pack_kick_count = nil
    -- v131c: the factory marker is per-episode — a fresh pack of the same
    -- state must get its own native-open attempt.
    _G._pack_factory_seen = nil
    _G._pack_rearm_count = nil
    -- The empty-escape latch MUST reset here too: it used to reset only
    -- when an area became non-empty, so after one escape the latch stayed
    -- set and every LATER pack with lost creation events wedged forever
    -- (observed in soak7: escape fired twice, then never again).
    _G._pack_empty_time = nil
    _G._pack_empty_handled = nil
    _G._pack_mismatch_time = nil
    return false
  end

  local now = (love.timer and love.timer.getTime()) or os.clock()

  -- v137: a stale vanilla restore event (use_card's +0.3s
  -- "G.STATE = prev_state", end_consumeable's +0.2s
  -- "G.STATE = G.GAME.PACK_INTERRUPT") can re-enter the pack we JUST
  -- abandoned. Full recovery churn on such a ghost costs seconds per
  -- cycle (stall tripwire -> STATE_COMPLETE re-arm -> rebuild ->
  -- populate -> auto-close) and ends in another abandon anyway. If we
  -- abandoned within the exit window and no live pack UI exists,
  -- re-abandon in one frame. Any dispatched action clears the window
  -- first (decidex_execute_inner), so a freshly purchased pack is never
  -- mistaken for a ghost.
  if _G._decidex_pack_exit_until and now < _G._decidex_pack_exit_until
      and not G.booster_pack then
    local gpc = G.pack_cards
    if not gpc or not gpc.cards or not gpc.cards[1] then
      print("[DecideX Pack Watchdog] ghost pack state after abandon (stale vanilla restore); re-abandoning")
      abandon_pack()
      return true
    end
  end

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
      -- Contents-vs-state mismatch: a healthy-looking pack whose cards
      -- belong to the WRONG set. This is the poison an abandon leaves
      -- behind: the queued vanilla open/create events for the abandoned
      -- pack still fire and can pair the OLD contents with the NEW state
      -- (probe_diag_v110: G.STATE=PLANET_PACK holding Grim/Incantation —
      -- spectrals). The client then picks cards whose vanilla use crashes
      -- on the empty shop hand. No amount of waiting fixes a poisoned
      -- pack; abandon it so the run re-enters with a fresh one.
      local expected = nil
      if G.STATE == G.STATES.TAROT_PACK then expected = "Tarot"
      elseif G.STATE == G.STATES.PLANET_PACK then expected = "Planet"
      elseif G.STATE == G.STATES.SPECTRAL_PACK then expected = "Spectral"
      elseif G.STATE == G.STATES.STANDARD_PACK then expected = "Standard"
      elseif G.STATE == G.STATES.BUFFOON_PACK then expected = "Joker" end
      local mismatch = false
      if expected then
        for _, pc in ipairs(G.pack_cards.cards) do
          local pset = pc.ability and pc.ability.set
          if expected == "Standard" then
            if pset ~= "Enhanced" and pset ~= "Default" then mismatch = true break end
          elseif pset ~= expected then
            mismatch = true break
          end
        end
      end
      if mismatch then
        if not _G._pack_mismatch_time then
          _G._pack_mismatch_time = now2
          return false
        end
        -- 5s (raised from 2s): right after a state change the NEW pack's UI
        -- creation event may still be pending while G.pack_cards still holds
        -- the PREVIOUS pack's area -- that healthy transition must not be
        -- abandoned (it threw away freshly bought packs on slow hosts).
        -- Real poison (wrong-set contents wedged by an abandon race) never
        -- self-heals, so 5s still stops the card-eating crash loop early.
        if now2 - _G._pack_mismatch_time > 5.0 then
          print("[DecideX Pack Watchdog] " .. tostring(expected) ..
                " state holds foreign-set contents (poisoned pack); abandoning")
          _G._pack_mismatch_time = nil
          _G._pack_empty_time = nil
          abandon_pack()
          return true
        end
        return false
      end
      _G._pack_mismatch_time = nil
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
  -- v139: G.STATE_COMPLETE == true with no factory marker means vanilla
  -- CONSUMED the state-entry gate and the factory event is queued but
  -- still pending — it sits behind the purchase/open event chain
  -- (diag139: base=40 events) and can legitimately take >0.8s to reach.
  -- That is healthy progress, not a stall: the old 0.8s tripwire re-armed
  -- STATE_COMPLETE here, queueing a SECOND factory behind the first and
  -- forcing the rebuild+populate detour on ~60% of packs. Give the
  -- pending factory 2.5s before treating it as lost; the common case now
  -- resolves as a clean native open with the real animation.
  if G.STATE_COMPLETE == true and not _G._pack_factory_seen and stalled_for < 2.5 then
    return false
  end
  -- v131: 4.0s -> 0.8s. Root cause (probe v131 diag, definitive): the
  -- pack factory event (game.lua:3333) DOES run but CRASHES inside the
  -- UIBox definition build (the same UI_definitions/card.lua family the
  -- Event Guard already drops elsewhere, e.g. :1435 blind_prompt_box) —
  -- the guard removes the event with completed=true BEFORE
  -- create_UIBox_*_pack() ever assigns G.pack_cards. The queue is then
  -- CLEAN (base=1 is the by-design eternal splash-spin event,
  -- game.lua:2276), so no queue kick can revive a chain that already
  -- died; rebuild_pack_ui + populate_empty_pack are the actual recovery.
  -- Healthy native fills surface their area in <0.3s, so 0.8s is a safe
  -- tripwire: recovery starts ~0.8s into the pack instead of v130's 4s.
  if stalled_for < 0.8 then return false end

  -- Only act once per stall episode.
  if _G._pack_stall_handled then return false end
  _G._pack_stall_handled = true

  print("[DecideX Pack Watchdog] pack stalled for " .. string.format("%.1f", stalled_for) ..
        "s (state=" .. tostring(G.STATE) .. ", paused=" .. tostring(G.SETTINGS and G.SETTINGS.paused) ..
        ", overlay=" .. tostring(not not G.OVERLAY_MENU) ..
        ", state_complete=" .. tostring(G.STATE_COMPLETE) ..
        ", factory_seen=" .. tostring(_G._pack_factory_seen ~= nil) ..
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

  -- 1.2. v131c: give the VANILLA factory a chance before we rebuild.
  --
  -- The factory event is queued exactly once, when update_<pack> observes
  -- STATE_COMPLETE false. A pack state entered with STATE_COMPLETE already
  -- true (stale from the previous stage) never queues it: the pack waits
  -- for our rebuild, which works but pops the UI in with NO open
  -- animation, needs bridge-populated cards, and is exactly the "卡 UI"
  -- feel reported from production. Re-arm the gate and vanilla opens the
  -- pack natively on the next update pass. Only when the factory provably
  -- never ran for THIS state (the wrapper marks every call, crash or not —
  -- a crashed factory keeps its marker so we fall through to the
  -- kick/rebuild ladder instead of re-arming forever).
  if G.STATE_COMPLETE and _G._pack_factory_seen ~= G.STATE
      and (_G._pack_rearm_count or 0) < 1 then
    -- Budget: ONE re-arm per episode (probe v131c: a tag double-pack with
    -- Sigil/Ouija crash storms re-armed 3x — each re-arm re-queues a FULL
    -- vanilla chain, and the crash churn that follows is what the budget
    -- must bound; after one attempt the kick/rebuild ladder takes over).
    _G._pack_rearm_count = (_G._pack_rearm_count or 0) + 1
    G.STATE_COMPLETE = false
    -- Pre-arm the populate safety net for the fresh native area. Probe
    -- v131c: re-armed opens are NATIVE (rebuild/populate counters hit 0)
    -- but the card-creation events are dead on this path too — native
    -- opens sat EMPTY for the full 12s escape (18 abandons, 13.5s dwells),
    -- while the minority whose creation events survived filled natively.
    -- populate_empty_pack yields to vanilla if the real cards race in
    -- before its 0.6s window; trim_overfull_pack (armed by rebuilt_at)
    -- drops the reverse race; auto-close stays suppressed until populate
    -- resolves, so a recovering pack is never closed early.
    _G._pack_rebuilt_at = now
    _G._pack_populated = nil
    print("[DecideX Pack Watchdog] factory never ran for this pack state; re-arming STATE_COMPLETE for a native open")
    _G._pack_stall_time = now
    _G._pack_stall_handled = nil
    return true
  end

  -- 1.5. v131: kick the vanilla event queue before rebuilding anything.
  --
  -- Production evidence (v130 deep probe: 79/79 packs stalled; probe_diag
  -- v126b: `state_complete=true, pack_size=2, queues=base=1` for 4+s):
  -- the update_<pack> factory event (game.lua:3333, immediate blocking)
  -- sat in G.E_MANAGER.queues.base behind an event that never completed.
  -- EventManager:update's blocked-head rule (engine/event.lua:194-197)
  -- freezes every later blockable event behind it: G.pack_cards never
  -- appears, STATE_COMPLETE stays true, and every pack went
  -- stall->rebuild->populate at 6-9s instead of the native ~1s.
  --
  -- kick_pack_queue() below walks the base queue itself (vanilla
  -- semantics), so the stuck event is IDENTIFIED, not guessed: a blocking
  -- immediate that fails to complete in a synchronous handle() is
  -- permanent poison (an immediate either completes or never does) and is
  -- evicted; everything else is left exactly as vanilla would leave it.
  if G.E_MANAGER and G.E_MANAGER.queues then
    local outcome = "stuck"
    local ok_kick, err_kick = pcall(function() outcome = kick_pack_queue() end)
    if not ok_kick then
      print("[DecideX Pack Watchdog] queue kick failed: " .. tostring(err_kick))
      outcome = "stuck"
    end
    _G._pack_kick_count = (_G._pack_kick_count or 0) + 1
    if G.pack_cards then
      -- Native chain revived: the factory event executed and created the
      -- area. Card-creation events fire next; do NOT rebuild over them.
      -- Arm the populate safety net anyway: if the creation events turn
      -- out to be dead too (they live behind the same broken queue), the
      -- area would otherwise sit empty for 12s before the escape —
      -- populate_empty_pack fills it at +0.6s instead.
      print("[DecideX Pack Watchdog] queue kick revived native pack chain")
      _G._pack_rebuilt_at = now
      _G._pack_populated = nil
      _G._pack_stall_time = nil
      _G._pack_stall_handled = nil
      return true
    end
    if outcome == "evicted" or (_G._pack_kick_count or 0) < 1 then
      -- An eviction frees the queue; the factory event behind it needs
      -- the next pass (natural or forced) to build the area. Give the
      -- chain that window instead of rebuilding over a possibly-live
      -- vanilla chain (a rebuild racing the factory event produces a
      -- second empty area — the wedge the old comments warn about).
      -- Budget: ONE extra window — the diag showed the queue is usually
      -- already clean (the factory died earlier), so the window only
      -- costs 0.8s in the common case and only saves a rebuild in the
      -- rare true-blockage case.
      _G._pack_stall_time = now
      _G._pack_stall_handled = nil
      return true
    end
    print("[DecideX Pack Watchdog] queue kicks exhausted this episode; falling to rebuild")
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

-- Auto-close a consumed booster pack from the bridge side.
--
-- This WASM build's natural close chain is broken: CardArea:remove()
-- nils .cards and vanilla button_callbacks.lua:2283 crashes on the
-- removed area (the Event Guard drops it), so until now every consumed
-- pack waited for the client's empty-pack grace (~800ms) plus a
-- force_leave_pack round trip — ~1.3s per pack, 8+ packs per run.
-- The pack is closed here instead. Measured on this build: vanilla's own
-- programmatic close (G.FUNCS.end_consumeable) queues its state restore
-- behind an event chain that NEVER lands (0/7 self-restores in probe
-- v122, every pack wedged until the client cleaned up at 3-4.2s), so the
-- fast exit uses abandon_pack() — the battle-tested instant path the
-- client's force_leave_pack has driven for weeks: G.STATE is restored
-- synchronously from G.GAME.PACK_INTERRUPT, PACK_INTERRUPT is cleared
-- synchronously, and the orphaned pack UI is handled by the existing
-- pack-UI sweep. Guards, in order:
--   * the pack area held cards this episode (a half-created pack is
--     transiently empty while its card-creation events are queued);
--   * no multi-choice pending: REMOVED in v131 (see the inline comment —
--     an empty area after seen_nonempty already implies all choices spent,
--     and the stuck pack_choices on this build made the old guard refuse
--     to close consumed mega packs);
--   * G.GAME.PACK_INTERRUPT must be set — REMOVED in v131b (diag build:
--     rebuilt packs always have it nil at close time; abandon_pack falls
--     back to the shop on its own);
--   * the empty state is stable for 0.45s (anti-flicker + v137: the
--     queued vanilla restore chain for the last used card lands first);
--   * once per episode.
local function auto_close_consumed_pack()
  if not is_pack_state() then
    _G._pack_seen_nonempty = nil
    _G._pack_auto_closed = nil
    _G._pack_empty_confirm = nil
    return false
  end
  local cards = G.pack_cards and G.pack_cards.cards
  if cards and cards[1] then
    _G._pack_seen_nonempty = true
    _G._pack_empty_confirm = nil
    return false
  end
  if not _G._pack_seen_nonempty then return false end
  -- v131b: the PACK_INTERRUPT refusal is GONE (diag build proved it: on
  -- every REBUILT+populated pack the interrupt is already nil at close
  -- time — this episode's abandon_pack cleared it, and vanilla's queued
  -- 0.3s restore (button_callbacks.lua:2627) consumed whatever the pack
  -- purchase had set). The guard therefore blocked ONLY the packs that
  -- most need a silent close, and every refusal cost a client
  -- force_leave round trip (~0.9s) plus a re-buy loop when the shop
  -- still had the same pack. abandon_pack already handles a nil
  -- interrupt: back = PACK_INTERRUPT or G.STATES.SHOP.
  local now = (love.timer and love.timer.getTime()) or os.clock()
  if _G._pack_rebuilt_at and not _G._pack_populated then return false end
  if not _G._pack_empty_confirm then
    _G._pack_empty_confirm = now
    return false
  end
  -- v137: 0.15 -> 0.45. The queued vanilla restore chain for the last
  -- used pack card lands at ~+0.3s (use_card: +0.2s dissolve, +0.1s
  -- "G.STATE = prev_state"). Closing before it fires raced the event:
  -- the restore then re-wrote the abandoned pack state (ghost churn)
  -- and indexed the cascade-removed area (button_callbacks.lua:2283,
  -- 61 crashes per 40min soak). At 0.45s the restore lands while the
  -- pack is still open: the area is alive-but-empty so the restore
  -- skips gracefully and vanilla's own self-close gets its chance.
  if now - _G._pack_empty_confirm < 0.45 then return false end
  if _G._pack_auto_closed then return false end
  _G._pack_auto_closed = true
  print("[DecideX Bridge] pack consumed (area empty); auto-closing via abandon_pack")
  local ok, err = pcall(function() abandon_pack() end)
  if not ok then
    print("[DecideX Bridge] pack auto-close failed: " .. tostring(err))
  else
    -- Force the next frame's emit: the client must see the pack gone (or
    -- pack_closing=true if the close failed) instead of re-deciding on a
    -- stale snapshot.
    LAST_PACK_SIG = ""
    LAST_STATE_NUM = nil
  end
  return ok
end

-- Drop cards above the pack size from a bridge-populated pack area.
--
-- With populate firing at +0.6s (v131), a vanilla emplace event that was
-- merely DELAYED (not dead) can still land afterwards: our refilled cards
-- plus vanilla's originals would exceed pack_size, and the extras sit
-- unpickable at the end of the area. Trim to pack_size every tick until
-- the area fits. No-op unless this episode was bridge-populated/kicked.
local function trim_overfull_pack()
  if not is_pack_state() then return false end
  if not (_G._pack_populated or _G._pack_rebuilt_at) then return false end
  local cards = G.pack_cards and G.pack_cards.cards
  if not (cards and cards[1]) then return false end
  local size = tonumber(G.GAME and G.GAME.pack_size) or 2
  if #cards <= size then return false end
  local trimmed = 0
  for i = #cards, size + 1, -1 do
    local c = cards[i]
    if c then pcall(function()
      G.pack_cards:remove_card(c)
      c:remove()
    end) end
    trimmed = trimmed + 1
  end
  if trimmed > 0 then
    print("[DecideX Pack Watchdog] trimmed " .. tostring(trimmed) ..
          " overfull pack card(s) (late vanilla emplace after bridge populate)")
    LAST_PACK_SIG = ""
  end
  return trimmed > 0
end

-- Fill a rebuilt pack area whose vanilla card-creation events are dead.
--
-- Vanilla creates pack cards in QUEUED events (card.lua:1721: delay
-- 1.3*sqrt(GAMESPEED) = 1.84s at the bridge's forced speed 4, then a second
-- event that emplaces the locals into G.pack_cards gated on the pack UIBox
-- sliding in). When the watchdog rebuilt the UI after those events already
-- completed into a since-removed area — or G.STATE_COMPLETE stuck true
-- blocked the factory from ever running — the rebuilt area stays empty
-- forever: probe v122 saw only 5/22 rebuilds ever show cards, every other
-- pack was abandoned at 6s with its contents lost.
--
-- This mirrors vanilla's own creation loop (same sets, voucher odds, seeds
-- and edition polling) into the rebuilt area, 0.6s after the rebuild —
-- v131: production showed the vanilla creation chain dead in 79/79
-- episodes, so the old 1.5s wait was pure dead time; a merely-delayed
-- vanilla emplace that lands late is trimmed by trim_overfull_pack
-- instead. If vanilla's emplace event fires before the window, the
-- non-empty check below yields to the originals.
local function populate_empty_pack()
  if not is_pack_state() then
    _G._pack_rebuilt_at = nil
    _G._pack_populated = nil
    return false
  end
  if not (_G._pack_rebuilt_at and not _G._pack_populated) then return false end
  local now = (love.timer and love.timer.getTime()) or os.clock()
  -- v131: 1.5s -> 0.6s. The prod deep probe showed the rebuilt area empty
  -- in 79/79 episodes: vanilla's creation events are dead behind the same
  -- broken queue that killed the factory event, so the 1.5s wait was pure
  -- dead time on every refill. 0.6s still outraces the native emplace
  -- event (which lands >= 1.3s after creation at GAMESPEED 1), and the
  -- duplicate class is covered by trim_overfull_pack below either way.
  if now - _G._pack_rebuilt_at < 0.6 then return false end
  if not (G.pack_cards and G.booster_pack) then return false end
  if G.pack_cards.cards and G.pack_cards.cards[1] then
    -- Vanilla delivered after all; nothing to fill.
    _G._pack_populated = true
    return false
  end
  -- Trim late vanilla emplaces: our cards + vanilla's would exceed the
  -- pack size. Extras sit at the end (vanilla emplaces its full set).
  local size = tonumber(G.GAME and G.GAME.pack_size) or 2
  local cards = G.pack_cards.cards or {}
  if #cards > size then
    for i = #cards, size + 1, -1 do
      local c = cards[i]
      if c then pcall(function()
        G.pack_cards:remove_card(c)
        c:remove()
      end) end
    end
  end
  _G._pack_populated = true
  print("[DecideX Pack Watchdog] rebuilt area still empty after 0.6s; populating " ..
        tostring(size) .. " cards (vanilla creation events dead)")
  local ok, err = pcall(function()
    for i = 1, size do
      local card = nil
      if G.STATE == G.STATES.TAROT_PACK then
        if G.GAME.used_vouchers.v_omen_globe and pseudorandom('omen_globe') > 0.8 then
          card = create_card("Spectral", G.pack_cards, nil, nil, true, true, nil, 'ar2')
        else
          card = create_card("Tarot", G.pack_cards, nil, nil, true, true, nil, 'ar1')
        end
      elseif G.STATE == G.STATES.PLANET_PACK then
        local forced_planet = nil
        if G.GAME.used_vouchers.v_telescope and i == 1 then
          local _hand, _tally = nil, 0
          for _, v in ipairs(G.handlist) do
            if G.GAME.hands[v].visible and G.GAME.hands[v].played > _tally then
              _hand = v
              _tally = G.GAME.hands[v].played
            end
          end
          if _hand then
            for _, v in pairs(G.P_CENTER_POOLS.Planet) do
              if v.config.hand_type == _hand then forced_planet = v.key end
            end
          end
        end
        card = create_card("Planet", G.pack_cards, nil, nil, true, true, forced_planet, 'pl1')
      elseif G.STATE == G.STATES.SPECTRAL_PACK then
        card = create_card("Spectral", G.pack_cards, nil, nil, true, true, nil, 'spe')
      elseif G.STATE == G.STATES.STANDARD_PACK then
        card = create_card((pseudorandom(pseudoseed('stdset'..G.GAME.round_resets.ante)) > 0.6) and "Enhanced" or "Base",
                           G.pack_cards, nil, nil, nil, true, nil, 'sta')
        local edition = poll_edition('standard_edition'..G.GAME.round_resets.ante, 2, true)
        card:set_edition(edition)
      elseif G.STATE == G.STATES.BUFFOON_PACK then
        card = create_card("Joker", G.pack_cards, nil, nil, true, true, nil, 'buf')
      end
      if card then G.pack_cards:emplace(card) end
    end
  end)
  if ok then
    LAST_PACK_SIG = ""
    print("[DecideX Pack Watchdog] pack populated from bridge")
  else
    print("[DecideX Pack Watchdog] pack populate failed: " .. tostring(err))
  end
  return ok
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
    -- Vanilla G.FUNCS.cash_out ends with this blind-ante tail
    -- (button_callbacks.lua:2964-2969). Short-circuiting straight to SHOP
    -- used to leave a Boss win's blind_states at Small=Big=Boss='Defeated';
    -- create_UIBox_blind_select() then falls through its "Small? Big? else
    -- Boss" ladder and offers the SAME boss again at the new ante, with
    -- blind_ante/blind_tags never refreshed (observed live: ante 2
    -- re-offering bl_psychic, then ante 3 re-offering bl_pillar).
    if G.GAME then
      local resets = G.GAME.round_resets
      local states = resets and resets.blind_states
      if resets and states and states.Boss == 'Defeated' then
        resets.blind_ante = resets.ante
        if resets.blind_tags and type(get_next_tag_key) == 'function' then
          resets.blind_tags.Small = get_next_tag_key()
          resets.blind_tags.Big = get_next_tag_key()
        end
      end
      if type(reset_blinds) == 'function' then
        local ok_reset, err_reset = pcall(reset_blinds)
        if not ok_reset then
          print("[DecideX Blind Guard] reset_blinds failed: " .. tostring(err_reset))
        end
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

-- Remember which blind this run actually played, and in which ante. Keyed
-- weakly by G.GAME so a fresh run or a loaded save starts without a record;
-- ante is kept because a blind is only "just played" inside the ante it was
-- played in (see last_played_blind_type).
local function note_played_blind(key)
  if not (G and G.GAME and key) then return end
  local store = _G._decidex_played_by_game
  if not store then
    store = setmetatable({}, { __mode = 'k' })
    _G._decidex_played_by_game = store
  end
  local resets = G.GAME.round_resets
  store[G.GAME] = {
    key = key,
    ante = resets and resets.ante,
    round = G.GAME.round,
  }
end

-- Re-derive "this blind is beaten" from the blind that was actually played.
--
-- Vanilla end_round() marks the blind 'Defeated' by comparing
-- G.GAME.round_resets.blind against the G.P_BLINDS prototypes
-- (functions/state_events.lua:258-268). When that identity is lost -- a
-- bridge-driven select, a resumed save, an end_round event chain cleared by
-- a recovery path -- the mark silently never lands. blind_on_deck is then
-- recomputed from stale states, so the SAME blind is offered again while
-- G.GAME.round climbs: observed live as 17 Big blinds at ante 1 over 1500s,
-- Boss never faced, ante never advanced and therefore no possible GAME_OVER
-- (a run that can never end). The mark is idempotent, so this runs once on
-- entering ROUND_EVAL and only when the state is actually missing.
local function ensure_played_blind_defeated()
  if not (G and G.GAME and G.STATES and G.STATE == G.STATES.ROUND_EVAL) then return end
  local resets = G.GAME.round_resets
  local states = resets and resets.blind_states
  local blind = G.GAME.blind
  if not (states and blind and blind.get_type) then return end
  local ok_type, blind_type = pcall(function() return blind:get_type() end)
  if not ok_type or not blind_type then return end
  local key = (blind_type == 'Small' and 'Small') or (blind_type == 'Big' and 'Big') or 'Boss'
  note_played_blind(key)
  if states[key] == 'Defeated' then return end
  local previous = states[key]
  states[key] = 'Defeated'
  print("[DecideX Blind Guard] marked " .. key .. " Defeated (was " ..
    tostring(previous) .. ", blind=" .. tostring(blind.key) .. ")")
end

-- Vanilla's blind-select ladder (functions/UI_definitions.lua:1441-1443).
local function blind_ladder(states)
  local function skipped(v)
    return v == 'Defeated' or v == 'Skipped' or v == 'Hide'
  end
  if not skipped(states.Small) then return 'Small' end
  if not skipped(states.Big) then return 'Big' end
  return 'Boss'
end

-- Which blind did this run just finish? Only a blind recorded at ROUND_EVAL in
-- the CURRENT ante counts: round_resets.blind keeps the last played blind
-- across the ante rollover, so after reset_blinds() it still names the old
-- Boss while the ladder legitimately offers the new ante's Small blind (and,
-- when both Small and Big are skipped, that Boss again). The old identity
-- test mistook those for a stale re-offer and marked the unplayed Boss
-- 'Defeated' -- 17 times in one blind select, observed live.
local function last_played_blind_type()
  local store = _G._decidex_played_by_game
  local rec = store and G.GAME and store[G.GAME]
  local resets = G.GAME and G.GAME.round_resets
  if not (rec and resets) then return nil end
  if rec.ante ~= resets.ante then return nil end
  return rec.key
end

local function drop_blind_select_ui(reason)
  if G.blind_select and not G.blind_select.REMOVED then
    pcall(function() G.blind_select:remove() end)
  end
  G.blind_select = nil
  for _, opt in pairs(G.blind_select_opts or {}) do
    if opt and not opt.REMOVED then
      pcall(function() opt:remove() end)
    end
  end
  G.blind_select_opts = nil
  if G.blind_prompt_box and not G.blind_prompt_box.REMOVED then
    pcall(function() G.blind_prompt_box:remove() end)
  end
  G.blind_prompt_box = nil
  if reason then
    -- A wedged pack can leave several late builds queued; report at most one
    -- line per 5s so the production log stays readable.
    local now = (love.timer and love.timer.getTime()) or os.clock()
    if not _G._blind_drop_last_print or now - _G._blind_drop_last_print > 5.0 then
      _G._blind_drop_last_print = now
      print("[DecideX Blind Guard] dropped blind-select UI (" .. reason .. ")")
    end
  end
end

-- Generic stale-UI sweep: every UIBox global must be removed when the game
-- leaves the state that owns it.
--
-- Vanilla owns these removals itself (game.lua: update_shop/update_hand_played/
-- update_draw_to_hand/update_new_round/update_blind_select/update_round_eval
-- all start with `if self.shop then self.shop:remove(); self.shop = nil end`;
-- G.FUNCS.cash_out removes G.round_eval at button_callbacks.lua:2940;
-- end_consumeable removes G.booster_pack at 2591) -- but on this WASM build
-- those removals run inside queued events that the Event Guard drops when a
-- sibling crashed. The global then survives the transition and its UIBox
-- keeps rendering ON TOP of the next stage: observed as the shop panel still
-- covering the hand in BLIND_SELECT, and the round-eval panel stuck over the
-- shop. Vanilla never shows these UIs in any other state, so "alive while the
-- state is different" is always leftover, never a legitimate draw.
--
-- Exceptions keep the sweep conservative:
--   - G.shop stays during ROUND_EVAL/HAND_PLAYED/DRAW_TO_HAND/NEW_ROUND: the
--     next-blind flow rebuilds it legitimately inside the shop-adjacent
--     transition (vanilla only removes it when those updates run, which they
--     do on their own schedule).
--   - A wipe/load lock means a screen transition is mid-flight; removing
--     UIBoxes then desyncs G.CONTROLLER (same guard as the blind-select
--     rebuild at line 2408).
--   - A 1.2s settle window after a state change lets vanilla's own removal
--     events run first; we only clean up what they left behind.
local function sweep_stale_ui()
  if not (G and G.I and G.I.UIBOX) then return end
  local now = (love.timer and love.timer.getTime()) or os.clock()
  if G.CONTROLLER and G.CONTROLLER.locks and (G.CONTROLLER.locks.wipe or G.CONTROLLER.locks.load) then
    return
  end
  if _G._sweep_state_enter == nil or (LAST_STATE_NUM and G.STATE ~= LAST_STATE_NUM) then
    _G._sweep_state_enter = now
    return
  end
  if now - _G._sweep_state_enter < 1.2 then return end

  local victims = {}
  -- G.shop is vanilla-only in SHOP (game.lua:3059), but a pack legitimately
  -- opens ON TOP of a live shop and returns to it on exit (vanilla's
  -- update_shop keeps G.shop while TAROT/PLANET/SPECTRAL/STANDARD/BUFFOON
  -- pack states run, and end_consumeable slides the SAME shop back in at
  -- button_callbacks.lua:2603). PLAY_TAROT (using a consumable) can also
  -- happen over the shop. Sweep it only in truly shop-less states.
  local shop_ok = (G.STATE == G.STATES.SHOP) or is_pack_state() or
    (G.STATE == G.STATES.PLAY_TAROT) or (G.STATE == G.STATES.ROUND_EVAL) or
    (G.STATE == G.STATES.HAND_PLAYED) or (G.STATE == G.STATES.DRAW_TO_HAND) or
    (G.STATE == G.STATES.NEW_ROUND)
  if G.shop and not G.shop.REMOVED and not shop_ok then
    victims[#victims + 1] = G.shop
    G.shop = nil
  end
  -- G.round_eval is vanilla-only in ROUND_EVAL (game.lua:3298) and during the
  -- cash-out animation (its removal event is queued there).
  if G.round_eval and not G.round_eval.REMOVED and G.STATE ~= G.STATES.ROUND_EVAL and
     not G.cashing_out then
    victims[#victims + 1] = G.round_eval
    G.round_eval = nil
  end
  -- G.booster_pack outside pack states is handled by sweep_pack_ui_invariants
  -- (nils G.pack_cards too); do not double-remove here.
  if G.blind_select and not G.blind_select.REMOVED and G.STATE == G.STATES.BLIND_SELECT then
    -- Handled by drop_blind_select_ui's inverse (stale check above); nothing here.
  end
  -- The shop sign rides along with the shop panel; vanilla removes it in
  -- toggle_shop (button_callbacks.lua:2508).
  if G.SHOP_SIGN and not G.SHOP_SIGN.REMOVED and G.shop == nil and not shop_ok then
    victims[#victims + 1] = G.SHOP_SIGN
    G.SHOP_SIGN = nil
  end

  if #victims == 0 then return end
  for _, ub in ipairs(victims) do
    pcall(function() ub:remove() end)
  end
  -- Reset the settle clock so a second leak found next frame still gets the
  -- full settle window before the next removal (prevents rapid-fire sweeps).
  _G._sweep_state_enter = now
  if not _G._sweep_last_print or now - _G._sweep_last_print > 5.0 then
    _G._sweep_last_print = now
    print("[DecideX UI Sweep] removed " .. #victims ..
      " stale UIBox(es) in " .. current_state_name())
  end
  -- Force a fresh snapshot so the client immediately re-decides on the
  -- cleaned screen instead of acting on the stale visual.
  LAST_HAND_SIG = ""
  LAST_SHOP_SIG = ""
  LAST_BLIND_SIG = ""
  LAST_EVAL_SIG = ""
  LAST_PACK_SIG = ""
end

-- Keep blind progression monotonic while the blind-select UI is up.
--
-- Vanilla queues the blind-select builder inside two nested events
-- (game.lua:3254-3290). A pack overlay blocks that queue, so the builder can
-- land in the MIDDLE of the next round, when blind_states[on_deck] is
-- 'Current': the ladder then re-picks the blind that was just beaten and
-- create_UIBox_blind_choice() rewrites its state to 'Select'
-- (UI_definitions.lua:1489), un-defeating it. From then on every ladder run
-- picks that same blind, G.GAME.round climbs, the Boss is never faced and the
-- ante never advances -- observed live as rounds 3..8 all facing the same Big
-- blind at ante 1 (a run that cannot end). Two invariants restore progress:
-- the blind just played is 'Defeated' at blind select, and blind_on_deck is
-- exactly what the vanilla ladder derives from the (repaired) states.
local function repair_blind_select_progress()
  if not (G and G.GAME and G.STATES and G.STATE == G.STATES.BLIND_SELECT) then return end
  -- facing_blind means G.FUNCS.select_blind already accepted the pick: it has
  -- just written blind_states[on_deck] = 'Current' and the round starts in the
  -- same event pass. Repairing here would mistake that fresh 'Current' for a
  -- blind that was beaten and skip the blind the player just chose.
  if G.GAME.facing_blind then return end
  local resets = G.GAME.round_resets
  local states = resets and resets.blind_states
  if not states then return end
  local changes = {}
  local rebuilt_needed = false
  local on_deck = G.GAME.blind_on_deck

  local function mark_defeated(key, why)
    changes[#changes + 1] = key .. "=" .. tostring(states[key]) .. "->Defeated" .. (why or "")
    states[key] = 'Defeated'
    rebuilt_needed = true
  end

  -- create_UIBox_blind_choice() only ever marks the blind it is offering, so a
  -- 'Select' on any other blind is a leftover from a delayed build (vanilla
  -- queues the builder inside two nested events; a pack overlay can land it in
  -- the middle of the next round).
  if on_deck ~= nil then
    for _, key in ipairs({ 'Small', 'Big', 'Boss' }) do
      local v = states[key]
      if (v == 'Select' or v == 'Current') and key ~= on_deck then
        mark_defeated(key)
      end
    end
  end
  -- ...and the offered blind must not be the one this ante just played. The
  -- record is per-ante on purpose: skipping both blinds legitimately re-offers
  -- the previous ante's Boss, and round_resets.blind still names it then.
  local last = last_played_blind_type()
  if last and last == on_deck and (states[last] == 'Select' or states[last] == 'Current') then
    mark_defeated(last, " (already played)")
  end
  local expected = blind_ladder(states)
  if G.GAME.blind_on_deck ~= expected then
    changes[#changes + 1] = "on_deck " .. tostring(G.GAME.blind_on_deck) .. "->" .. expected
    -- A non-nil on_deck means an earlier builder already bound its options to
    -- the wrong blind. nil is the pre-build state (or a fresh run): the box
    -- either does not exist yet or was built from this same ladder.
    if G.GAME.blind_on_deck ~= nil then rebuilt_needed = true end
    G.GAME.blind_on_deck = expected
  end
  if #changes == 0 then return end
  print("[DecideX Blind Guard] blind select repaired: " .. table.concat(changes, ", ") ..
    " (round=" .. tostring(G.GAME.round) .. ", ante=" .. tostring(resets.ante) .. ")")
  if not rebuilt_needed then return end
  drop_blind_select_ui("rebuilding for repaired on_deck")
  -- The watcher rebuilds in the same frame when the enter stamp is old.
  _G._blind_select_enter_time = ((love.timer and love.timer.getTime()) or os.clock()) - 10
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

  -- §D actual-action backfill: when no explicit actual action was passed,
  -- use the one recorded at play/discard execution time. The recorded cards
  -- left G.hand by the time the HAND_PLAYED/DRAW_TO_HAND snapshot is built,
  -- so match each recorded card (rank:suit:enhancement) against the hand
  -- snapshot to recover the ORIGINAL positions. Consumers treat
  -- actual.cards as 0-BASED indices into the emitted "hand" array (see
  -- balatro_web/app.py _get_snapshot_meta and the state-0002 fixture), so
  -- the Lua 1-based position is stored minus one. Only entries whose cards
  -- are ALL recovered are emitted -- a partial match must not fabricate
  -- indices. Enhancement is matched exactly first, then ignored as a
  -- fallback (enhancement names may differ across save/load round-trips).
  if actual == nil then
    local rec = _G._decidex_actual_action
    if rec and type(rec.cards) == "table" and rec.cards[1] then
      local hand_cards = (G.hand and G.hand.cards) or {}
      local used = {}
      local indices = {}
      local complete = true
      for _, rc in ipairs(rec.cards) do
        local found = nil
        -- First pass: identity + enhancement must both match.
        for i, c in ipairs(hand_cards) do
          if not used[i] and c.base and tostring(c.base.value or "") == rc.r
              and tostring(c.base.suit or "") == rc.s then
            local ce = (c.ability and c.ability.name) or ""
            if ce == (rc.e or "") then found = i break end
          end
        end
        -- Second pass: rank+suit alone (enhancement drifted).
        if not found then
          for i, c in ipairs(hand_cards) do
            if not used[i] and c.base and tostring(c.base.value or "") == rc.r
                and tostring(c.base.suit or "") == rc.s then
              found = i break
            end
          end
        end
        if found then
          used[found] = true
          indices[#indices + 1] = found - 1
        else
          complete = false
          break
        end
      end
      if complete then
        actual = { type = tostring(rec.type or ""), cards = indices }
      end
    end
  end

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

  -- Levelled hands (Planet cards): the client predicts scores with these
  -- instead of the level-1 table. Only hands above level 1 are emitted.
  local hand_levels_parts = {}
  for hname, hinfo in pairs((G.GAME and G.GAME.hands) or {}) do
    local hlvl = tonumber(hinfo.level) or 1
    if hlvl > 1 then
      hand_levels_parts[#hand_levels_parts + 1] = jstr(hname) .. ':[' .. hlvl .. ',' .. (tonumber(hinfo.chips) or 0) .. ',' .. (tonumber(hinfo.mult) or 0) .. ']'
    end
  end
  parts[#parts + 1] = '"hand_levels":{' .. table.concat(hand_levels_parts, ",") .. '}'

  -- Decision-support counters (Supernova/Card Sharp read per-type hand play
  -- counts; Erosion needs the full-deck deficit; Fortune Teller reads tarot
  -- usage; The Idol needs its rotating rank/suit target; the client prioritizes
  -- Planet packs by hand usage). All values are already maintained by vanilla,
  -- so emitting them is cheap and side-effect free.
  local played_parts = {}
  local played_round_parts = {}
  for hname, hinfo in pairs((G.GAME and G.GAME.hands) or {}) do
    local played = tonumber(hinfo.played) or 0
    local played_round = tonumber(hinfo.played_this_round) or 0
    if played > 0 then played_parts[#played_parts + 1] = jstr(hname) .. ':' .. tostring(played) end
    if played_round > 0 then played_round_parts[#played_round_parts + 1] = jstr(hname) .. ':' .. tostring(played_round) end
  end
  parts[#parts + 1] = '"hand_played":{' .. table.concat(played_parts, ",") .. '}'
  parts[#parts + 1] = '"hand_played_round":{' .. table.concat(played_round_parts, ",") .. '}'
  parts[#parts + 1] = '"starting_deck_size":' .. tostring((G.GAME and G.GAME.starting_deck_size) or 52)
  parts[#parts + 1] = '"deck_cards_total":' .. tostring((G.playing_cards and #G.playing_cards) or 52)
  parts[#parts + 1] = '"tarots_used":' .. tostring((G.GAME and G.GAME.consumeable_usage_total and G.GAME.consumeable_usage_total.tarot) or 0)
  local idol_card = (G.GAME and G.GAME.current_round and G.GAME.current_round.idol_card) or nil
  if idol_card and idol_card.rank and idol_card.suit then
    parts[#parts + 1] = '"idol_rank":' .. jstr(RANK_MAP[idol_card.rank] or tostring(idol_card.rank))
    parts[#parts + 1] = '"idol_suit":' .. jstr(SUIT_MAP[idol_card.suit] or tostring(idol_card.suit))
  end

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
  -- jmap serializes table values through jstr, which returns "null" for any
  -- table -- so actual.cards (an array) must be serialized with jtable here,
  -- exactly like the original action-snapshot emitter did.
  parts[#parts + 1] = '"actual":' ..
    (actual and ('{"type":' .. jstr(tostring(actual.type or "")) .. ',"cards":'
      .. jtable(actual.cards or {}) .. '}') or "null")

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
    -- Epoch: bump whenever the pack AREA identity changes. Every freshly
    -- created pack gets a new CardArea, so each episode is unambiguously
    -- numbered for the client's per-episode latches -- immune to skipped
    -- JS steps, state flicker, and stale timers.
    if G.pack_cards ~= _G._decidex_epoch_area then
      _G._decidex_epoch_area = G.pack_cards
      _G._decidex_pack_epoch = (_G._decidex_pack_epoch or 0) + 1
    end
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
  -- The bridge's auto-close already queued this pack's exit
  -- (end_consumeable restores the state ~0.6s out). The client must wait
  -- instead of force-leaving: a second exit (abandon_pack) races the
  -- queued restore and can write nil into G.STATE.
  parts[#parts + 1] = '"pack_closing":' .. ((_G._pack_auto_closed and is_pack_state) and "true" or "false")
  parts[#parts + 1] = '"pack_epoch":' .. tostring(_G._decidex_pack_epoch or 0)

  local won = (G.GAME and G.GAME.won) or false
  parts[#parts + 1] = '"won":' .. (won and "true" or "false")

  return "{" .. table.concat(parts, ",") .. "}"
end

local LAST_STATE_NUM = nil
local LAST_HAND_SIG = ""
local LAST_SHOP_SIG = ""
local LAST_BLIND_SIG = ""
local LAST_EVAL_SIG = ""
-- LAST_PACK_SIG is forward-declared near the top of the file:
-- auto_close_consumed_pack must reset it to force a re-emit with
-- pack_closing=true (the pack signature is unchanged by the close, and
-- without a fresh snapshot the client would force-leave a closing pack).

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

  -- Latch hygiene: _facing_blind_since is only meaningful inside
  -- BLIND_SELECT; resetting it here (rather than inside the select gate's
  -- guard) removes the cross-episode stale timestamp that made the first
  -- select after a stuck episode clear a legitimately fresh flag.
  if G.STATE ~= G.STATES.BLIND_SELECT then
    _G._facing_blind_since = nil
  end

  if G.STATE == G.STATES.DRAW_TO_HAND then
    _G._hand_discard_saw_draw = true
    if not _G._draw_to_hand_enter_time then
      _G._draw_to_hand_enter_time = now
      _G._last_draw_queue_len = nil
      _G._last_draw_queue_progress = now
    end

    -- Queue-progress-agnostic escape: post pack-exit chaos keeps the event
    -- queue churning (refreshing the progress timer) while the state never
    -- advances - observed 30s DRAW_TO_HAND stalls with a full hand. Once
    -- the hand is full, the draw is DONE regardless of queue activity.
    local dhf_limit = (G.hand and G.hand.config and G.hand.config.card_limit) or 8
    local dhf_drawn = (G.hand and G.hand.cards and #G.hand.cards) or 0
    if dhf_drawn >= dhf_limit then
      if not _G._draw_hand_full_time then _G._draw_hand_full_time = now end
      if now - _G._draw_hand_full_time > 20.0 then
        _G._draw_hand_full_time = nil
        _G._draw_to_hand_enter_time = nil
        print("[DecideX Bridge] Watchdog: hand full for 20s in DRAW_TO_HAND, advancing to SELECTING_HAND")
        G.STATE = G.STATES.SELECTING_HAND
        G.STATE_COMPLETE = false
        if G.GAME and G.GAME.blind and G.GAME.blind.drawn_to_hand then
          pcall(function() G.GAME.blind:drawn_to_hand() end)
        end
        return
      end
    else
      _G._draw_hand_full_time = nil
    end

    local base_len = (G.E_MANAGER and G.E_MANAGER.queues and G.E_MANAGER.queues.base and #G.E_MANAGER.queues.base) or 0
    if base_len ~= _G._last_draw_queue_len then
      _G._last_draw_queue_len = base_len
      _G._last_draw_queue_progress = now
    end

    -- Draw-progress stall: the queue-length timer above only measures
    -- queue ACTIVITY, and the base queue churns on unrelated background
    -- events (observed base=30-38 during a stall), so the 12s escape never
    -- armed while zero cards were dealt for 49s (soak19 986s: STATE=3,
    -- STATE_COMPLETE=true, hand=0). Track the hand COUNT itself: if the
    -- dealing made no progress for 12s and the hand is not full, re-trigger
    -- the draw regardless of queue noise.
    if dhf_drawn ~= (_G._last_draw_hand_count or -1) then
      _G._last_draw_hand_count = dhf_drawn
      _G._last_draw_hand_progress = now
    end
    if (now - (_G._last_draw_hand_progress or now) > 12.0) and dhf_drawn < dhf_limit then
      _G._last_draw_hand_progress = now
      if G.deck and G.deck.cards and #G.deck.cards > 0 then
        print("[DecideX Bridge] Watchdog: DRAW_TO_HAND dealing stalled (hand=" ..
          tostring(dhf_drawn) .. "/" .. tostring(dhf_limit) ..
          ", queue=" .. tostring(base_len) .. "); re-triggering draw_from_deck_to_hand")
        pcall(function() G.FUNCS.draw_from_deck_to_hand(nil) end)
      else
        print("[DecideX Bridge] Watchdog: DRAW_TO_HAND dealing stalled with empty deck; advancing to SELECTING_HAND")
        G.STATE = G.STATES.SELECTING_HAND
        G.STATE_COMPLETE = false
        if G.GAME and G.GAME.blind and G.GAME.blind.drawn_to_hand then
          pcall(function() G.GAME.blind:drawn_to_hand() end)
        end
      end
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
    _G._draw_hand_full_time = nil
    _G._last_draw_queue_len = nil
    _G._last_draw_queue_progress = nil
    _G._last_draw_hand_count = nil
    _G._last_draw_hand_progress = nil
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
    -- §D: the recorded actual action belongs to the round that just ended;
    -- a stale record must not leak into the next round's snapshots.
    _G._decidex_actual_action = nil
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

  -- A delayed blind-select build can leave a blind-select UI alive outside
  -- BLIND_SELECT; repair_nil_state() would then infer BLIND_SELECT from it and
  -- abandon whatever is actually on screen (observed: a pack). Vanilla only
  -- ever shows this UI in BLIND_SELECT, so anything else is leftover.
  if G.STATE ~= G.STATES.BLIND_SELECT and G.blind_select and not G.blind_select.REMOVED then
    drop_blind_select_ui("stale UI in " .. current_state_name())
  end
  -- Generic sweep for every other cross-stage UI leak (shop panel over the
  -- next blind, round-eval panel over the shop); full rationale on the
  -- function.
  pcall(sweep_stale_ui)
  local ok_blind_repair, err_blind_repair = pcall(repair_blind_select_progress)
  if not ok_blind_repair then
    print("[DecideX Blind Guard] repair failed: " .. tostring(err_blind_repair))
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
      -- On the entry frame vanilla has already declared the round won, but
      -- cash_out has not run yet, so a missing 'Defeated' mark is safe to
      -- repair here (never after cash_out's reset_blinds()).
      local ok_guard, err_guard = pcall(ensure_played_blind_defeated)
      if not ok_guard then
        print("[DecideX Blind Guard] guard failed: " .. tostring(err_guard))
      end
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
    -- Highlight state MUST be part of the signature: select_pack_card flips
    -- c.highlighted without changing the card count or names, and without it
    -- no [DECIDEX_STATE] snapshot is emitted after a successful selection --
    -- the AutoPilot's state-change unlock never fires and every selection
    -- idled until the 3.8s watchdog force-clear (6-9 idle locks per soak).
    local pack_sig_parts = { tostring(#p_cards) }
    for i, c in ipairs(p_cards) do
      local cname = (c.ability and c.ability.name) or ""
      local hl = (c.highlighted and 1 or 0)
      pack_sig_parts[#pack_sig_parts + 1] = i .. ":" .. cname .. ":" .. hl
    end
    -- The hand MUST be part of the signature too: cards can be drawn into
    -- G.hand DURING a pack (recovery draws, queued blind-start draws), and
    -- without a re-emit the client keeps the entry snapshot -- where the
    -- hand was empty. Every hand-consuming pack card (Cryptid, Medium,
    -- Familiar/Grim/Incantation...) is then judged unusable against a hand
    -- that actually holds 8 cards, and the pack wedges in
    -- "waiting for readiness" until a generic timeout.
    local h_cards = (G.hand and G.hand.cards) or {}
    pack_sig_parts[#pack_sig_parts + 1] = "H" .. tostring(#h_cards)
    for i, c in ipairs(h_cards) do
      local r = c.base and c.base.value or ""
      local s = c.base and c.base.suit or ""
      local enh = (c.ability and c.ability.name) or ""
      pack_sig_parts[#pack_sig_parts + 1] = "h" .. i .. ":" .. r .. ":" .. s .. ":" .. enh .. ":" .. (c.highlighted and "1" or "0")
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

-- v134: one dispatch per card object for the NON-idempotent shop/hand
-- actions (buy, sell, use). Same bug class as the v133 pack-pick fix: the
-- card/object leaves its area via a QUEUED vanilla event, and until it
-- lands the snapshot is unchanged — a re-dispatch runs the action a SECOND
-- time (double charge on buy, double refund on sell, double effect on a
-- hand consumable such as a planet level-up or a Hermit payout).
-- sort_id is object-unique per session, so a dead entry can never block a
-- different card; the set grows one byte per dispatched card and is capped.
local function decidex_once_refused(c, label)
  if c and c.sort_id then
    _G._decidex_once_ids = _G._decidex_once_ids or {}
    if _G._decidex_once_ids[c.sort_id] then
      return false, "duplicate " .. label ..
        " in progress (card already dispatched once)"
    end
  end
  return true
end

local function decidex_once_mark(c)
  if c and c.sort_id then
    _G._decidex_once_ids = _G._decidex_once_ids or {}
    if _G._decidex_once_count and _G._decidex_once_count > 512 then
      _G._decidex_once_ids = {}
      _G._decidex_once_count = 0
    end
    if not _G._decidex_once_ids[c.sort_id] then
      _G._decidex_once_ids[c.sort_id] = true
      _G._decidex_once_count = (_G._decidex_once_count or 0) + 1
    end
  end
end

local function decidex_once_unmark(c)
  if c and c.sort_id and _G._decidex_once_ids then
    if _G._decidex_once_ids[c.sort_id] then
      _G._decidex_once_ids[c.sort_id] = nil
      _G._decidex_once_count = math.max((_G._decidex_once_count or 1) - 1, 0)
    end
  end
end

-- Programmatic execution of actions
local function decidex_execute_inner(action, args)
  if not G then return false, "no G" end
  args = args or {}

  -- v137: any dispatched action proves a live decision-maker, so the
  -- post-abandon exit window no longer applies: without this, a pack
  -- legitimately purchased seconds after an abandon would be killed by
  -- the ghost-state guard in pack_stall_watchdog. select_pack_card is
  -- the exception — it keeps its own post-abandon gate below, whose
  -- whole job is refusing a stuck retry that lands in a resurrected
  -- empty pack.
  if action ~= "select_pack_card" then
    _G._decidex_pack_exit_until = nil
  end

  -- §D actual-action backfill: record what was really selected at the
  -- moment of the action. By the time the snapshot is emitted the selected
  -- cards have already left G.hand (the play/discard animation moved them
  -- to G.play/G.discard), so a HAND_PLAYED snapshot could only report an
  -- empty "actual" -- every history entry degenerated to "打出 0 张"/
  -- "弃牌 0 张" with no cards, which destroyed the advisor-vs-player
  -- pairing value. Card identity is captured HERE (rank/suit/enhancement),
  -- then matched against the emitted hand by build_state_json, so the
  -- recorded action survives into the snapshot with real cards.
  local function capture_actual(kind)
    local picked = {}
    for _, c in ipairs((G.hand and G.hand.highlighted) or {}) do
      if c and c.base then
        picked[#picked + 1] = {
          r = tostring(c.base.value or ""),
          s = tostring(c.base.suit or ""),
          e = (c.ability and c.ability.name) or "",
        }
      end
    end
    _G._decidex_actual_action = (#picked > 0) and
      { type = kind, cards = picked } or nil
  end

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
        capture_actual("play")
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
        capture_actual("discard")
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
      -- A stale facing_blind ("select accepted, blind starting") wedges the
      -- select: its clearing event can crash (Event Guard drops it) and
      -- nothing re-fires, so the autopilot waits on "already selected"
      -- while the blind-select UI sits there for a minute (observed 45-60s
      -- at antes 5-6, always self-recovering late). If the blind-select UI
      -- is STILL up with facing_blind set for >10s, the start never
      -- happened - clear the stale flag and select again.
      if G.GAME and G.GAME.facing_blind and G.blind_select and not G.blind_select.REMOVED then
        if not _G._facing_blind_since then
          _G._facing_blind_since = now
        elseif now - _G._facing_blind_since > 10.0 then
          _G._facing_blind_since = nil
          _G._select_blind_in_flight = nil
          G.GAME.facing_blind = nil
          print("[DecideX Frame Guard] cleared stale facing_blind after 10s stuck in BLIND_SELECT")
        end
      else
        _G._facing_blind_since = nil
      end
      if (G.GAME and G.GAME.facing_blind) or _G._select_blind_in_flight then
        return true, "blind already selected (animating)"
      end
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
      local once_ok, once_err = decidex_once_refused(c, "booster buy")
      if not once_ok then return false, once_err end
      if G.FUNCS and G.FUNCS.use_card then
        -- Card:open() charges the cost itself via ease_dollars(-self.cost)
        -- (card.lua:1716); charging here as well would double-charge.
        decidex_once_mark(c)
        local btn = { config = { ref_table = c, button = 'use_card' } }
        local ok, err = pcall(G.FUNCS.use_card, btn)
        if ok then return true, "opened booster pack via use_card" end
        return false, "use_card failed for booster: " .. tostring(err)
      end
      if c.open then
        decidex_once_mark(c)
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
      local once_ok, once_err = decidex_once_refused(c, "voucher buy")
      if not once_ok then return false, once_err end
      local btn = { config = { ref_table = c, button = 'use_card' } }
      if G.FUNCS and G.FUNCS.use_card then
        decidex_once_mark(c)
        local ok, err = pcall(G.FUNCS.use_card, btn)
        if ok then return true, "redeemed voucher via use_card" end
      end
      if c.redeem then
        decidex_once_mark(c)
        local ok, err = pcall(function() c:redeem() end)
        if ok then return true, "redeemed voucher directly" end
      end
      return false, "failed to redeem voucher"
    end

    -- Standard card / joker / consumable purchase
    if G.FUNCS and G.FUNCS.buy_from_shop then
      local once_ok, once_err = decidex_once_refused(c, "buy")
      if not once_ok then return false, once_err end
      local btn = { config = { ref_table = c, id = 'buy_button' } }
      decidex_once_mark(c)
      local ok, res = pcall(G.FUNCS.buy_from_shop, btn)
      -- buy_from_shop returns false when check_for_buy_space() refuses the
      -- purchase (joker/consumable slots are full). pcall alone would treat
      -- that as success.
      if not ok then return false, "buy_from_shop error: " .. tostring(res) end
      if res == false then
        -- Graceful refusal: NOTHING was charged or emplaced — the client
        -- sells a joker and legitimately re-buys this card, so release the
        -- once-mark (an exception throw would have kept it: a mid-body
        -- crash may have queued partial effects).
        decidex_once_unmark(c)
        return false, "no space to buy " .. tostring(c.name or "card")
      end
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
    -- This is an ESCAPE hatch: the autopilot only calls it when the pack
    -- is a dead end. Rebuilding here used to "succeed" with a fresh EMPTY
    -- area (the card-creation events had already fired into the nil
    -- global), so the abandon step below was never reached and the JS
    -- retried force_leave every 20s against the same dead end - forever.
    -- Leave means leave.
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

  if action == "force_end_round" then
    -- Escape hatch for a round whose resolution chain broke: SELECTING_HAND
    -- with zero hands left never self-resolves (vanilla ends a round only
    -- through the final played hand or the all-areas-empty auto-end_round
    -- in Game:update_selecting_hand). Observed live: a dropped scoring
    -- event left the game waiting forever, hand stuffed to 21 cards, 160
    -- queued events. end_round() evaluates win/loss itself and runs the
    -- whole end-of-round chain (game over, cash out, ante up).
    if G.STATE ~= G.STATES.SELECTING_HAND then
      return false, "not in SELECTING_HAND"
    end
    local cr = (G.GAME and G.GAME.current_round) or {}
    local hands_left = tonumber(cr.hands_left) or 0
    local discards_left = tonumber(cr.discards_left) or 0
    if hands_left > 0 and discards_left > 0 then
      return false, "actions still available"
    end
    if G.hand and G.hand.unhighlight_all then
      pcall(function() G.hand:unhighlight_all() end)
    end
    local ok, err = pcall(end_round)
    if ok then return true, "forced end_round" end
    return false, "end_round failed: " .. tostring(err)
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

    -- v133: ONE use per card per pack episode. The picked card leaves the
    -- area via a QUEUED vanilla event; until it lands, the emitted snapshot
    -- is unchanged and a re-dispatch runs use_card a SECOND time — each run
    -- queues its own hand-level-up (production report: one planet pack
    -- leveled the same hand repeatedly). sort_id is object-unique, so this
    -- only ever refuses a card that was dispatched and is STILL present —
    -- exactly the double-apply case. Legit retries (joker slots freed,
    -- transient gates) never reach use_card, so they are never marked.
    -- The refusal reason contains "in progress" so the client classifies
    -- it transient (wait + strike) instead of a hard failure.
    if G.pack_cards ~= _G._decidex_used_area then
      _G._decidex_used_area = G.pack_cards
      _G._pack_dispatched = {}
    end
    if c.sort_id and _G._pack_dispatched and _G._pack_dispatched[c.sort_id] then
      return false, "duplicate use in progress (card already dispatched once this pack)"
    end

    -- Check if card is a Joker and whether slots are available
    if c.ability and c.ability.set == "Joker" and G.jokers and G.jokers.cards then
      local max_jokers = (G.jokers.config and G.jokers.config.card_limit) or 5
      if #G.jokers.cards >= max_jokers and not (c.edition and c.edition.negative) then
        return false, "cannot select joker: joker slots full"
      end
    end

    -- Pre-validate cards that consume the hand itself BEFORE calling vanilla
    -- use_card: vanilla removes the card from the pack area FIRST and only
    -- then hits `pseudorandom_element(G.hand.cards)` inside
    -- Familiar/Grim/Incantation ("interval is empty" when the hand is empty —
    -- e.g. every pack opened from the shop), so each failed attempt still
    -- eats one pack card. Sigil/Ouija convert the whole hand (no-op on empty;
    -- Ouija also shrinks hand size). A clean rejection here lets the client
    -- escalate to pack skip/leave instead of churning.
    local cname = (c.ability and c.ability.name) or ""
    local hand_needs_cards = ({ Familiar = true, Grim = true, Incantation = true,
      Immolate = true, Cryptid = true, Sigil = true, Ouija = true })[cname]
    if hand_needs_cards and (not (G.hand and G.hand.cards) or #G.hand.cards == 0) then
      return false, cname .. " needs a non-empty hand (destroy/convert target)"
    end

    -- Target selection for targeted tarots/spectrals inside booster pack
    if args.target_indices then
      prepare_and_highlight_targets(c, args.target_indices)
    end

    if G.FUNCS and G.FUNCS.use_card then
      -- Mark BEFORE the call: any dispatch attempt (success or mid-body
      -- crash) has already queued this card's effects; a repeat must be
      -- refused even if this very call errors.
      if c.sort_id then
        _G._pack_dispatched = _G._pack_dispatched or {}
        _G._pack_dispatched[c.sort_id] = true
      end
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
    -- v134: a used/sold card leaves its area via a queued event; a
    -- re-dispatch while it lingers would double-apply the effect (planet
    -- level-up, Hermit payout, sell refund).
    local once_ok, once_err = decidex_once_refused(c, action)
    if not once_ok then return false, once_err end
    if action == "use_card" and G.FUNCS and G.FUNCS.use_card then
      if args.target_indices then
        prepare_and_highlight_targets(c, args.target_indices)
      end
      decidex_once_mark(c)
      local ok, err = pcall(G.FUNCS.use_card, {config = {ref_table = c, button = 'use_card'}})
      return ok, tostring(err or "used card")
    elseif action == "sell_card" and G.FUNCS and G.FUNCS.sell_card then
      decidex_once_mark(c)
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

  -- v131c: factory markers (installed on the first tick after
  -- UI_definitions.lua defines the create_UIBox_*_pack globals).
  pcall(install_pack_factory_markers)

  -- A Moveable aligned to a dead major aborts G:update every frame.
  pcall(install_align_to_major_guard)

  -- A delayed blind-select build inside a pack rewrites blind_states and
  -- crashes its own prompt-box event (see install_blind_select_build_guard).
  pcall(install_blind_select_build_guard)

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

  -- Refill a rebuilt-but-empty pack area whose vanilla creation events are
  -- dead (must run before the auto-close so a recoverable pack gets its
  -- populate window instead of an instant abandon).
  pcall(populate_empty_pack)

  -- v131: trim duplicates a LATE vanilla emplace can leave behind after a
  -- bridge populate (our cards + vanilla's would exceed pack_size).
  pcall(trim_overfull_pack)

  -- Consumed-pack fast exit (see the comment on auto_close_consumed_pack).
  pcall(auto_close_consumed_pack)

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
