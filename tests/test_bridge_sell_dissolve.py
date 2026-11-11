"""v142 P1 runtime closure: sell_card's once-mark must survive the dissolve
window.

Executes the REAL bridge Lua (tools/balatro_bridge/decidex-bridge.lua)
under lupa with a minimal G stub, then drives DECIDEX_EXECUTE directly:

Vanilla facts being modeled (verified in source):
- use_card removes the card from its area SYNCHRONOUSLY (function body,
  button_callbacks.lua) -> a successful pcall leaves nothing to protect,
  unmark on success is correct.
- sell_card removal is ASYNC: refund now, start_dissolve queues
  self:remove() at +0.7s (card.lua:1592/2185). During the window the card
  still sits in its area; a re-dispatch inside it would double-refund.

Assertions:
1. sell dispatch #1 refunds exactly once.
2. An immediate re-dispatch inside the dissolve window is REFUSED
   (duplicate refusal) and does NOT refund again.
3. After the dissolve completes (card leaves the area) and the bridge
   maintenance sweep runs, a re-dispatch on the same index is allowed
   (fresh card in that slot -> refundable again).
4. use_card symmetry: a successful use unmarks immediately, so a legit
   re-use of the SAME index (new card object) is never refused, and the
   refused text for a real in-flight duplicate is present.
"""

import os
import pytest

lupa = pytest.importorskip("lupa")

BRIDGE = os.path.join(
    os.path.dirname(__file__), "..", "tools", "balatro_bridge", "decidex-bridge.lua"
)

PRELUDE = r"""
-- --- vanilla-behavior stubs -------------------------------------------
-- vanilla sell_card (card.lua:1592): refund fires NOW; the card object
-- stays in its area until the queued dissolve event removes it (+0.7s).
G_FUNCS = {}
G_FUNCS.sell_card = function(btn)
  local c = btn.config.ref_table
  c.refunded = (c.refunded or 0) + 1
  c.sold = true
  return true
end

-- vanilla use_card (button_callbacks.lua:2194): area removal is
-- SYNCHRONOUS inside the function body.
G_FUNCS.use_card = function(btn)
  local c = btn.config.ref_table
  c.uses = (c.uses or 0) + 1
  if c.area then c.area:remove_card(c) end
  return true
end

G = {
  FUNCS = G_FUNCS,
  STATES = { SHOP = 5, BLIND_SELECT = 3, MENU = 1, SPLASH = 0 },
  STATE = 5,
  GAME = { dollars = 100, bankrupt_at = -5 },
  hand = { highlighted = {} },
  SETTINGS = { GAMESPEED = 4, tutorial_complete = true },
}
_G.G = G

-- bridge tail hooks love.update as a fallback; provide a minimal love stub
love = { update = function(dt) end }
_G.love = love

-- area stub with vanilla-ish emplace/remove semantics
local function make_area(name)
  local a = { cards = {}, name = name }
  function a:remove_card(c)
    for i, v in ipairs(self.cards) do
      if v == c then table.remove(self.cards, i) break end
    end
    c.area = nil
  end
  function a:emplace(c)
    table.insert(self.cards, c)
    c.area = self
  end
  return a
end
G.make_area = make_area
G.jokers = make_area("jokers")
G.consumeables = make_area("consumeables")

-- card stub: bare table is enough; bridge keys once-marks on the object.
local function make_card(name)
  return { ability = { name = name, set = "Joker" } }
end
G.make_card = make_card

-- test helper: complete a pending dissolve (pull the card out of its
-- area), mirroring the vanilla queued self:remove() at +0.7s.
G.finish_dissolves = function()
  for _, area_name in ipairs({ "jokers", "consumeables" }) do
    local a = G[area_name]
    for i = #a.cards, 1, -1 do
      local c = a.cards[i]
      if c.sold and not c.dissolved then
        c.dissolved = true
        a:remove_card(c)
      end
    end
  end
end
"""


@pytest.fixture(scope="module")
def lua():
    L = lupa.LuaRuntime(unpack_returned_tuples=True)
    L.execute(PRELUDE)
    src = open(BRIDGE, "r", encoding="utf-8").read()
    chunk = L.eval('function(src) return load(src, "bridge") end')(src)
    # unpack_returned_tuples=True collapses the load() result into the
    # callable itself; calling it returns the chunk's return value (true)
    # or raises LuaError on failure.
    try:
        chunk()
    except lupa.LuaError as e:  # pragma: no cover - failure path
        pytest.fail(f"bridge failed to load: {e}")
    assert L.eval("type(_G.DECIDEX_EXECUTE)") == "function"
    return L


