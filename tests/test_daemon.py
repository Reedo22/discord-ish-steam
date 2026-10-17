"""Tests for rp-webrtc.py (the screen-share daemon). Stdlib only, like the daemon.

    python3 -m unittest discover -s tests -v

Pure-logic tests always run. Tests that need ffmpeg skip cleanly without it.
"""
import importlib.util
import io
import http.server
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def load_daemon():
    spec = importlib.util.spec_from_file_location("rp_webrtc", os.path.join(ROOT, "rp-webrtc.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)          # __main__ guard: importing must NOT start the daemon
    return mod


d = load_daemon()


def box(btype, payload=b""):
    return struct.pack(">I", 8 + len(payload)) + btype + payload


class FakeClient:
    """Stands in for a WsClient: records frames, can be closed."""
    def __init__(self):
        self.frames, self.alive, self.closed = [], True, False

    def send(self, frame):
        self.frames.append(frame)

    def close(self):
        self.alive, self.closed = False, True


def reset_ws():
    with d.ws_lock:
        d.ws_clients.clear()
        d.fmp4_init["seg"] = None
        d.fmp4_init["owner"] = None


# --------------------------------------------------------------------------- WebSocket
class WebSocketFraming(unittest.TestCase):
    def test_accept_key_matches_rfc6455_example(self):
        # RFC 6455 section 1.3 worked example
        self.assertEqual(d._ws_accept("dGhlIHNhbXBsZSBub25jZQ=="), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")

    def test_frame_length_encodings(self):
        for n, hdr_len in ((0, 2), (125, 2), (126, 4), (65535, 4), (65536, 10)):
            f = d._ws_frame(b"x" * n)
            self.assertEqual(f[0], 0x82)                       # FIN + binary
            self.assertEqual(len(f), hdr_len + n)
        self.assertEqual(d._ws_frame(b"x" * 126)[1], 126)
        self.assertEqual(struct.unpack(">H", d._ws_frame(b"x" * 300)[2:4])[0], 300)
        self.assertEqual(struct.unpack(">Q", d._ws_frame(b"x" * 70000)[2:10])[0], 70000)


class WsClientQueue(unittest.TestCase):
    def test_overflow_closes_instead_of_buffering_forever(self):
        a, b = socket.socketpair()
        try:
            c = d.WsClient(a)
            # never read from b -> writer blocks once the socket buffer fills
            big = b"\0" * (1 << 20)
            for _ in range(d.WS_QUEUE_MAX + 50):
                c.send(big)
                if not c.alive:
                    break
            self.assertFalse(c.alive)
            self.assertTrue(c.dropped)
        finally:
            b.close()

    def test_frames_delivered_in_order(self):
        a, b = socket.socketpair()
        try:
            c = d.WsClient(a)
            for i in range(5):
                c.send(bytes([i]) * 10)
            got = b""
            b.settimeout(2)
            while len(got) < 50:
                got += b.recv(100)
            self.assertEqual(got, b"".join(bytes([i]) * 10 for i in range(5)))
            c.close()
        finally:
            b.close()


# --------------------------------------------------------------------------- fMP4 pump
class Fmp4Pump(unittest.TestCase):
    def setUp(self):
        reset_ws()

    def tearDown(self):
        reset_ws()

    def test_init_then_fragments_broadcast_as_moof_mdat_pairs(self):
        ftyp, moov = box(b"ftyp", b"isom"), box(b"moov", b"m" * 20)
        moof, mdat = box(b"moof", b"f" * 8), box(b"mdat", b"d" * 30)
        sent = []
        with mock.patch.object(d, "ws_broadcast", sent.append):
            # read the init seg while the stream is still "live"
            seen_init = []
            real_read = d._read_exact

            def spy(stream, n):
                with d.ws_lock:
                    if d.fmp4_init["seg"] and not seen_init:
                        seen_init.append(d.fmp4_init["seg"])
                return real_read(stream, n)
            with mock.patch.object(d, "_read_exact", spy):
                d.fmp4_pump(io.BytesIO(ftyp + moov + box(b"free") + moof + mdat + moof + mdat))
        self.assertEqual(seen_init, [ftyp + moov])
        self.assertEqual(sent, [moof + mdat, moof + mdat])

    def test_fragments_before_init_are_not_sent(self):
        sent = []
        with mock.patch.object(d, "ws_broadcast", sent.append):
            d.fmp4_pump(io.BytesIO(box(b"moof") + box(b"mdat")))
        self.assertEqual(sent, [])

    def test_stream_end_withdraws_init_and_closes_viewers(self):
        fc = FakeClient()
        with d.ws_lock:
            d.ws_clients.add(fc)
        d.fmp4_pump(io.BytesIO(box(b"ftyp") + box(b"moov")))
        self.assertIsNone(d.fmp4_init["seg"])
        self.assertTrue(fc.closed)
        self.assertEqual(len(d.ws_clients), 0)

    def test_old_stream_end_leaves_newer_capture_alone(self):
        newer = object()
        with d.ws_lock:
            d.fmp4_init["seg"], d.fmp4_init["owner"] = b"NEW", newer
        fc = FakeClient()
        with d.ws_lock:
            d.ws_clients.add(fc)
        d.fmp4_pump(io.BytesIO(b""))                 # an old encoder exiting late
        self.assertEqual(d.fmp4_init["seg"], b"NEW")
        self.assertFalse(fc.closed)

    def test_bad_box_sizes_stop_cleanly(self):
        for bad in (struct.pack(">I", 0) + b"mdat", struct.pack(">I", 4) + b"mdat",
                    struct.pack(">I", 1) + b"mdat" + struct.pack(">Q", 8)):
            with mock.patch.object(d, "ws_broadcast") as bc:
                d.fmp4_pump(io.BytesIO(box(b"ftyp") + box(b"moov") + bad + b"\0" * 64))
                bc.assert_not_called()

    def test_64bit_box_size(self):
        payload = b"z" * 10
        big_mdat = struct.pack(">I", 1) + b"mdat" + struct.pack(">Q", 16 + len(payload)) + payload
        sent = []
        with mock.patch.object(d, "ws_broadcast", sent.append):
            d.fmp4_pump(io.BytesIO(box(b"ftyp") + box(b"moov") + box(b"moof") + big_mdat))
        self.assertEqual(sent, [box(b"moof") + big_mdat])


# --------------------------------------------------------------------------- encode helpers
class EncodeHelpers(unittest.TestCase):
    def test_canvas_even_and_capped(self):
        with mock.patch.object(d, "MAX_H", ""):
            self.assertEqual(d._wl_canvas("1599x899+0+0", None), (1598, 898))
            self.assertEqual(d._wl_canvas("", True, (1001, 501)), (1000, 500))
        with mock.patch.object(d, "MAX_H", "1080"):
            self.assertEqual(d._wl_canvas("3840x2160+0+0", None), (1920, 1080))
            self.assertEqual(d._wl_canvas("1280x720+0+0", None), (1280, 720))   # never upscale

    def test_apply_scale_prepends_to_existing_vf(self):
        with mock.patch.object(d, "MAX_H", "720"):
            out = d._apply_scale(["-vf", "format=nv12"])
        self.assertEqual(out[0], "-vf")
        self.assertTrue(out[1].startswith("scale=-2:2*trunc(min(ih\\,720)/2),"))
        self.assertTrue(out[1].endswith("format=nv12"))

    def test_auto_prefers_av1_then_h264_then_cpu(self):
        def run(works):
            with mock.patch.object(d, "_ffmpeg_encoders",
                                   return_value="av1_nvenc h264_nvenc libx264"), \
                 mock.patch.object(d, "_probe_encoder", side_effect=lambda n: n in works), \
                 mock.patch.object(d.shutil, "which", return_value="/usr/bin/nvidia-smi"), \
                 mock.patch.object(d.os.path, "exists", return_value=False), \
                 mock.patch.dict(d._enc_pick, {}, clear=True):
                return d._working_encoder("auto"), d._working_encoder("h264")
        self.assertEqual(run({"av1_nvenc", "h264_nvenc"}), ("av1_nvenc", "h264_nvenc"))
        self.assertEqual(run({"h264_nvenc"}), ("h264_nvenc", "h264_nvenc"))   # no AV1 GPU
        self.assertEqual(run(set()), ("libx264", "libx264"))

    def test_failed_hw_probe_is_retried_but_success_is_cached(self):
        calls = []
        with mock.patch.object(d, "_ffmpeg_encoders", return_value="h264_nvenc"), \
             mock.patch.object(d, "_probe_encoder", side_effect=lambda n: calls.append(n)), \
             mock.patch.object(d.shutil, "which", return_value="/usr/bin/nvidia-smi"), \
             mock.patch.object(d.os.path, "exists", return_value=False), \
             mock.patch.dict(d._enc_pick, {}, clear=True):
            self.assertEqual(d._working_encoder("h264"), "libx264")
            n = len(calls)
            d._working_encoder("h264")                     # inside the retry window: cached
            self.assertEqual(len(calls), n)
            d._enc_pick["h264"]["t"] -= d.ENC_RETRY_S + 1  # window elapsed: probe hardware again
            d._working_encoder("h264")
            self.assertGreater(len(calls), n)
        with mock.patch.dict(d._enc_pick, {"av1": {"name": "av1_nvenc", "t": 0.0}}, clear=True), \
             mock.patch.object(d, "_probe_encoder") as pe:
            self.assertEqual(d._working_encoder("auto"), "av1_nvenc")
            pe.assert_not_called()

    def test_profile_pinned_only_for_h264(self):
        self.assertEqual(d._profile_args("h264_nvenc"), ["-profile:v", "high", "-level", "5.2"])
        self.assertEqual(d._profile_args("libx264"), ["-profile:v", "high", "-level", "5.2"])
        self.assertEqual(d._profile_args("av1_nvenc"), [])
        self.assertEqual(d.MSE_CODEC[d._codec_of("av1_qsv")], "av01.0.13M.08")

    def test_measured_nvenc_settings(self):
        a = d._enc_video_args("av1_nvenc")
        self.assertEqual(a[a.index("-preset") + 1], "p5")
        self.assertEqual(a[a.index("-tune") + 1], "ll")
        self.assertIn("-spatial-aq", a)
        with mock.patch.object(d, "_working_encoder", return_value="av1_nvenc"), \
             mock.patch.object(d, "FPS", "60"), mock.patch.object(d, "BITRATE", "8M"):
            _, _, args = d.pick_encoder()
        self.assertEqual(args[args.index("-g") + 1], "120")         # 2 s GOP
        self.assertEqual(args[args.index("-bufsize") + 1], "8M")    # 1 s VBV

    def test_ffmpeg_choice_prefers_gpu_over_bundled_newness(self):
        def pick(bundled_exists, hw):
            with mock.patch.object(d, "_ffbin", []), \
                 mock.patch.object(d.os.path, "exists", side_effect=lambda p: bundled_exists if p == d.BUNDLED_FFMPEG else False), \
                 mock.patch.object(d.shutil, "which", return_value="/usr/bin/ffmpeg"), \
                 mock.patch.object(d, "_hw_ok", side_effect=lambda b: hw[b == d.BUNDLED_FFMPEG]):
                return d.ffmpeg_bin()
        # hw = (system_has_gpu, bundled_has_gpu)
        self.assertEqual(pick(True, (True, True)), d.BUNDLED_FFMPEG)
        self.assertEqual(pick(True, (True, False)), "ffmpeg")        # bundled too new for driver
        self.assertEqual(pick(True, (False, False)), d.BUNDLED_FFMPEG)   # CPU anyway: keep audio
        self.assertEqual(pick(False, (True, True)), "ffmpeg")

    def test_mediamtx_config_is_loopback_rtmp_without_moq(self):
        cfg = open(os.path.join(ROOT, "bin", "rp-mediamtx.yml")).read()
        self.assertIn("rtmpAddress: 127.0.0.1:%d" % d.RTMP_PORT, cfg)
        self.assertIn("webrtcAddress: 127.0.0.1:%d" % d.WHEP_PORT, cfg)
        self.assertIn("webrtcLocalUDPAddress: :%d" % d.WEBRTC_UDP, cfg)
        self.assertRegex(cfg, r"(?m)^moq: no")
        self.assertRegex(cfg, r"(?m)^rtsp: no")

    def test_mtx_config_replaces_ice_block(self):
        ice = [{"urls": ["turn:a:3478", "turns:a:5349"], "username": "u", "credential": "p"},
               {"urls": "stun:s:19302"}]
        with mock.patch.object(d, "turn_ice", return_value=ice):
            path = d._mtx_config_path()
        txt = open(path).read()
        self.assertEqual(txt.count("webrtcICEServers2:"), 1)
        self.assertIn("  - url: turn:a:3478\n    username: \"u\"\n    password: \"p\"", txt)
        self.assertIn("  - url: stun:s:19302", txt)
        self.assertNotIn("stun.l.google.com", txt)       # template's static block dropped
        self.assertIn("paths:", txt)                       # the rest of the template kept


class CloudflaredUrl(unittest.TestCase):
    def test_ignores_api_endpoint_in_error_lines(self):
        err = ('ERR Error unmarshaling QuickTunnel response: error="failed to request quick '
               'Tunnel: Post \\"https://api.trycloudflare.com/tunnel\\""')
        self.assertIsNone(d._CF_URL_RE.search(err))
        ok = "INF |  https://investigator-purchased-rehab-within.trycloudflare.com  |"
        self.assertEqual(d._CF_URL_RE.search(ok).group(0),
                         "https://investigator-purchased-rehab-within.trycloudflare.com")


class SteamExcludedFromShareAudio(unittest.TestCase):
    """The share's desktop audio must NOT include Steam: the call's voices (echoed back to
    the friend) and any incoming share (two-way shares -> feedback loop)."""

    @staticmethod
    def graph(*apps):
        g = [{"id": 1, "type": "PipeWire:Interface:Node", "info": {"props": {"node.name": "ds-share-audio", "media.class": "Stream/Input/Audio"}}},
             {"id": 2, "type": "PipeWire:Interface:Port", "info": {"direction": "input", "props": {"node.id": 1, "audio.channel": "FL"}}},
             {"id": 3, "type": "PipeWire:Interface:Port", "info": {"direction": "input", "props": {"node.id": 1, "audio.channel": "FR"}}}]
        pid = 100
        for nid, binary, name, chans in apps:
            g.append({"id": nid, "type": "PipeWire:Interface:Node", "info": {"props": {
                "media.class": "Stream/Output/Audio", "application.process.binary": binary, "application.name": name}}})
            for ch in chans:
                pid += 1
                g.append({"id": pid, "type": "PipeWire:Interface:Port", "info": {"direction": "output", "props": {"node.id": nid, "audio.channel": ch}}})
        return g

    def test_links_every_app_but_steam(self):
        g = self.graph((10, "spotify", "Spotify", ["FL", "FR"]),
                       (11, "steamwebhelper", "Chromium", ["FL", "FR"]),
                       (12, None, "pw-play", ["MONO"]),
                       (13, "steam", "Steam Voice", ["FL", "FR"]))
        want, _ = d.PipeWireAppMix.plan(g)
        self.assertEqual(sorted(want), [(101, 2), (102, 3), (105, 2), (105, 3)])  # spotify L/R, mono -> both

    def test_surround_downmix_and_lfe_dropped(self):
        g = self.graph((10, "game", "Game", ["FL", "FR", "FC", "LFE", "RL", "RR"]))
        want, _ = d.PipeWireAppMix.plan(g)
        self.assertEqual(sorted(want), sorted([(101, 2), (102, 3), (103, 2), (103, 3), (105, 2), (106, 3)]))

    def test_no_capture_node_yet(self):
        self.assertEqual(d.PipeWireAppMix.plan([])[0], [])

    def test_pw_dump_with_several_documents_parses(self):
        raw = b'[{"id": 1}]\n[{"id": 2}, {"id": 3}]\n{"id": 4}\n'
        self.assertEqual([o["id"] for o in d._json_docs(raw)], [1, 2, 3, 4])
        self.assertEqual(d._json_docs(b""), [])
        self.assertEqual(d._json_docs(None), [])


class MediaMTXCompat(unittest.TestCase):
    def test_retries_without_keys_an_old_mediamtx_rejects(self):
        """Old mediamtx: 'ERR: json: unknown field "moq"' and exit 1 (verified with v1.9.3)."""
        import tempfile as tf
        with tf.TemporaryDirectory() as tmp:
            s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
            fake = os.path.join(tmp, "fake_mtx.py")
            with open(fake, "w") as f:
                f.write("import sys, socket, time\n"
                        "cfg = open(sys.argv[1]).read()\n"
                        "if 'moq:' in cfg:\n"
                        "    print('ERR: json: unknown field \"moq\"'); sys.exit(1)\n"
                        "l = socket.socket(); l.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
                        "l.bind(('127.0.0.1', %d)); l.listen(); time.sleep(30)\n" % port)
            launcher = os.path.join(tmp, "mediamtx")
            with open(launcher, "w") as f:
                f.write("#!/bin/sh\nexec %s %s \"$@\"\n" % (sys.executable, fake))
            os.chmod(launcher, 0o755)
            with mock.patch.object(d, "MEDIAMTX", launcher), mock.patch.object(d, "RTMP_PORT", port), \
                 mock.patch.object(d, "_kill_stray_mediamtx"), mock.patch.object(d, "public_ip", return_value=None), \
                 mock.patch.object(d, "turn_ice", return_value=[]), mock.patch.dict(d.state, {"mtx": None}):
                d.ensure_mediamtx()
                try:
                    self.assertTrue(d._port_open(port))
                finally:
                    d._kill(d.state["mtx"])


class NoConsole(unittest.TestCase):
    def test_pythonw_style_start_logs_to_file_instead_of_crashing(self):
        """pythonw.exe gives the process sys.stderr = None; the daemon must survive it."""
        import tempfile as tf
        with tf.TemporaryDirectory() as tmp:
            code = ("import sys, importlib.util; sys.stdout = sys.stderr = None\n"
                    "spec = importlib.util.spec_from_file_location('d', %r)\n"
                    "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
                    "sys.stderr.write('[test] logged without a console\\n')\n"
                    % os.path.join(ROOT, "rp-webrtc.py"))
            env = dict(os.environ, TMPDIR=tmp, TEMP=tmp, TMP=tmp)
            r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, timeout=30)
            self.assertEqual(r.returncode, 0, r.stderr)
            with open(os.path.join(tmp, "rp-webrtc.log")) as f:
                self.assertIn("logged without a console", f.read())


class SessionDetection(unittest.TestCase):
    def test_wayland_detected_late_and_then_sticks(self):
        with mock.patch.object(d, "_wl_seen", [False]), \
             mock.patch.object(d, "_detect_wayland", side_effect=[False, True]) as det:
            self.assertFalse(d.is_wayland())             # boot: compositor not up yet
            self.assertTrue(d.is_wayland())              # later: socket appeared
            self.assertTrue(d.is_wayland())              # latched, no more checks
            self.assertEqual(det.call_count, 2)


# --------------------------------------------------------------------------- request guards
class Guards(unittest.TestCase):
    def test_origins(self):
        ok = d.origin_allowed
        self.assertTrue(ok("https://steamloopback.host"))
        self.assertTrue(ok("https://store.steampowered.com"))
        self.assertTrue(ok("https://steamcommunity.com"))
        self.assertFalse(ok("https://evil.example"))
        self.assertFalse(ok("https://steamloopback.host.evil.example"))
        self.assertFalse(ok("https://notsteampowered.com"))
        self.assertFalse(ok("http://steamloopback.host"))
        self.assertFalse(ok("null"))
        self.assertTrue(ok(None))                          # curl / Lua backend
        self.assertTrue(ok(None, "none"))
        self.assertFalse(ok(None, "cross-site"))           # <img src=127.0.0.1/start> from a site

    def test_whep_target(self):
        good = ("https://abc-def.trycloudflare.com/screen/whep",
                "http://192.168.1.5:48890/screen/whep", "http://127.0.0.1:8889/screen/whep")
        bad = (None, "", "file:///etc/passwd", "http://169.254.169.254/latest/meta-data",
               "http://router.lan/admin", "gopher://x/screen/whep", "http:///screen/whep",
               "http://8.8.8.8:48890/screen/whep",          # plain http across the internet
               "http://share.example.com/screen/whep",      # plain http to a hostname
               "https://u:p@x.example/screen/whep")
        for t in good:
            self.assertTrue(d.whep_target_ok(t), t)
        for t in bad:
            self.assertFalse(d.whep_target_ok(t), t)

    def test_control_host_must_be_loopback(self):
        for h in ("127.0.0.1:48592", "localhost:48592", "[::1]:48592", "127.0.0.1", "LOCALHOST"):
            self.assertTrue(d.host_is_loopback(h), h)
        for h in (None, "", "evil.example:48592", "127.0.0.1.evil.example", "127.0.0.1:abc",
                  "192.168.1.5:48592", "localhost.evil:1"):
            self.assertFalse(d.host_is_loopback(h), h)

    def test_private_hosts(self):
        for h in ("10.0.0.1", "172.17.0.169", "192.168.1.1", "127.0.0.1", "100.101.2.3", "::1", "fd00::1"):
            self.assertTrue(d.is_private_host(h), h)
        for h in ("8.8.8.8", "172.32.0.1", "example.com", "", None):
            self.assertFalse(d.is_private_host(h), h)

    def test_source_validation(self):
        with mock.patch.object(d, "is_wayland", return_value=False), \
             mock.patch.object(d, "IS_WIN", False), \
             mock.patch.object(d, "cameras", return_value=[{"id": "/dev/video0", "name": "cam"}]):
            v = d.validate_source
            self.assertIsNone(v("3840x2160+3840+0", None, None))
            self.assertIsNone(v("1920x1080+-1920+0", None, None))     # left-of-primary monitor
            self.assertIsNone(v(None, "0x3a00007", None))
            self.assertIsNone(v(None, None, "/dev/video0"))
            self.assertEqual(v("1920x1080+0+0;id", None, None), "bad geom")
            self.assertEqual(v(None, "0x1; rm -rf ~", None), "bad window id")
            self.assertEqual(v(None, "0x1 2>/dev/null|sh", None), "bad window id")
            self.assertEqual(v(None, None, "/etc/shadow"), "unknown camera")
        with mock.patch.object(d, "is_wayland", return_value=True):
            self.assertIsNone(d.validate_source(None, "portal", None))


# --------------------------------------------------------------------------- HTTP control API
class ControlApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), d.H)
        cls.srv.daemon_threads = True
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def get(self, path, headers=None, method="GET", data=None):
        req = urllib.request.Request(self.base + path, headers=headers or {},
                                     method=method, data=data)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def test_ping_from_steam_echoes_origin(self):
        code, hdr, body = self.get("/ping", {"Origin": "https://steamloopback.host"})
        self.assertEqual(code, 200)
        self.assertEqual(hdr["Access-Control-Allow-Origin"], "https://steamloopback.host")
        j = json.loads(body)
        for k in ("ok", "running", "sharing", "streaming", "viewers", "cf_gen", "ws", "whep"):
            self.assertIn(k, j)

    def test_foreign_origin_refused_everywhere(self):
        evil = {"Origin": "https://evil.example"}
        with mock.patch.object(d, "start_capture") as sc:
            for path in ("/ping", "/start?geom=100x100%2B0%2B0", "/sources", "/url", "/stop"):
                code, _, body = self.get(path, evil)
                self.assertEqual(code, 403, path)
                self.assertNotIn(b"trycloudflare", body)
            sc.assert_not_called()
        code, _, _ = self.get("/whep?target=http://x/screen/whep", evil, "POST", b"v=0")
        self.assertEqual(code, 403)
        code, _, _ = self.get("/whep", evil, "OPTIONS")
        self.assertEqual(code, 403)

    def test_cross_site_no_cors_get_refused(self):
        with mock.patch.object(d, "start_capture") as sc:
            code, _, _ = self.get("/start?geom=100x100%2B0%2B0", {"Sec-Fetch-Site": "cross-site"})
            self.assertEqual(code, 403)
            sc.assert_not_called()

    def test_start_rejects_injection_before_capture(self):
        with mock.patch.object(d, "start_capture") as sc, \
             mock.patch.object(d, "is_wayland", return_value=False), \
             mock.patch.object(d, "IS_WIN", False):
            code, _, body = self.get("/start?win=0x1%3Bid&local=1")
            self.assertEqual(code, 400)
            self.assertEqual(json.loads(body)["error"], "bad window id")
            sc.assert_not_called()

    def test_start_reports_failure_reason(self):
        def fail(**kw):
            d.state["err"] = "Screen picker was cancelled"
            return False
        with mock.patch.object(d, "start_capture", side_effect=fail), \
             mock.patch.object(d, "turn_ice", return_value=[]), \
             mock.patch.object(d, "pick_encoder", return_value=("libx264", [], [])):
            code, _, body = self.get("/start?geom=100x100%2B0%2B0&local=1&br=6",
                                     {"Origin": "https://steamloopback.host"})
        j = json.loads(body)
        self.assertEqual(code, 200)
        self.assertFalse(j["ok"])
        self.assertEqual(j["error"], "Screen picker was cancelled")
        self.assertNotEqual(j["br"], "6")                # unit-less bitrate ignored

    def test_whep_proxy_rejects_non_whep_targets(self):
        code, _, _ = self.get("/whep?target=" + urllib.request.quote("http://127.0.0.1:1/admin"),
                              method="POST", data=b"v=0")
        self.assertEqual(code, 400)

    def test_dns_rebinding_refused(self):
        # A page that rebinds its own name to 127.0.0.1 makes SAME-ORIGIN requests: no
        # Origin header, Sec-Fetch-Site: same-origin. Only the Host header gives it away.
        with mock.patch.object(d, "start_capture") as sc:
            for path in ("/ping", "/start?geom=100x100%2B0%2B0&local=1", "/url"):
                code, _, body = self.get(path, {"Host": "evil.example:48592",
                                                "Sec-Fetch-Site": "same-origin"})
                self.assertEqual(code, 403, path)
                self.assertNotIn(b"whep", body)
            sc.assert_not_called()

    def test_duplicate_start_keeps_running_share(self):
        d.state.pop("start_sig", None)
        with mock.patch.object(d, "start_capture", return_value=True) as sc, \
             mock.patch.object(d, "capture_alive", return_value=True), \
             mock.patch.object(d, "turn_ice", return_value=[]), \
             mock.patch.object(d, "pick_encoder", return_value=("libx264", [], [])), \
             mock.patch.object(d, "is_wayland", return_value=False), \
             mock.patch.object(d, "IS_WIN", False):
            q = "/start?geom=100x100%2B0%2B0&local=1&fps=30"
            k1 = json.loads(self.get(q)[2])["whep"]
            k2 = json.loads(self.get(q)[2])["whep"]              # double-click
            self.assertEqual(sc.call_count, 1)
            self.assertEqual(k1, k2)                               # same share, same link
            self.get("/start?geom=100x100%2B0%2B0&local=1&fps=60")  # a real option change
            self.assertEqual(sc.call_count, 2)
        d.state.pop("start_sig", None)

    def test_whep_proxy_body_cap(self):
        code, _, _ = self.get("/whep?target=" + urllib.request.quote("https://h.example/screen/whep"),
                              method="POST", data=b"x" * (d.MAX_SDP + 1))
        self.assertEqual(code, 413)

    def test_whep_proxy_does_not_follow_redirects(self):
        hits = []

        class Bounce(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                if self.path.startswith("/secret"):
                    hits.append(self.path)
                    self.send_response(200); self.end_headers(); self.wfile.write(b"internal")
                    return
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.send_response(307)
                self.send_header("Location", "http://127.0.0.1:%d/secret" % self.server.server_address[1])
                self.end_headers()

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Bounce)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            target = "http://127.0.0.1:%d/screen/whep" % srv.server_address[1]
            code, _, body = self.get("/whep?target=" + urllib.request.quote(target), method="POST",
                                     data=b"v=0")
        finally:
            srv.shutdown(); srv.server_close()
        self.assertEqual(hits, [])
        self.assertNotEqual(body, b"internal")

    def test_preflight_for_steam(self):
        code, hdr, _ = self.get("/whep", {"Origin": "https://steamloopback.host"}, "OPTIONS")
        self.assertEqual(code, 204)
        self.assertEqual(hdr["Access-Control-Allow-Origin"], "https://steamloopback.host")


