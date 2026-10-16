"""Tests for plugin/backend/main.lua, run through LuaJIT via ctypes (the same VM family
Millennium uses; there's no standalone lua binary needed). Skips if libluajit is absent.

    python3 -m unittest discover -s tests -v
"""
import ctypes
import ctypes.util
import os
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MAIN_LUA = os.path.join(ROOT, "plugin", "backend", "main.lua")
LUA_GLOBALSINDEX = -10002


def _load_luajit():
    for name in (ctypes.util.find_library("luajit-5.1"), "libluajit-5.1.so.2",
                 "/usr/lib/x86_64-linux-gnu/libluajit-5.1.so.2"):
        if not name:
            continue
        try:
            return ctypes.CDLL(name)
        except OSError:
            pass
    return None


LJ = _load_luajit()
if LJ:
    # lua_State* must stay a full pointer — ctypes' default int return truncates it
    P, I, S = ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p
    for fn, res, args in (("luaL_newstate", P, []), ("luaL_openlibs", None, [P]),
                          ("luaL_loadstring", I, [P, S]), ("lua_pcall", I, [P, I, I, I]),
                          ("lua_tolstring", S, [P, I, P]), ("lua_getfield", None, [P, I, S]),
                          ("lua_settop", None, [P, I]), ("lua_close", None, [P])):
        getattr(LJ, fn).restype = res
        getattr(LJ, fn).argtypes = args

# Stubs os.execute, loads main.lua the way Millennium does (run the chunk, expect a table),
# then fires the hooks. Results come back through globals.
DRIVER = r"""
READY = 0
package.preload["millennium"] = function() return { ready = function() READY = READY + 1 end } end
local calls = {}
os.execute = function(c) calls[#calls + 1] = c; return 0 end
local chunk = assert(loadfile(MAIN_LUA))
local mod = chunk()
assert(type(mod) == "table", "main.lua must return a table")
for _, k in ipairs({ "on_load", "on_frontend_loaded", "on_unload" }) do
  assert(type(mod[k]) == "function", "missing hook " .. k)
end
mod.on_load()
mod.on_frontend_loaded()
mod.on_unload()
CALLS = #calls
CMD = calls[1] or ""
"""


@unittest.skipUnless(LJ, "libluajit-5.1 not installed")
class LuaBackend(unittest.TestCase):
    def run_lua(self, code, env=None):
        L = LJ.luaL_newstate()
        LJ.luaL_openlibs(L)
        old = {k: os.environ.get(k) for k in (env or {})}
        os.environ.update(env or {})
        try:
            src = ("MAIN_LUA = %r\n" % MAIN_LUA) + code
            rc = LJ.luaL_loadstring(L, src.encode()) or LJ.lua_pcall(L, 0, 0, 0)
            if rc:
                self.fail("lua error: %s" % LJ.lua_tolstring(L, -1, None).decode())
            out = {}
            for g in ("CALLS", "CMD", "READY"):
                LJ.lua_getfield(L, LUA_GLOBALSINDEX, g.encode())
                v = LJ.lua_tolstring(L, -1, None)
                out[g] = v.decode() if v is not None else None
                LJ.lua_settop(L, -2)
            return out
        finally:
            LJ.lua_close(L)
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_hooks_spawn_daemon_exactly_once_detached(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self.run_lua(DRIVER, {"TMPDIR": tmp})
            self.assertTrue(os.path.exists(os.path.join(tmp, "ds-lua-backend.log")))
        self.assertEqual(out["CALLS"], "1")          # on_frontend_loaded must not respawn
        # without ready() Millennium never launches Steam's UI (no timeout)
        self.assertEqual(out["READY"], "1")
        cmd = out["CMD"]
        self.assertIn(os.path.join(ROOT, "rp-webrtc.py"), cmd)
        self.assertTrue(cmd.startswith("setsid "), cmd)
        self.assertTrue(cmd.rstrip().endswith("&"), "must not block Steam's startup: " + cmd)

    def test_ds_repo_override_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            open(os.path.join(tmp, "rp-webrtc.py"), "w").close()
            out = self.run_lua(DRIVER, {"DS_REPO": tmp, "TMPDIR": tmp})
        self.assertIn(os.path.join(tmp, "rp-webrtc.py"), out["CMD"])

    def test_missing_daemon_does_not_crash_plugin(self):
        # Point every candidate somewhere empty: the hooks must swallow it (a raised error
        # would make Millennium offer to DISABLE the plugin = the whole reskin).
        with tempfile.TemporaryDirectory() as tmp:
            code = DRIVER.replace("local chunk = assert(loadfile(MAIN_LUA))",
                                  "debug.getinfo = function() return { source = '=?' } end\n"
                                  "local chunk = assert(loadfile(MAIN_LUA))")
            out = self.run_lua(code, {"HOME": tmp, "DS_REPO": tmp, "TMPDIR": tmp})
        self.assertEqual(out["CALLS"], "0")
        self.assertEqual(out["READY"], "1")          # ...and Steam must still open


if __name__ == "__main__":
    unittest.main()
