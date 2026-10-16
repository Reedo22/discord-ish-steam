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
  const p = win.__ds_test.parseSignal("u|ice=!!notbase64|ws=%E0%A4%A|aud=7|zzz");
  assert.equal(p.url, "u");
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
