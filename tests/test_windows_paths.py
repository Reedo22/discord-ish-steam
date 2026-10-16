"""Windows code paths of rp-webrtc.py, exercised on ANY OS by forcing IS_WIN and recording
the command lines instead of running them. Real-Windows behaviour (gdigrab actually
capturing, the installer, WASAPI) is covered by .github/workflows/test.yml on a Windows
runner; this file catches command-construction bugs without one.

    python3 -m unittest discover -s tests -v
"""
import os
import unittest
from unittest import mock

from test_daemon import d, reset_ws


class Recorder:
    """Replaces _spawn: records argv, returns a fake live process."""
    def __init__(self):
        self.cmds = []

    def __call__(self, cmd, **kw):
        self.cmds.append(cmd)
        p = mock.Mock()
        p.poll.return_value = None
        p.stdout = None
        return p


def on_windows(**extra):
    """Patch the daemon into 'Windows' with no real side effects."""
    rec = Recorder()
    patches = [
        mock.patch.object(d, "IS_WIN", True),
        mock.patch.object(d, "_spawn", rec),
        mock.patch.object(d, "ensure_mediamtx"),
        mock.patch.object(d, "is_wayland", return_value=False),
        mock.patch.object(d, "_working_encoder", return_value="h264_nvenc"),
        mock.patch.object(d.threading, "Thread"),          # no pump/WASAPI feeder threads
        mock.patch.object(d, "_open_log", return_value=d.subprocess.DEVNULL),
        mock.patch.object(d, "_wasapi_device", return_value=extra.get("wasapi")),
        mock.patch.object(d, "AUDIO", extra.get("audio", True)),
        mock.patch.object(d, "opus_over_rtmp", return_value=extra.get("opus", True)),
        # these capture tests are about the WASAPI-loopback command line
        mock.patch.object(d, "WasapiExcludeSteam", side_effect=RuntimeError("n/a")),
        mock.patch.object(d, "_steam_audio_pid", return_value=None),
    ]
    return rec, patches


class WindowsCapture(unittest.TestCase):
    def setUp(self):
        reset_ws()
        d.state.update({"ff": None, "fmp4": None, "wl_gst": None, "wl_ses": None})

    def run_capture(self, wasapi=None, audio=True, opus=True, **src):
        rec, patches = on_windows(wasapi=wasapi, audio=audio, opus=opus)
        for p in patches:
            p.start()
        try:
            ok = d.start_capture(**src)
        finally:
            for p in reversed(patches):
                p.stop()
            d.state.update({"ff": None, "fmp4": None})
        return ok, rec.cmds

    def test_monitor_left_of_primary_uses_negative_gdigrab_offset(self):
        ok, cmds = self.run_capture(geom="1920x1080+-1920+0")
        self.assertTrue(ok)
        self.assertEqual(len(cmds), 1)    # ONE capture + encode, tee'd to WebRTC and WS
        self.assertEqual(cmds[0].count("gdigrab"), 1)
        for c in cmds:
            i = c.index("gdigrab")
            self.assertEqual(c[c.index("-offset_x", i) + 1], "-1920")
            self.assertEqual(c[c.index("-video_size", i) + 1], "1920x1080")
            self.assertEqual(c[c.index("-i", i) + 1], "desktop")
            self.assertIn("h264_nvenc", c)

    def test_wasapi_audio_uses_device_native_format_and_reaches_webrtc(self):
        ok, cmds = self.run_capture(geom="2560x1440+0+0", wasapi={"index": 3, "rate": 44100, "ch": 6})
        c = cmds[0]
        self.assertIn("pipe:0", c)
        self.assertEqual(c[c.index("-ar") + 1], "44100")          # input side: device native
        self.assertEqual(c[c.index("-ac") + 1], "6")
        self.assertIn("libopus", c)                               # one Opus encode for both
        self.assertIn("[onfail=ignore:select='v,a':f=flv", c[-1])  # WebRTC copy HAS audio
        self.assertTrue(d.state["audio"])
        self.assertEqual(d.state["acodec"], "opus")

    def test_old_ffmpeg_falls_back_to_aac_on_ws_only(self):
        ok, cmds = self.run_capture(geom="2560x1440+0+0", opus=False,
                                    wasapi={"index": 3, "rate": 48000, "ch": 2})
        c = cmds[0]
        self.assertIn("aac", c)
        self.assertIn("[onfail=ignore:select='v':f=flv", c[-1])    # WebRTC video-only
        self.assertIn("[select='v,a':f=mp4", c[-1])                # WS keeps the sound

    def test_missing_pyaudiowpatch_means_video_only_not_failure(self):
        ok, cmds = self.run_capture(geom="1920x1080+0+0", wasapi=None)
        self.assertTrue(ok)
        self.assertNotIn("pipe:0", cmds[0])
        self.assertFalse(d.state["audio"])

    def test_window_title_is_a_single_argv_entry(self):
        title = 'Minecraft 1.21 - "Survival" ; & | weird'
        ok, cmds = self.run_capture(win=title)
        self.assertTrue(ok)
        for c in cmds:
            self.assertIsInstance(c, list)                 # never a shell string
            self.assertIn("title=" + title, c)

    def test_window_titles_with_shell_characters_pass_validation(self):
        with mock.patch.object(d, "IS_WIN", True):
            self.assertIsNone(d.validate_source(None, "Notepad ; rm -rf & dir", None))

    def test_dshow_camera(self):
        with mock.patch.object(d, "cameras", return_value=[{"id": "HD Pro Webcam C920", "name": "x"}]):
            ok, cmds = self.run_capture(cam="HD Pro Webcam C920")
        for c in cmds:
            i = c.index("dshow")
            self.assertEqual(c[i + 2], "video=HD Pro Webcam C920")


