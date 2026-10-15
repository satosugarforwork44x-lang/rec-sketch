"""test_rec.py — レックスケッチを Hub を起動せずに確かめる（py -m unittest discover -s tests）。

本物のマイクの代わりに、決まった音を返す「にせの soundcard」を使う。
"""
import importlib.util
import json
import math
import os
import stat
import sys
import tempfile
import time
import unittest
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

try:
    import numpy as np
except ImportError:  # numpy が無い環境では録音のテストを飛ばす
    np = None


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Store:
    def __init__(self):
        self.d = {}

    def get(self, scope, key, default=None):
        return self.d.get((scope, key), default)

    def set(self, scope, key, value):
        self.d[(scope, key)] = json.loads(json.dumps(value))


class _Obj:
    pass


# ---------- にせの soundcard ----------
class _FakeRec:
    def __init__(self, freq, amp, ch):
        self.freq, self.amp, self.ch, self.n = freq, amp, ch, 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def record(self, numframes):
        time.sleep(numframes / 48000 / 20)          # 本物より 20 倍速く進む
        t = (np.arange(numframes) + self.n) / 48000
        self.n += numframes
        x = (self.amp * np.sin(2 * math.pi * self.freq * t)).astype(np.float32)
        return np.repeat(x[:, None], self.ch, axis=1)


class _FakeDev:
    def __init__(self, id, name, freq, amp):
        self.id, self.name, self.freq, self.amp = id, name, freq, amp

    def recorder(self, samplerate, channels, blocksize=None):
        return _FakeRec(self.freq, self.amp, channels)


class _FakeSC:
    mic = _FakeDev("mic-1", "テストのマイク", 440, 0.5)
    spk = _FakeDev("spk-1", "スピーカー", 0, 0)
    loop = _FakeDev("spk-1", "スピーカー", 880, 0.25)

    def default_microphone(self):
        return self.mic

    def default_speaker(self):
        return self.spk

    def all_microphones(self, include_loopback=False):
        return [self.mic]

    def get_microphone(self, id, include_loopback=False):
        return self.loop if include_loopback else self.mic


