--[[
This file is part of "love.js" by 2dengine.
https://2dengine.com/doc/lovejs.html
]]

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
      if not ok or not busy then
        playing[s] = nil
      end
    end
  end
  local _love_audio_play = love.audio.play
  function love.audio.play(...)
    for i = 1, select("#", ...) do
      local source = select(i, ...)
      if source ~= nil then
        playing[source] = true
      end
    end
    return _love_audio_play(...)
  end

  local _love_audio_stop = love.audio.stop
  function love.audio.stop(source, ...)
    if source then
      return _love_audio_stop(source, ...)
    end
    _cleanup_playing()
    for s in pairs(playing) do
      s:stop()
      playing[s] = nil
    end
  end

  local reg = debug.getregistry()
  if reg and reg.Source then
    local _Source_play = reg.Source.play
    reg.Source.play = function(source, ...)
      local res = _Source_play(source, ...)
      if res then
        playing[source] = true
      end
      return res
    end
  end
end

function love.system.getClipboardText()
  return love.js.eval([[
(function() {
  var clipboard = navigator.clipboard;
  if (!clipboard) return '';
  if (clipboard.text !== undefined) return clipboard.text;
  return '';
})();
]])
end

function love.system.setClipboardText(text)
  local cmd = [[
(function() {
  var clipboard = navigator.clipboard;
  if (!clipboard) return;
  clipboard.text = %q;
  navigator.clipboard.writeText(clipboard.text).catch(function () {});
})();
]]
  cmd = string.format(cmd, text)
  return love.js.eval(cmd)
end