class WasapiPacing(unittest.TestCase):
    """WASAPI loopback returns NOTHING while the desktop is quiet; ffmpeg must still get a
    continuous real-time PCM stream or the whole share (video too) never starts."""

    def run_feed(self, stream, seconds=1.0, rate=48000, ch=2, clock=None):
        clock = clock or [0.0]
        written = []
        ticks = [0]

        class Proc:
            class stdin:
                @staticmethod
                def write(b):
                    written.append(b)

            def poll(self):
                ticks[0] += 1
                return None if clock[0] < seconds else 0

        def sleep(dt):
            clock[0] += dt
        d._paced_pcm_feed(Proc(), stream, rate, ch, 480, clock=lambda: clock[0], sleep=sleep)
        return b"".join(written)

    def test_quiet_device_gets_real_time_silence(self):
        quiet = mock.Mock()
        quiet.get_read_available.return_value = 0     # loopback: no packets at all
        out = self.run_feed(quiet, seconds=1.0)
        frames = len(out) // 4
        self.assertTrue(45000 <= frames <= 48000, frames)    # ~1 s of 48 kHz, never more
        self.assertEqual(set(out), {0})
        quiet.read.assert_not_called()                         # never blocks on read()

    def test_no_device_at_all_still_feeds(self):
        out = self.run_feed(None, seconds=0.5)
        self.assertTrue(22000 <= len(out) // 4 <= 24000)

    def test_real_audio_is_passed_through(self):
        clock = [0.0]

        def read(n, exception_on_overflow=False):
            clock[0] += n / 48000.0                            # a real device delivers in real time
            return b"\x01\x02" * n * 2                         # n stereo s16 frames
        loud = mock.Mock()
        loud.get_read_available.return_value = 960
        loud.read.side_effect = read
        out = self.run_feed(loud, seconds=0.05, clock=clock)
        self.assertIn(b"\x01\x02", out)


class WindowsAudioSource(unittest.TestCase):
    def test_prefers_steam_excluding_process_loopback(self):
        fake = mock.Mock()
        with mock.patch.object(d, "IS_WIN", True), mock.patch.object(d, "AUDIO", True), \
             mock.patch.object(d, "_steam_audio_pid", return_value=4321), \
             mock.patch.object(d, "WasapiExcludeSteam", return_value=fake) as w:
            src = d.audio_source()
        w.assert_called_once_with(4321)                 # the webhelper, not steam.exe
        self.assertIsInstance(src, d._WinExcludeSteamAudio)
        self.assertTrue(src.stdin)

    def test_old_windows_falls_back_to_whole_desktop(self):
        with mock.patch.object(d, "IS_WIN", True), mock.patch.object(d, "AUDIO", True), \
             mock.patch.object(d, "_steam_audio_pid", return_value=4321), \
             mock.patch.object(d, "WasapiExcludeSteam", side_effect=RuntimeError("no process loopback")), \
             mock.patch.object(d, "_wasapi_device", return_value={"index": 1, "rate": 48000, "ch": 2}):
            src = d.audio_source()
        self.assertIsInstance(src, d._WasapiAudio)

    def test_activation_failure_is_fast_on_non_windows(self):
        import time as _t
        t0 = _t.monotonic()
        with self.assertRaises(RuntimeError):
            d.WasapiExcludeSteam(1)
        self.assertLess(_t.monotonic() - t0, 2)


class WindowsProcessControl(unittest.TestCase):
    def test_kill_uses_taskkill_tree(self):
        p = mock.Mock(pid=4242)
        p.poll.return_value = None
        with mock.patch.object(d, "IS_WIN", True), mock.patch.object(d.subprocess, "run") as run:
            d._kill(p)
        argv = run.call_args[0][0]
        self.assertEqual(argv[:4], ["taskkill", "/F", "/T", "/PID"])
        self.assertEqual(argv[4], "4242")

    def test_cap_env_does_not_inject_display(self):
        with mock.patch.object(d, "IS_WIN", True), mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DISPLAY", None)
            self.assertNotIn("DISPLAY", d._cap_env())

    def test_dshow_device_listing_parse(self):
        stderr = ('[dshow @ 000001] "HD Pro Webcam C920" (video)\n'
                  '[dshow @ 000001]   Alternative name "@device_pnp_\\\\?\\usb#vid"\n'
                  '[dshow @ 000001] "NVIDIA Broadcast" (video)\n'
                  '[dshow @ 000001] "Microphone (Realtek)" (audio)\n')
        with mock.patch.object(d, "IS_WIN", True), \
             mock.patch.object(d.subprocess, "run", return_value=mock.Mock(stderr=stderr)):
            cams = d.cameras()
        self.assertEqual([c["id"] for c in cams], ["HD Pro Webcam C920"])   # junk + audio dropped


if __name__ == "__main__":
    unittest.main()
