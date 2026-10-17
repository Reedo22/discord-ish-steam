// Tests for the Millennium frontend (plugin/.millennium/Dist/index.js).
//   node --test tests/
// The plugin is a hand-written IIFE for Steam's CEF, so it's loaded the same way its own
// self-updater loads a newer copy (strip the trailing ES-module export, eval) into a vm
// context with just enough of window/navigator/fetch stubbed to boot.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const SRC = readFileSync(new URL("../plugin/.millennium/Dist/index.js", import.meta.url), "utf8");

function boot({ fetchImpl } = {}) {
  const store = new Map();
  const intervals = [];
  const win = {
    navigator: { platform: "Linux x86_64", userAgent: "test", mediaDevices: null },
    localStorage: {
      getItem: (k) => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => store.set(k, String(v)),
    },
    fetch: fetchImpl || (() => Promise.reject(new Error("offline"))),
    setInterval: (f, ms) => { intervals.push(ms); return intervals.length; },
    clearInterval: () => {},
    setTimeout: () => 0, clearTimeout: () => {},
    console: { log() {}, warn() {} },
    btoa: (s) => Buffer.from(s, "binary").toString("base64"),
    atob: (s) => Buffer.from(s, "base64").toString("binary"),
    URL, Date, Math, JSON, Promise, Object, Array, String, Number, Error,
  };
  win.window = win;
  const ctx = vm.createContext(win);
  vm.runInContext(SRC.replace(/^export\s+default[\s\S]*$/m, ""), ctx);
  return { win, store, intervals };
}

test("plugin boots in a bare context without throwing and starts one tick loop", () => {
  const { win, intervals } = boot();
  assert.equal(win.__DISCORDISH_LOADED__, 1);
  assert.equal(win.__DISCORDISH_BOOTED__, true);
  assert.ok(intervals.includes(150), "tick interval registered");
  assert.ok(win.__ds_test, "test hooks exposed");
});

test("self-update VERSION marker is findable by the updater's regex", () => {
  const m = SRC.match(/var VERSION = (\d+)/);
  assert.ok(m && +m[1] >= 64);
});

test("signal payload round-trips url, ICE, ws and audio", () => {
  const { win } = boot();
  const { sigPayload, parseSignal } = win.__ds_test;
  const ice = [{ urls: ["turn:a:3478"], username: "u|x", credential: "p" }];
  const ws = "wss://abc-def.trycloudflare.com/screen/ws?x=1|2";
  const p = parseSignal(sigPayload("https://abc-def.trycloudflare.com/screen/whep", ice, ws, true));
  assert.equal(p.url, "https://abc-def.trycloudflare.com/screen/whep");
  assert.deepEqual(JSON.parse(JSON.stringify(p.ice)), ice);
  assert.equal(p.ws, ws);
  assert.equal(p.audio, true);
  const bare = parseSignal(sigPayload("http://1.2.3.4:48890/screen/whep", null, null, false));
  assert.equal(bare.ice, null);
  assert.equal(bare.ws, null);
  assert.equal(bare.audio, false);
});

test("garbage signal fields don't throw", () => {
  const { win } = boot();
  const p = win.__ds_test.parseSignal("https://h/screen/whep?k=1|ice=!!notbase64|ws=%E0%A4%A|aud=7|zzz");
  assert.equal(p.url, "https://h/screen/whep?k=1");
  assert.equal(p.ice, null);
  assert.equal(p.audio, false);
});

test("stale signals from a previous session are ignored, fresh ones honoured", () => {
  const { win } = boot();
  const fresh = win.__ds_test.signalIsFresh;
  const now = 1_800_000_000;
  // first poll after Steam starts (seen === undefined)
  assert.equal(fresh({ unOrdinal: 5, rtTimestamp: now - 3 * 86400 }, undefined, now), false);
  assert.equal(fresh({ unOrdinal: 5, rtTimestamp: now - 60 }, undefined, now), true);
  assert.equal(fresh({ unOrdinal: 5 }, undefined, now), true);       // no timestamp: old behaviour
  // later polls: purely by ordinal
  assert.equal(fresh({ unOrdinal: 6, rtTimestamp: now - 3 * 86400 }, 5, now), true);
  assert.equal(fresh({ unOrdinal: 5 }, 5, now), false);
});

