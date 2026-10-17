--[[
  Pure Lua implementation of the LuaJIT "bit" library for PUC-Rio Lua 5.1 (WebAssembly / love.js)
  Supports: tobit, tohex, bnot, band, bor, bxor, lshift, rshift, arshift, rol, ror, bswap
]]

local bit = {}

local MOD = 4294967296
local HALF_MOD = 2147483648

function bit.tobit(x)
  x = x % MOD
  if x >= HALF_MOD then
    return x - MOD
  end
  return x
end

function bit.tohex(x, n)
  n = n or 8
  local hex = "0123456789abcdef"
  x = x % MOD
  local s = {}
  for i = 1, math.abs(n) do
    local digit = (x % 16) + 1
    table.insert(s, 1, hex:sub(digit, digit))
    x = math.floor(x / 16)
  end
  return table.concat(s)
end

function bit.bnot(x)
  return bit.tobit(MOD - 1 - (x % MOD))
end

local function bit_op(a, b, op)
  a = a % MOD
  b = b % MOD
  local res = 0
  local bitval = 1
  for i = 1, 32 do
    local a_bit = a % 2
    local b_bit = b % 2
    if op == "and" then
      if a_bit == 1 and b_bit == 1 then res = res + bitval end
    elseif op == "or" then
      if a_bit == 1 or b_bit == 1 then res = res + bitval end
    elseif op == "xor" then
      if a_bit ~= b_bit then res = res + bitval end
    end
    a = math.floor(a / 2)
    b = math.floor(b / 2)
    bitval = bitval * 2
  end
  return bit.tobit(res)
end

function bit.band(a, ...)
  local res = a
  local args = { ... }
  for i = 1, #args do
    res = bit_op(res, args[i], "and")
  end
  return res
end

function bit.bor(a, ...)
  local res = a
  local args = { ... }
  for i = 1, #args do
    res = bit_op(res, args[i], "or")
  end
  return res
end

function bit.bxor(a, ...)
  local res = a
  local args = { ... }
  for i = 1, #args do
    res = bit_op(res, args[i], "xor")
  end
  return res
end

function bit.lshift(x, n)
  n = n % 32
  if n == 0 then return bit.tobit(x) end
  return bit.tobit((x % MOD) * (2 ^ n))
end

function bit.rshift(x, n)
  n = n % 32
  if n == 0 then return bit.tobit(x) end
  return bit.tobit(math.floor((x % MOD) / (2 ^ n)))
end

function bit.arshift(x, n)
  n = n % 32
  x = bit.tobit(x)
  if n == 0 then return x end
  if x >= 0 then
    return bit.tobit(math.floor(x / (2 ^ n)))
  else
    return bit.tobit(math.floor((x + MOD) / (2 ^ n)) - (2 ^ (32 - n)))
  end
end

function bit.rol(x, n)
  n = n % 32
  return bit.bor(bit.lshift(x, n), bit.rshift(x, 32 - n))
end

function bit.ror(x, n)
  n = n % 32
  return bit.bor(bit.rshift(x, n), bit.lshift(x, 32 - n))
end

function bit.bswap(x)
  local a = bit.band(x, 0xff)
  local b = bit.band(bit.rshift(x, 8), 0xff)
  local c = bit.band(bit.rshift(x, 16), 0xff)
  local d = bit.band(bit.rshift(x, 24), 0xff)
  return bit.bor(bit.lshift(a, 24), bit.lshift(b, 16), bit.lshift(c, 8), d)
end

-- Export globally and to package.loaded
_G.bit = bit
_G.bit32 = bit
package.loaded["bit"] = bit
package.loaded["bit32"] = bit

return bit