class StopCancelsPicker(unittest.TestCase):
    def test_stop_unblocks_a_pending_portal_wait(self):
        """/stop must not hang behind a share that's waiting on the compositor's picker."""
        class Bus:
            def get_unique_name(self):
                return ":1.42"

            def add_signal_receiver(self, *a, **k):
                return mock.Mock()
        d._portal["cancel"].clear()
        err = []

        def waiter():
            try:
                d._portal_call(Bus(), lambda *a: None, [], {}, 30)
            except RuntimeError as e:
                err.append(str(e))
        with mock.patch.object(d, "_sv", side_effect=lambda x: x):
            t = threading.Thread(target=waiter)
            t.start()
            time.sleep(0.3)
            d._portal["cancel"].set()
            t.join(3)
        d._portal["cancel"].clear()
        self.assertFalse(t.is_alive())
        self.assertIn("cancelled", err[0])


# --------------------------------------------------------------------------- end to end
@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
class EndToEndWs(unittest.TestCase):
    """Real ffmpeg fMP4 -> fmp4_pump -> media server -> a WebSocket viewer. Exercises the
    same muxer flags the daemon uses (FMP4_OUT) with a synthetic test source."""

    def setUp(self):
        reset_ws()
        d.state["key"] = "testkey"
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), d.MediaH)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]
        self.ff = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re",
             "-f", "lavfi", "-i", "testsrc2=s=320x180:r=30", "-t", "4",
             "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
             "-profile:v", "high", "-bf", "0", "-g", "30", "-pix_fmt", "yuv420p"] + d.FMP4_OUT,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.pump = threading.Thread(target=d.fmp4_pump, args=(self.ff.stdout,), daemon=True)
        self.pump.start()

    def tearDown(self):
        self.ff.kill()
        self.ff.wait()
        self.srv.shutdown()
        self.srv.server_close()
        reset_ws()

    def _ws_connect(self, key="testkey"):
        s = socket.create_connection(("127.0.0.1", self.port), 5)
        s.sendall(b"GET /screen/ws?k=" + key.encode() + b" HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n"
                  b"Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                  b"Sec-WebSocket-Version: 13\r\n\r\n")
        f = s.makefile("rb")
        status = f.readline()
        while f.readline() not in (b"\r\n", b""):
            pass
        return s, f, status

    @staticmethod
    def _read_msg(f):
        h = f.read(2)
        if len(h) < 2:
            return None
        n = h[1] & 0x7F
        if n == 126:
            n = struct.unpack(">H", f.read(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", f.read(8))[0]
        return f.read(n)

    def test_wrong_or_missing_key_is_refused(self):
        for k in ("", "nope", "testke"):
            s, f, status = self._ws_connect(k)
            self.assertIn(b"404", status, k)
            s.close()

    def test_viewer_gets_init_then_fragments_then_close_on_encoder_exit(self):
        deadline = time.time() + 5
        while d.fmp4_init["seg"] is None and time.time() < deadline:
            time.sleep(0.05)
        self.assertIsNotNone(d.fmp4_init["seg"], "encoder never produced an init segment")
        s, f, status = self._ws_connect()
        self.assertIn(b"101", status)
        first = self._read_msg(f)
        self.assertEqual(first[4:8], b"ftyp")
        self.assertIn(b"moov", first)
        frags = [self._read_msg(f) for _ in range(5)]
        for m in frags:
            self.assertEqual(m[4:8], b"moof")
            self.assertIn(b"mdat", m)
        # encoder exits (the -t 4 runs out) -> the viewer is closed, /ping-style state clears
        self.ff.wait(10)
        self.pump.join(5)
        self.assertIsNone(d.fmp4_init["seg"])
        s.settimeout(3)
        while True:
            m = self._read_msg(f)
            if m is None:
                break
        s.close()


if __name__ == "__main__":
    unittest.main()