test("saved share options restore fps and audio too", () => {
  const store = new Map([["ds_share_opts", JSON.stringify({ h: "720", br: "1M", fps: "60", audio: "off" })]]);
  const win = {
    navigator: { platform: "Linux", userAgent: "t", mediaDevices: null },
    localStorage: { getItem: (k) => store.get(k) ?? null, setItem: () => {} },
    fetch: () => Promise.reject(new Error("offline")),
    setInterval: () => 0, clearInterval: () => {}, setTimeout: () => 0, clearTimeout: () => {},
    console: { log() {}, warn() {} }, JSON, Promise, Object, Array, String, Number, Math, Date, URL, Error,
  };
  win.window = win;
  vm.runInContext(SRC.replace(/^export\s+default[\s\S]*$/m, ""), vm.createContext(win));
  assert.deepEqual({ ...win.__ds_share_opts }, { h: "720", br: "1M", fps: "60", audio: "off", codec: "auto" });
});

test("a newer remote copy replaces the bundled one exactly once", async () => {
  const newer = SRC.replace(/var VERSION = \d+/, "var VERSION = 9999");
  const { win } = boot({ fetchImpl: () => Promise.resolve({ ok: true, text: () => Promise.resolve(newer) }) });
  await new Promise((r) => setImmediate(r));
  await new Promise((r) => setImmediate(r));
  assert.equal(win.__ds_VERSION, 9999);
  assert.equal(win.__DISCORDISH_LOADED__, 2);
});

test("codec is signaled for AV1 and defaults to H.264 for older hosts", () => {
  const { win } = boot();
  const { sigPayload, parseSignal } = win.__ds_test;
  assert.equal(parseSignal(sigPayload("https://h/screen/whep?k=1", null, null, true, "av1")).codec, "av1");
  assert.equal(parseSignal(sigPayload("https://h/screen/whep?k=1", null, null, true, "h264")).codec, "h264");
  assert.equal(parseSignal("https://h/screen/whep|aud=1").codec, "h264");          // pre-v65 host
  assert.equal(parseSignal("https://h/screen/whep|cdc=evil").codec, "h264");       // unknown -> safe default
  assert.ok(!sigPayload("u", null, null, false, "h264").includes("cdc="));         // old viewers unaffected
});

test("LAN fallback endpoint is signaled and parsed", () => {
  const { win } = boot();
  const { sigPayload, parseSignal } = win.__ds_test;
  const lan = "http://192.168.1.50:48890/screen/whep?k=a|b";
  const p = parseSignal(sigPayload("https://play.example/screen/whep?k=a", null, null, false, "av1", lan));
  assert.equal(p.lan, lan);
  assert.equal(parseSignal(sigPayload("u", null, null, false, "av1", "u")).lan, null);   // not duplicated
});

test("audio codec is signaled so WebRTC viewers request the Opus track", () => {
  const { win } = boot();
  const { sigPayload, parseSignal } = win.__ds_test;
  assert.equal(parseSignal(sigPayload("u", null, null, true, "av1", null, "opus")).acodec, "opus");
  assert.equal(parseSignal(sigPayload("u", null, null, true, "av1", null, "aac")).acodec, "aac");
  assert.ok(!sigPayload("u", null, null, false, "av1", null, "opus").includes("ac="));  // no audio -> nothing
});

test("signals from a friend can only point at share endpoints", () => {
  const { win } = boot();
  const { parseSignal, sigPayload } = win.__ds_test;
  const ok = (u) => parseSignal(sigPayload(u, null, null, false)).url;
  // accepted: https anywhere, plain http only to LAN/private addresses
  assert.equal(ok("https://share.example.com/screen/whep?k=abc"), "https://share.example.com/screen/whep?k=abc");
  assert.equal(ok("https://a-b.trycloudflare.com/screen/whep?k=x"), "https://a-b.trycloudflare.com/screen/whep?k=x");
  for (const lan of ["http://192.168.1.5:48890/screen/whep?k=a", "http://172.17.0.169:48890/screen/whep?k=a",
                     "http://10.0.0.2:48890/screen/whep?k=a", "http://100.101.1.1:48890/screen/whep?k=a"])
    assert.equal(ok(lan), lan);
  // rejected
  for (const bad of ["http://76.1.2.3:48890/screen/whep?k=a",       // plain http across the internet
                     "http://172.32.0.1:48890/screen/whep?k=a",     // just outside 172.16/12
                     "https://evil.example/collect",                 // not a share endpoint
                     "https://evil.example/screen/whep/x",
                     "javascript:alert(1)", "file:///etc/passwd", "ftp://x/screen/whep",
                     "https://user:pw@h/screen/whep", "not a url", ""])
    assert.equal(ok(bad), null, bad);
  assert.equal(ok("https://h/screen/whep?" + "x".repeat(3000)), null);  // oversized
});

