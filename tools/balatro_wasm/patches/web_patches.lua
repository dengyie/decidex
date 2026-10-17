--[[
  WebAssembly / WebGL compatibility patches for Balatro on love.js (LÖVE 11.5)
]]

-- 1. Ensure bit library is available
require "bit"

-- 2. Spoof OS as Web so Balatro skips Steamworks / native shared libraries
local orig_getOS = love.system and love.system.getOS
if love.system then
  love.system.getOS = function()
    return "Web"
  end
end

-- Ensure love.event.quit does not terminate the browser engine
if love.event then
  love.event.quit = function(...)
    print("[DecideX Web] love.event.quit intercepted and safely ignored in browser.")
  end
end

-- In browser WebGL, requestAnimationFrame controls framerate; busy sleep burns CPU
if love.timer then
  love.timer.sleep = function() end
end

-- 3. Two-argument math.log polyfill for Lua 5.1 (math.log(x, base))
local orig_log = math.log
math.log = function(x, base)
  if base and base ~= math.exp(1) then
    return orig_log(x) / orig_log(base)
  end
  return orig_log(x)
end

-- 4. math.randomseed float-compatibility
local orig_randomseed = math.randomseed
math.randomseed = function(seed)
  if type(seed) == "number" then
    local int_part = math.floor(seed)
    local frac_part = math.floor((seed - int_part) * 2147483647)
    orig_randomseed(int_part + frac_part)
  else
    orig_randomseed(seed)
  end
end

-- 5. WebGL NPOT Mipmap safety: disable mipmaps on newImage
if love.graphics and love.graphics.newImage then
  local orig_newImage = love.graphics.newImage
  love.graphics.newImage = function(filename, flags)
    flags = flags or {}
    if type(flags) == "table" then
      flags.mipmaps = false
    end
    local ok, img = pcall(orig_newImage, filename, flags)
    if not ok then
      -- Fallback retry without flags
      img = orig_newImage(filename)
    end
    if img and type(img) == "userdata" then
      local meta = getmetatable(img)
      if meta and meta.__index then
        meta.__index.setMipmapFilter = function() end
      end
    end
    return img
  end
end

-- 6. Synchronous & Coroutine-safe Channel / Thread emulation for browser
local channels = {}

local function handle_save_request(request)
  if not request or type(request) ~= "table" then return end
  local ok, err = pcall(function()
    if request.type == 'save_progress' then
      local prefix_profile = (request.save_progress.SETTINGS.profile or 1)..''
      if not love.filesystem.getInfo(prefix_profile) then love.filesystem.createDirectory( prefix_profile ) end
      prefix_profile = prefix_profile..'/'

      if not love.filesystem.getInfo(prefix_profile..'meta.jkr') then
        love.filesystem.append( prefix_profile..'meta.jkr', 'return {}' )
      end

      local meta = STR_UNPACK(get_compressed(prefix_profile..'meta.jkr') or 'return {}')
      meta.unlocked = meta.unlocked or {}
      meta.discovered = meta.discovered or {}
      meta.alerted = meta.alerted or {}

      local _append = false
      for k, v in pairs(request.save_progress.UDA or {}) do
        if string.find(v, 'u') and not meta.unlocked[k] then 
          meta.unlocked[k] = true
          _append = true
        end
        if string.find(v, 'd') and not meta.discovered[k] then 
          meta.discovered[k] = true
          _append = true
        end
        if string.find(v, 'a') and not meta.alerted[k] then 
          meta.alerted[k] = true
          _append = true
        end
      end
      if _append then compress_and_save( prefix_profile..'meta.jkr', STR_PACK(meta)) end

      compress_and_save('settings.jkr', request.save_progress.SETTINGS)
      compress_and_save(prefix_profile..'profile.jkr', request.save_progress.PROFILE)

      if channels["save_request"] then
        table.insert(channels["save_request"].items, 'done')
      end
    elseif request.type == 'save_settings' then 
      compress_and_save('settings.jkr', request.save_settings)
      compress_and_save(request.profile_num..'/profile.jkr', request.save_profile)
    elseif request.type == 'save_metrics' then 
      compress_and_save('metrics.jkr', request.save_metrics)
    elseif request.type == 'save_notify' then 
      local prefix_profile = (request.profile_num or 1)..''
      if not love.filesystem.getInfo(prefix_profile) then love.filesystem.createDirectory( prefix_profile ) end
      prefix_profile = prefix_profile..'/'

      if not love.filesystem.getInfo(prefix_profile..'unlock_notify.jkr') then love.filesystem.append( prefix_profile..'unlock_notify.jkr', '') end
      local unlock_notify = get_compressed(prefix_profile..'unlock_notify.jkr') or ''

      if request.save_notify and not string.find(unlock_notify, request.save_notify) then 
        compress_and_save( prefix_profile..'unlock_notify.jkr', unlock_notify..request.save_notify..'\n')
      end
    elseif request.type == 'save_run' then 
      local prefix_profile = (request.profile_num or 1)..''
      if not love.filesystem.getInfo(prefix_profile) then love.filesystem.createDirectory( prefix_profile ) end
      prefix_profile = prefix_profile..'/'

      compress_and_save(prefix_profile..'save.jkr', request.save_table)
    end
  end)
  if not ok then
    print("[Web Save Error]: " .. tostring(err))
  end
end

local FakeChannel = {}
FakeChannel.__index = FakeChannel

function FakeChannel:push(val)
  if self.name == "save_request" and type(val) == "table" and val.type then
    handle_save_request(val)
    return true
  end
  table.insert(self.items, val)
  return true
end

function FakeChannel:supply(val)
  return self:push(val)
end

function FakeChannel:pop()
  if #self.items > 0 then
    return table.remove(self.items, 1)
  end
  return nil
end

function FakeChannel:demand()
  return self:pop()
end

function FakeChannel:peek()
  return self.items[1]
end

function FakeChannel:getCount()
  return #self.items
end

function FakeChannel:clear()
  self.items = {}
end

local FakeThread = {}
FakeThread.__index = FakeThread

function FakeThread:start(...)
  -- In single-threaded browser WASM, background threads are stubbed or handled via synchronous dispatch
end

function FakeThread:wait()
  return true
end

function FakeThread:isRunning()
  return false
end

function FakeThread:getError()
  return nil
end

if love.thread then
  love.thread.getChannel = function(name)
    if not channels[name] then
      channels[name] = setmetatable({ name = name, items = {} }, FakeChannel)
    end
    return channels[name]
  end

  love.thread.newThread = function(arg)
    return setmetatable({ name = tostring(arg) }, FakeThread)
  end
end

print("[DecideX Web] web_patches.lua loaded successfully")
return true
