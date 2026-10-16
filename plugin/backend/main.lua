-- Millennium Lua backend for discord-ish-steam.
--
-- Sole job: make sure the screen-share daemon (rp-webrtc.py) is running whenever Steam is.
-- Before this, the daemon's lifetime was owned by something OUTSIDE Steam - a systemd --user
-- service on Linux, a logon scheduled task on Windows - and when that didn't take (the most
-- common case being install.ps1 failing to resolve a real python.exe, which silently skipped
-- BOTH the task and the Startup shortcut) the user was left starting the script by hand
-- forever. The plugin already runs exactly when sharing is possible, so it should own this.
--
-- Millennium v3 dropped Python backends: a plugin whose backendType isn't "lua" is flagged
-- legacy and DISABLED, so this is deliberately Lua. main.lua must return a table.
--
-- Spawning is unconditional and idempotent - rp-webrtc.py self-guards on its control port
-- (see _already_running there), so a duplicate exits 0 quietly. That means this can't fight
-- with the systemd service or the logon task if either is also doing its job.

local WIN = package.config:sub(1, 1) == "\\"
local started = false

local function log(msg)
  local line = "[ds-backend] " .. tostring(msg)
  print(line)
  local tmp = os.getenv("TEMP") or os.getenv("TMPDIR") or "/tmp"
  local f = io.open(tmp .. (WIN and "\\" or "/") .. "ds-lua-backend.log", "a")
  if f then f:write(os.date("%Y-%m-%d %H:%M:%S ") .. line .. "\n"); f:close() end
end

local function exists(p)
  if not p then return false end
  local f = io.open(p, "r")
  if f then f:close() return true end
  return false
end

-- Where is rp-webrtc.py? Both installers default to <home>/discord-ish-steam, but the repo
-- can be cloned anywhere, so try the explicit override and our own on-disk location first.
-- NOTE: on Linux the plugin dir is a SYMLINK into the repo, so deriving the path from
-- main.lua's own location may resolve to the Millennium plugins dir instead - hence the
-- home-dir candidates as a backstop rather than relying on it.
local function daemon_path()
  local sep = WIN and "\\" or "/"
  local cands = {}

  local override = os.getenv("DS_REPO")
  if override then cands[#cands + 1] = override .. sep .. "rp-webrtc.py" end

  local src = debug.getinfo(1, "S").source
  if src and src:sub(1, 1) == "@" then
    local dir = src:sub(2):match("^(.*)[/\\][^/\\]-$")        -- .../plugin/backend
    if dir then
      local up2 = dir:match("^(.*)[/\\][^/\\]-[/\\][^/\\]-$")  -- repo root
      if up2 then cands[#cands + 1] = up2 .. sep .. "rp-webrtc.py" end
    end
  end

  local home = os.getenv("USERPROFILE") or os.getenv("HOME")
  if home then cands[#cands + 1] = home .. sep .. "discord-ish-steam" .. sep .. "rp-webrtc.py" end

  for _, c in ipairs(cands) do
    if exists(c) then return c end
  end
  return nil, cands
end

-- Prefer pythonw.exe on Windows so no console window flashes up at every Steam launch.
local function python_exe()
  if not WIN then return "python3" end
  for _, name in ipairs({ "pythonw.exe", "python.exe" }) do
    local p = io.popen('where ' .. name .. ' 2>NUL')
    if p then
      local out = p:read("*l")
      p:close()
      if out and out ~= "" and exists(out) then return '"' .. out .. '"' end
    end
  end
  return "pythonw"                                  -- last resort: hope it's on PATH
end

local function ensure()
  if started then return end
  if not os.execute then log("os.execute unavailable; cannot start the daemon"); return end

  local script, tried = daemon_path()
  if not script then
    log("rp-webrtc.py not found; looked in: " .. table.concat(tried or {}, ", "))
    return
  end

  local py = python_exe()
  local cmd
  if WIN then
    -- `start "" /B` detaches so os.execute returns immediately and the daemon outlives us
    cmd = 'start "" /B ' .. py .. ' "' .. script .. '"'
  else
    -- setsid so it survives the webhelper; & so os.execute doesn't block Steam's startup
    cmd = 'setsid ' .. py .. ' "' .. script .. '" >/dev/null 2>&1 & '
  end

  log("ensuring daemon: " .. cmd)
  local ok = os.execute(cmd)
  if ok == false then
    log("spawn returned failure; the daemon may already be running (that is fine)")
  end
  started = true
end

-- Millennium 3 holds back Steam's whole UI (steamwebhelper is not even launched) until EVERY
-- Lua backend reports ready() — with no timeout. Forgetting it means Steam never opens; found
-- in the Windows test VM. So signal readiness unconditionally, whatever ensure() did.
local function signal_ready()
  local ok, m = pcall(require, "millennium")
  if ok and type(m) == "table" and type(m.ready) == "function" then
    local ok2, err = pcall(m.ready)
    if not ok2 then log("ready() failed: " .. tostring(err)) end
  else
    log("millennium module unavailable; cannot signal ready (" .. tostring(m) .. ")")
  end
end

return {
  -- Never let a failure here surface as a plugin crash: Millennium's crash handler offers to
  -- DISABLE the plugin, which would take the whole reskin down over an optional convenience.
  on_load = function()
    local ok, err = pcall(ensure)
    if not ok then log("on_load error: " .. tostring(err)) end
    signal_ready()
  end,

  -- Second chance, in case on_load ran before the user session was ready. ensure() is a
  -- no-op once it has spawned, and the daemon's own port guard makes a repeat harmless.
  on_frontend_loaded = function()
    local ok, err = pcall(ensure)
    if not ok then log("on_frontend_loaded error: " .. tostring(err)) end
  end,

  -- Deliberately does NOT kill the daemon: on Linux systemd may own it, and on Windows the
  -- logon task may have started it. We only ever add a way for it to be up, never take one away.
  on_unload = function()
    log("plugin unloading; leaving the daemon running")
  end,
}