test("signal ws/lan/ice fields are validated too", () => {
  const { win } = boot();
  const { parseSignal, sigPayload } = win.__ds_test;
  const u = "https://h.example/screen/whep?k=1";
  let p = parseSignal(sigPayload(u, [{ urls: "turn:t.example:3478", username: "a", credential: "b" }],
                                 "wss://h.example/screen/ws?k=1", true, "av1", "http://192.168.1.5:48890/screen/whep?k=1"));
  assert.equal(p.ws, "wss://h.example/screen/ws?k=1");
  assert.equal(p.lan, "http://192.168.1.5:48890/screen/whep?k=1");
  assert.ok(p.ice);
  p = parseSignal(sigPayload(u, [{ urls: "https://evil" }], "ws://8.8.8.8/screen/ws", true, "av1",
                             "http://8.8.8.8:48890/screen/whep"));
  assert.equal(p.ice, null);
  assert.equal(p.ws, null);
  assert.equal(p.lan, null);
  assert.equal(parseSignal(sigPayload(u, [{ urls: ["stun:s:19302"], username: 5 }], null, false)).ice, null);
});

test("private address detection", () => {
  const { win } = boot();
  const priv = win.__ds_test.isPrivateHost;
  for (const h of ["10.1.2.3", "127.0.0.1", "172.16.0.1", "172.31.255.255", "192.168.0.1", "100.64.0.1", "[::1]", "fd12:3456::1", "fe80::1"])
    assert.equal(priv(h), true, h);
  for (const h of ["8.8.8.8", "172.15.0.1", "172.32.0.1", "100.128.0.1", "192.169.0.1", "example.com", "::ffff:8.8.8.8"])
    assert.equal(priv(h), false, h);
});

test("user CSS can't load remote resources", () => {
  const { win } = boot();
  const s = win.__ds_test.sanitizeUserCss;
  const css = s(`@import url("https://evil.example/x.css");
.a { background: url(https://evil.example/?k=1) }
.b { background: url( 'http://x' ) }
.c { background: u\\72l(https://evil.example) }
.d { background: image-set("https://evil.example/a.png" 1x) }
.e { background: url(data:image/png;base64,AAAA) }
:root { --dc-bg: #000 }`);
  assert.doesNotMatch(css, /evil\.example|http:\/\/x/);
  assert.match(css, /url\(data:image\/png;base64,AAAA\)/);
  assert.match(css, /--dc-bg: #000/);
});

test("theme tokens: presets, accent and corners become :root overrides", () => {
  const { win } = boot();
  const css = win.__ds_test.themeCss({ preset: "mocha", accent: "#ff0000", radius: "round", css: ".x{}" });
  assert.match(css, /^:root:root \{/);
  assert.match(css, /--dc-accent: #ff0000;/);          // explicit accent beats the preset's
  assert.match(css, /--dc-bg: #1e1e2e;/);
  assert.match(css, /--dc-radius-lg: 18px;/);
  assert.match(css, /\.x\{\}$/);
  assert.equal(win.__ds_test.themeCss({ preset: "discord", accent: "", radius: "default", css: "" }), "");
});

test("theme codes round-trip and junk is rejected", () => {
  const { win, store } = boot();
  const t = win.__ds_test;
  const th = { preset: "steam", accent: "#123abc", radius: "sharp", css: ".friend { color: red }" };
  const code = t.exportTheme(th);
  assert.match(code, /^dstheme:[A-Za-z0-9+/=]+$/);
  assert.deepEqual({ ...t.importTheme(code) }, th);
  assert.equal(t.importTheme("hello"), null);
  assert.equal(t.importTheme("dstheme:!!!"), null);
  // invalid fields fall back to defaults instead of reaching the page
  const bad = t.importTheme("dstheme:" + Buffer.from(JSON.stringify(
    { preset: "nope", accent: "red;}body{display:none", radius: "huge", css: 5 })).toString("base64"));
  assert.deepEqual({ ...bad }, { preset: "discord", accent: "", radius: "default", css: "" });
  assert.equal(store.has("ds_theme"), false);            // importing alone doesn't save
});