@unittest.skipIf(np is None, "numpy が無い")
class RecordTest(unittest.TestCase):
    def setUp(self):
        self.api = _load("hubmod_rec_test", "api.py")
        self.api._recmod.load_backend = lambda: _FakeSC()
        self.tmp = Path(tempfile.mkdtemp())
        ctx = _Obj()
        ctx.store, ctx.data_dir, ctx.config = _Store(), self.tmp / "data", {}
        ctx.host = _Obj()
        ctx.host.config = {"modules": {"folders": {"root": str(self.tmp / "registries")}}}
        self.api.setup(ctx)
        self.api._find["state"] = "done"           # faster-whisper は探さない

    def _record(self, source, secs=0.5):
        self.api.start(source=source, title="定例/会議")
        time.sleep(secs)
        self.assertEqual(self.api.status()["items"][0][0], "REC")
        return self.api.stop()

    def test_folder_and_guide(self):
        st = self.api.state()
        self.assertEqual(Path(st["folder"]), self.tmp / "registries" / "Rec Sketch")
        self.assertTrue((Path(st["folder"]) / "REC_FORMAT.md").is_file())

    def test_mic_only_is_mono(self):
        r = self._record("mic")
        self.assertIn("定例_会議", r["saved"])                       # ファイル名に使えない字は _
        with wave.open(str(Path(r["folder"]) / r["saved"])) as w:
            self.assertEqual((w.getnchannels(), w.getframerate(), w.getsampwidth()), (1, 48000, 2))
            self.assertGreater(w.getnframes(), 48000 // 10)
        self.assertIsNone(self.api.status())

    def test_both_is_two_channels_left_me_right_pc(self):
        r = self._record("both")
        with wave.open(str(Path(r["folder"]) / r["saved"])) as w:
            self.assertEqual(w.getnchannels(), 2)
            x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").reshape(-1, 2) / 32767
        self.assertAlmostEqual(float(np.max(np.abs(x[:, 0]))), 0.5, places=2)    # 左 = マイク
        self.assertAlmostEqual(float(np.max(np.abs(x[:, 1]))), 0.25, places=2)   # 右 = PC の音
        f = r["files"][0]
        self.assertEqual((f["channels"], f["text"]), (2, None))

    def test_never_overwrites_and_one_at_a_time(self):
        a = self._record("mic", 0.2)["saved"]
        b = self._record("mic", 0.2)["saved"]
        self.assertNotEqual(a, b)
        self.api.start(source="mic")
        with self.assertRaises(self.api.RecError):
            self.api.start(source="mic")
        self.api.stop()

    def test_levels_while_recording(self):
        self.api.start(source="both")
        time.sleep(0.3)
        lv = self.api.live()["rec"]["levels"]
        self.api.stop()
        self.assertAlmostEqual(lv[0], 20 * math.log10(0.5), delta=0.5)
        self.assertAlmostEqual(lv[1], 20 * math.log10(0.25), delta=0.5)

    def test_blob_only_inside_folder(self):
        name = self._record("mic", 0.2)["saved"]
        self.assertTrue(self.api.blob_path(name).is_file())
        for bad in ("../x.wav", "REC_FORMAT.md", "nothing.wav"):
            with self.assertRaises(self.api.RecError):
                self.api.blob_path(bad)

    def test_mics(self):
        self.assertEqual(self.api.mics()[0]["name"], "テストのマイク")

    @unittest.skipIf(os.name == "nt", "にせの Python（シェルスクリプト）は Windows では動かない")
    def test_transcribe_job_reads_progress_and_finds_text(self):
        name = self._record("both", 0.2)["saved"]
        fake = self.tmp / "fake-python"
        fake.write_text("#!/bin/sh\n"
                        'echo \'{"type": "stage", "stage": "model"}\'\n'
                        'echo \'{"type": "progress", "p": 0.5}\'\n'
                        'printf "# t\\n" > "$3"\n'
                        'echo "{\\"type\\": \\"done\\", \\"out\\": \\"$3\\"}"\n', encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        self.api._ctx.store.set("rec", "whisper", {"python": str(fake), "pyver": "3.14", "version": "1"})
        job = self.api.transcribe(name, model="small", lang="ja")
        self.assertEqual(job["state"], "running")
        for _ in range(50):
            if self.api.live()["job"]["state"] != "running":
                break
            time.sleep(0.1)
        self.assertEqual(self.api.live()["job"]["state"], "done")
        f = next(x for x in self.api.state()["files"] if x["file"] == name)
        self.assertEqual(f["text"], Path(name).stem + ".md")

    @unittest.skipIf(importlib.util.find_spec("av") is None, "PyAV が無い")
    def test_share_makes_small_mono_m4a_and_never_overwrites(self):
        import av
        name = self._record("both", 0.4)["saved"]
        outs = []
        for _ in range(2):
            self.assertEqual(self.api.share(name)["state"], "running")
            for _ in range(100):
                if self.api.live()["share"]["state"] != "running":
                    break
                time.sleep(0.1)
            self.assertEqual(self.api.live()["share"]["state"], "done", self.api.live()["share"])
            outs.append(self.api.live()["share"]["out"])
        self.assertEqual(outs, [Path(name).stem + ".m4a", Path(name).stem + " (2).m4a"])
        folder = Path(self.api.state()["folder"])
        with av.open(str(folder / outs[0])) as c:
            a = c.streams.audio[0]
            self.assertEqual((a.codec_context.name, a.codec_context.channels), ("aac", 1))   # 2ch は 1 本に混ぜる
        self.assertLess((folder / outs[0]).stat().st_size, (folder / name).stat().st_size / 5)
        f = next(x for x in self.api.state()["files"] if x["file"] == name)
        self.assertEqual(f["m4a"], outs[0])
        self.assertEqual(self.api.share_path(name), str(folder / outs[0]))

    def test_bookmarks_without_screen_are_kept_as_times(self):
        self.api.start(source="mic")
        time.sleep(0.2)
        self.api.mark()
        self.assertEqual(self.api.live()["rec"]["marks"], 1)
        name = self.api.stop()["saved"]
        f = next(x for x in self.api.state()["files"] if x["file"] == name)
        self.assertEqual((f["marks"], f["shots"]), (1, 0))

    @unittest.skipIf(importlib.util.find_spec("PIL") is None, "Pillow が無い")
    def test_screen_on_keeps_stills_next_to_the_recording(self):
        fake = _FakeMSS()
        self.api._screen.load_backend = lambda: fake
        self.api.start(source="mic", screen=True, monitor=1)
        time.sleep(0.3)
        self.api.mark()
        time.sleep(0.8)
        live = self.api.live()["rec"]
        self.assertTrue(live["screen"]["on"])
        name = self.api.stop()["saved"]
        f = next(x for x in self.api.state()["files"] if x["file"] == name)
        self.assertGreaterEqual(f["shots"], 1)
        self.assertTrue(Path(f["shotsDir"]).is_dir())

    @unittest.skipIf(importlib.util.find_spec("PIL") is None, "Pillow が無い")
    def test_look_back_lists_stills_and_serves_only_listed_images(self):
        fake = _FakeMSS()
        self.api._screen.load_backend = lambda: fake
        self.api.start(source="mic", screen=True)
        time.sleep(0.2)
        self.api.mark()
        time.sleep(0.6)
        live = self.api.shots()
        self.assertTrue(live["live"])
        self.assertGreaterEqual(len(live["shots"]), 1)
        name = self.api.stop()["saved"]
        d = self.api.shots(name)
        key = d["shots"][0]["key"]
        self.assertTrue(self.api.blob_path(f"s|{key}").is_file())
        th = self.api.blob_path(f"th|{key}")
        self.assertEqual(th.suffix, ".jpg")
        for bad in (f"s|{Path(name).stem}|shots.json", f"s|{Path(name).stem}|../x.png", "s|nothing|t000001-mark.png"):
            with self.assertRaises(self.api.RecError):
                self.api.blob_path(bad)
        self.api.view(name)
        self.assertEqual(self.api.shots()["file"], name)        # 一覧から選んだ録音

    def test_auto_stills_pause_while_look_back_is_open_only_without_hub_support(self):
        self.api._exclude.update(on=False, supported=False)
        self.api.viewer_open(True)
        self.assertFalse(self.api._auto_ok())
        self.api.viewer_open(False)
        self.assertTrue(self.api._auto_ok())
        calls = []
        self.api._ctx.host.set_capture_excluded = lambda on, owner="": calls.append((on, owner)) or {"on": on, "supported": True}
        self.api._set_exclude(True)
        self.api.viewer_open(True)
        self.assertTrue(self.api._auto_ok())                  # Hub が窓を写さないので止めない
        self.api._set_exclude(False)
        self.assertEqual(calls, [(True, "rec"), (False, "rec")])

    def test_transcribe_needs_whisper(self):
        name = self._record("mic", 0.2)["saved"]
        self.api._ctx.store.set("rec", "whisper", None)
        with self.assertRaises(self.api.RecError):
            self.api.transcribe(name)


class TranscribeTest(unittest.TestCase):
    """transcribe.py の並べ方（faster-whisper なしで確かめられる所）。"""

    def setUp(self):
        self.t = _load("rec_transcribe_test", "transcribe.py")

    def test_echo_of_pc_sound_is_dropped_from_me(self):
        mine = [{"start": 1.0, "end": 3.0, "text": "来週の予定を確認します"}, {"start": 5.0, "end": 6.0, "text": "はい、わかりました"}]
        theirs = [{"start": 1.1, "end": 3.1, "text": "来週の予定を確認します。"}]
        self.assertEqual([m["text"] for m in self.t.drop_echo(mine, theirs)], ["はい、わかりました"])

    def test_merge_joins_same_speaker_and_sorts(self):
        segs = [{"start": 4, "end": 5, "who": "相手", "text": "お願いします"},
                {"start": 0, "end": 1, "who": "自分", "text": "始めます"},
                {"start": 1.5, "end": 2, "who": "自分", "text": "よろしく"},
                {"start": 9, "end": 10, "who": "自分", "text": " "}]
        out = self.t.merge(segs)
        self.assertEqual([(o["who"], o["text"]) for o in out], [("自分", "始めますよろしく"), ("相手", "お願いします")])

    def test_markdown(self):
        md = self.t.render_md("定例", "a.wav", 3725, True, "small", "ja",
                              [{"start": 3725, "end": 3726, "who": "自分", "text": "以上です"}])
        self.assertIn("[01:02:05] 自分: 以上です", md)
        self.assertIn("自分＝マイク／相手＝PC の音", md)
        en = self.t.render_md("Weekly", "a.wav", 5, True, "small", "en", [{"start": 1, "end": 2, "who": "Me", "text": "Hi"}])
        self.assertIn("[00:00:01] Me: Hi", en)
        self.assertIn("Me = microphone", en)

    def test_check_without_whisper_says_no(self):
        import subprocess
        r = subprocess.run([sys.executable, str(ROOT / "transcribe.py"), "--check"], capture_output=True, text=True)
        info = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertIn("ok", info)


if __name__ == "__main__":
    unittest.main()


class MixFormatPatchTest(unittest.TestCase):
    """機器の形式が soundcard の想定と違う（assert で止まる）ときに、float32 の形式を作って渡すこと。"""

    def test_falls_back_to_own_float32_format(self):
        import struct
        import types
        rec = _load("rec_recorder_patch_test", "recorder.py")
        seen = {}

        class _Vtbl:
            def Initialize(self, this, share, flags, duration, period, fmt, guid):
                seen.update(share=share, flags=flags, fmt=bytes(fmt))
                return 0

        class _Ptr:
            lpVtbl = _Vtbl()

        class _AudioClient:
            def __init__(self, ptr, samplerate, channels, blocksize, isloopback, exclusive_mode=False):
                assert False   # soundcard の形式の確かめに通らなかった、のまね

            deviceperiod = (0.01, 0.003)

        mf = types.SimpleNamespace(
            _AudioClient=_AudioClient,
            _ffi=types.SimpleNamespace(new=lambda t, b: bytearray(b), cast=lambda t, b: b, NULL=None),
            _ole32=types.SimpleNamespace(AUDCLNT_SHAREMODE_SHARED=0, AUDCLNT_SHAREMODE_EXCLUSIVE=1),
            _com=types.SimpleNamespace(check_error=lambda hr: None))
        sys.modules["soundcard.mediafoundation"] = mf
        try:
            rec._patch_mix_format()
            c = _AudioClient([[_Ptr()]], 48000, 1, 24000, True)
        finally:
            del sys.modules["soundcard.mediafoundation"]
        tag, ch, sr, avg, align, bits, cb, valid, mask, guid = struct.unpack("<HHIIHHHHI16s", seen["fmt"])
        self.assertEqual((tag, ch, sr, avg, align, bits, cb, valid, mask), (0xFFFE, 1, 48000, 192000, 4, 32, 22, 32, 4))
        self.assertEqual(guid.hex(), "0300000000001000800000aa00389b71")
        self.assertTrue(seen["flags"] & 0x00020000)          # ループバック
        self.assertEqual((c.samplerate, c.channelmap), (48000, [0]))


# ---------- 画面（にせの mss） ----------
class _FakeShot:
    def __init__(self, img):
        self.size, self.rgb = img.size, img.tobytes()


class _FakeMSSInst:
    def __init__(self, owner):
        self.owner = owner
        self.monitors = [{"left": 0, "top": 0, "width": 320, "height": 240}, {"left": 0, "top": 0, "width": 320, "height": 240}]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def grab(self, mon):
        return _FakeShot(self.owner.frame())


class _FakeMSS:
    """frame() が今の画面。テストから scene を差し替える。"""

    def __init__(self):
        self.scene = "a"
        self.n = 0

    def MSS(self):  # noqa: N802
        return _FakeMSSInst(self)

    def frame(self):
        from PIL import Image, ImageDraw
        self.n += 1
        img = Image.new("RGB", (320, 240), (240, 240, 240))
        d = ImageDraw.Draw(img)
        if self.scene == "a":
            d.rectangle([20, 20, 300, 120], fill=(30, 60, 200))
        elif self.scene == "b":
            d.rectangle([20, 100, 300, 230], fill=(200, 40, 40))
        elif self.scene == "moving":
            x = (self.n * 37) % 260
            d.rectangle([x, 0, x + 60, 240], fill=(0, 0, 0))
        d.rectangle([280, 200, 318, 238], fill=((self.n * 50) % 255, 0, 0))   # 隅の小さな「顔」はいつも動く
        return img


@unittest.skipIf(importlib.util.find_spec("PIL") is None, "Pillow が無い")
class ScreenTest(unittest.TestCase):
    def setUp(self):
        self.s = _load("rec_screen_test", "screen.py")
        self.tmp = Path(tempfile.mkdtemp())

    def run_scenes(self, scenes, mark_at=()):
        """scenes: [(場面, 秒)]。時計は 0.5 秒ずつ進める（実時間は待たない）。"""
        fake, clock = _FakeMSS(), [0.0]
        w = self.s.Watcher(self.tmp / "rec", clock=lambda: clock[0], mss_mod=fake, interval=0.001)
        dec, saved = self.s.Decider(), []
        # Watcher のスレッドを使わず、同じ手順を時計つきで回す（決め方を確かめる）
        with fake.MSS() as m:
            for scene, secs in scenes:
                fake.scene = scene
                for _ in range(int(secs / 0.5)):
                    clock[0] += 0.5
                    img = w._grab(m)
                    if any(abs(clock[0] - t) < 0.25 for t in mark_at):
                        w._save(img, clock[0], "mark")
                        dec.remember(self.s.small_of(img))
                    elif dec.feed(self.s.small_of(img), clock[0]):
                        w._save(img, clock[0], "auto")
        return w.shots

    def test_slide_change_is_kept_once_after_it_settles(self):
        shots = self.run_scenes([("a", 15), ("b", 15)])
        self.assertEqual([s["kind"] for s in shots], ["auto", "auto"])
        self.assertGreaterEqual(shots[1]["t"], 15 + self.s.STABLE_SEC)       # 止まってから

    def test_corner_face_and_moving_video_are_not_kept(self):
        shots = self.run_scenes([("a", 15), ("moving", 20)])
        self.assertEqual(len(shots), 1)

    def test_going_back_to_the_same_slide_is_not_kept_again(self):
        shots = self.run_scenes([("a", 15), ("b", 15), ("a", 15)])
        self.assertEqual(len(shots), 2)

    def test_bookmark_is_kept_at_once_as_png_and_indexed(self):
        shots = self.run_scenes([("a", 5)], mark_at=(2.0,))
        marks = [s for s in shots if s["kind"] == "mark"]
        self.assertEqual(len(marks), 1)
        self.assertTrue(marks[0]["file"].endswith("-mark.png"))
        self.assertEqual(self.s.read_index(self.tmp / "rec")[0]["file"], marks[0]["file"])

    def test_watcher_thread_runs_and_marks(self):
        fake, t = _FakeMSS(), [0.0]
        w = self.s.Watcher(self.tmp / "rec2", clock=lambda: t[0], mss_mod=fake, interval=0.01)
        w.start()
        t[0] = 3.0
        w.mark()
        time.sleep(0.3)
        w.stop()
        self.assertEqual(w.counts()["mark"], 1, w.error)
        self.assertTrue((self.tmp / "rec2" / w.shots[0]["file"]).is_file())


class HotkeyTest(unittest.TestCase):
    def test_parse_and_normalize(self):
        h = _load("rec_hotkey_test", "hotkey.py")
        self.assertEqual(h.parse("F9"), (0, 0x78))
        self.assertEqual(h.parse("ctrl+shift+F10"), (0x6, 0x79))
        self.assertEqual(h.normalize("scrolllock"), "ScrollLock")
        with self.assertRaises(h.HotkeyError):
            h.parse("Ctrl+Q")


class TranscriptShotsTest(unittest.TestCase):
    def test_stills_and_bookmarks_go_between_lines_in_time_order(self):
        t = _load("rec_transcribe_shots", "transcribe.py")
        md = t.render_md("会議", "2026-10-15 1000 会議.wav", 60, True, "small", "ja",
                         [{"start": 3, "end": 4, "who": "自分", "text": "始めます"}, {"start": 30, "end": 31, "who": "相手", "text": "はい"}],
                         [{"t": 12, "kind": "auto", "file": "t000012-auto.jpg"}, {"t": 40, "kind": "mark", "file": None}])
        lines = [l for l in md.splitlines() if l.startswith("[")]
        self.assertEqual(lines[1], "[00:00:12] 📷 ![](<2026-10-15 1000 会議/t000012-auto.jpg>)")
        self.assertEqual(lines[3], "[00:00:40] 🔖 しおり")
        self.assertIn("しおり 1 個", md)