def _dispatch(lua, action, area, index):
    # NOTE: build the args table on the Lua side — a Python dict proxy
    # raises KeyError when Lua indexes a missing field (args.target_indices
    # is probed with a plain `if args.target_indices`), while a Lua table
    # simply yields nil.
    fn = lua.eval(
        "function(a, area, idx)"
        " local t = { index = idx, area = area }"
        " local ok, info = _G.DECIDEX_EXECUTE(a, t)"
        " return { ok = ok, info = tostring(info or '') } end"
    )
    res = fn(action, area, index)
    return bool(res["ok"]), str(res["info"])


def _one_tick(lua):
    # Run only the once-sweep part of maintenance by invoking DECIDEX_STEP
    # would need FS stubs; the sweep is what we need, so call it through
    # the same pcall-wrapped site the bridge uses: simulate by directly
    # running the bridge's step in a tolerant way is overkill — instead
    # exercise the exact sweep semantics via DECIDEX_STEP's maintenance
    # precondition: the bridge guards io.open; stub io.open to fail.
    lua.execute("""
    if not _G._io_open_stubbed then
      _G._io_open_stubbed = true
      local real_open = io.open
      io.open = function(path, mode)
        if tostring(path):find("balatro_eval") then return nil end
        return real_open(path, mode)
      end
    end
    """)
    step = lua.eval("function(dt) return _G.DECIDEX_STEP(dt) end")
    step(0.016)


class TestSellDissolveWindow:
    def test_sell_refunds_once_and_rejects_inside_window(self, lua):
        lua.execute("G.jokers = G.make_area('jokers_t1')")
        lua.execute("G.jokers:emplace(G.make_card('Banner'))")
        ok, msg = _dispatch(lua, "sell_card", "jokers", 1)
        assert ok is True
        assert lua.eval("G.jokers.cards[1].refunded") == 1
        assert lua.eval("G.jokers.cards[1].sold") is True
        # dissolve pending: card still in area, mark still live
        assert lua.eval("#G.jokers.cards") == 1

        # re-dispatch inside the window: must be refused, no second refund
        ok2, msg2 = _dispatch(lua, "sell_card", "jokers", 1)
        assert ok2 is False
        assert "duplicate" in str(msg2) and "in progress" in str(msg2)
        assert lua.eval("G.jokers.cards[1].refunded") == 1

    def test_sweep_releases_mark_after_dissolve_then_fresh_sell(self, lua):
        # complete the dissolve: card leaves the area
        lua.execute("G.finish_dissolves()")
        assert lua.eval("#G.jokers.cards") == 0
        # bridge maintenance tick: sweep releases marks of area-less cards
        _one_tick(lua)
        # fresh card slides into the now-free slot
        lua.execute("G.jokers:emplace(G.make_card('Cavendish'))")
        ok, msg = _dispatch(lua, "sell_card", "jokers", 1)
        assert ok is True, msg
        assert lua.eval("G.jokers.cards[1].refunded") == 1

    def test_use_card_unmarks_on_success_same_index_reusable(self, lua):
        lua.execute("G.consumeables:emplace(G.make_card('Mercury'))")
        ok, msg = _dispatch(lua, "use_card", "consumeables", 1)
        assert ok is True, msg
        # synchronous removal already happened in the stub body
        assert lua.eval("#G.consumeables.cards") == 0
        assert lua.eval("G.consumeables and true or false") is True
        # mark was unmarked on success: a NEW card in the same index can be
        # used immediately (v134's 65-refusals/5min regression guard)
        lua.execute("G.consumeables:emplace(G.make_card('Saturn'))")
        ok2, msg2 = _dispatch(lua, "use_card", "consumeables", 1)
        assert ok2 is True, msg2
        assert lua.eval("G.consumeables.cards[1] and 1 or 0") == 0

    def test_use_card_duplicate_in_flight_refused(self, lua):
        # a use_card whose stubbed body does NOT remove the card (crash
        # before removal) keeps the mark: re-dispatch must be refused.
        lua.execute("""
        G.consumeables = G.make_area('consumeables2')
        local c = G.make_card('Hermit')
        G.consumeables:emplace(c)
        c.area = G.consumeables
        -- simulate dispatch where the body crashed BEFORE removal:
        c.refunded = nil
        """)
        # mark manually (as a failed dispatch would leave it)
        lua.execute("""
        _G._decidex_once_ids = _G._decidex_once_ids or {}
        _G._decidex_once_area = _G._decidex_once_area or {}
        local c = G.consumeables.cards[1]
        if not _G._decidex_once_ids[c] then
          _G._decidex_once_ids[c] = true
          _G._decidex_once_area[c] = c.area
        end
        """)
        ok, msg = _dispatch(lua, "use_card", "consumeables2", 1)
        assert ok is False
        assert "duplicate" in str(msg)
