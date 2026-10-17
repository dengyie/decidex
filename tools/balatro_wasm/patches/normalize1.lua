--[[
This file is part of "love.js" by 2dengine.
https://2dengine.com/doc/lovejs.html
]]

love.js = {}
local cache = {}
function love.js.eval(cmd)
  love.system = love.system or require('love.system')
  love.system.openURL('javascript:'..cmd)
  
  for i = 1, 2^32 do
    local line = io.read()
    if not line then
      break
    end
    cache[i] = line
  end
  local output = table.concat(cache, '\n')

  for i = #cache, 1, -1 do
    cache[i] = nil
  end

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
