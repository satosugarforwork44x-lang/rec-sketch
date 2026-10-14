"""recorder.py — 録音そのもの（別スレッド）。api.py から使う。

・soundcard（BSD-3-Clause）で Windows の WASAPI から録る。マイクだけ（1ch）か、マイク＋PC の音（2ch: 左=自分・右=相手）
・少しずつ WAV に書く（標準の wave）。wave は書くたびにヘッダーの長さを直すので、Hub が落ちても途中までは聞ける
・WASAPI は COM を使う。録るスレッドの中で COM を始める（Hub の画面のスレッドとは別）
  順番に注意: soundcard は読み込むときに自分で CoInitializeEx し、「もう始まっている（S_FALSE）」を
  エラー 0x100000001 として投げる。だから soundcard を先に読み込み、そのあとで com_thread() に入る
"""
from __future__ import annotations

import contextlib
import math
import threading
import time
import wave
from pathlib import Path

RATE = 48000
BLOCK = RATE // 10          # 1 回に読む長さ（0.1 秒）
BUFFER = RATE // 2          # WASAPI の受け皿（0.5 秒）。2 つの入口を順に読むので、待っている間にあふれない大きさにする
FLOOR_DB = -90.0


class RecorderError(Exception):
    pass


@contextlib.contextmanager
def com_thread():
    """このスレッドで COM を使えるようにする（Windows 以外では何もしない）。"""
    ok = False
    try:
        import ctypes
        ole32 = ctypes.windll.ole32          # type: ignore[attr-defined]
        ok = ole32.CoInitializeEx(None, 0) in (0, 1)   # S_OK / S_FALSE。0 = COINIT_MULTITHREADED
    except (ImportError, AttributeError, OSError):
        ole32 = None
    try:
        yield
    finally:
        if ok and ole32 is not None:
            ole32.CoUninitialize()


def load_backend():
    """soundcard と numpy。無ければ RecorderError（画面に入れ方を出す）。"""
    try:
        import numpy  # noqa: F401
        import soundcard
    except ImportError as e:
        raise RecorderError(f"録音には soundcard と numpy が必要です（{e.name} が入っていません）。"
                            "コマンドプロンプトで py -m pip install soundcard numpy を実行して、Hub を再起動してください") from None
    except Exception as e:  # noqa: BLE001 — Windows 以外・音の仕組みが無い環境など
        raise RecorderError(f"録音の仕組み（soundcard）を使えません: {type(e).__name__}: {e}") from None
    _patch_mix_format()
    return soundcard


# ---------- soundcard の補い ----------
# soundcard は機器の形式（GetMixFormat）が WAVEFORMATEXTENSIBLE・float32 であることを assert で確かめ、
# 違うと文の無い AssertionError で止まる（例: EMEET SmartCam C960 のマイク）。
# そのときだけ、こちらで float32 の形式を作って Windows に渡し、変換は Windows に任せる（AUTOCONVERTPCM）。
_IEEE_FLOAT = bytes.fromhex("0300000000001000800000aa00389b71")   # KSDATAFORMAT_SUBTYPE_IEEE_FLOAT（GUID のバイト列）


def _patch_mix_format() -> None:
    import sys
    mf = sys.modules.get("soundcard.mediafoundation")
    cls = getattr(mf, "_AudioClient", None) if mf else None
    if cls is None or getattr(cls, "_rec_sketch_patched", False):
        return
    original = cls.__init__

    def __init__(self, ptr, samplerate, channels, blocksize, isloopback, exclusive_mode=False):
        try:
            original(self, ptr, samplerate, channels, blocksize, isloopback, exclusive_mode)
        except AssertionError:
            _init_float32(mf, self, ptr, samplerate, channels, blocksize, isloopback, exclusive_mode)

    cls.__init__ = __init__
    cls._rec_sketch_patched = True


def _init_float32(mf, self, ptr, samplerate, channels, blocksize, isloopback, exclusive_mode) -> None:
    """soundcard の _AudioClient.__init__ と同じことを、形式だけ自分で作って行う。"""
    import struct
    self._ptr = ptr
    self.channelmap = list(range(channels)) if isinstance(channels, int) else list(channels)
    if blocksize is None:
        blocksize = self.deviceperiod[0] * samplerate
    n = len(set(self.channelmap))
    sr = int(samplerate)
    mask = 0x4 if n == 1 else (1 << n) - 1                       # 1ch は中央、それ以外は前から順に
    raw = struct.pack("<HHIIHHHHI16s", 0xFFFE, n, sr, sr * n * 4, n * 4, 32, 22, 32, mask, _IEEE_FLOAT)
    self._rec_sketch_fmt = mf._ffi.new("char[]", raw)            # Initialize の間、消えないように持っておく
    share = mf._ole32.AUDCLNT_SHAREMODE_EXCLUSIVE if exclusive_mode else mf._ole32.AUDCLNT_SHAREMODE_SHARED
    flags = 0x00100000 | 0x80000000 | 0x08000000 | 0x00080000    # 変換・混ぜ直し・良い変換・設定を残さない
    if isloopback:
        flags |= 0x00020000
    duration = int(blocksize / samplerate * 10_000_000)
    hr = self._ptr[0][0].lpVtbl.Initialize(self._ptr[0], share, flags, duration, 0,
                                           mf._ffi.cast("WAVEFORMATEXTENSIBLE *", self._rec_sketch_fmt), mf._ffi.NULL)
    mf._com.check_error(hr)
    self.samplerate = samplerate
    self._idle_start_time = None


def list_mics(backend=None) -> list[dict]:
    """マイクの一覧（ループバックは含めない）。COM のため別スレッドで調べる。"""
    out: list[dict] = []
    err: list[BaseException] = []

    def run():
        try:
            sc = backend or load_backend()     # 先に読み込む（下の com_thread より後だと 0x100000001 になる）
            with com_thread():
                default = sc.default_microphone()
                for m in sc.all_microphones(include_loopback=False):
                    out.append({"id": str(m.id), "name": m.name, "default": default is not None and m.id == default.id})
        except BaseException as e:  # noqa: BLE001
            err.append(e)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(10)
    if err:
        e = err[0]
        raise e if isinstance(e, RecorderError) else RecorderError(f"マイクを調べられません: {e}")
    return out


def describe(e: BaseException) -> str:
    """利用者に見せる 1 行。文が空のエラー（soundcard の assert など）でも、種類と止まった場所が分かるようにする。"""
    import traceback
    msg = str(e) or type(e).__name__
    tb = traceback.extract_tb(e.__traceback__)
    if tb:
        f = tb[-1]
        where = f"{Path(f.filename).name}:{f.lineno}"
        code = (f.line or "").strip()
        msg = f"{type(e).__name__}: {msg if str(e) else ''} [{where}] {code}".replace(":  [", " [")
    return msg[:400]


PRIVACY_HINT = ("。Windows の 設定 → プライバシーとセキュリティ → マイク で「マイクへのアクセス」と"
                "「デスクトップ アプリがマイクにアクセスできるようにする」が オン か確かめてください")


def privacy_hint(e: BaseException) -> str:
    """マイクが許可されていないと、Windows は形式を隠したり（soundcard の assert で止まる）、
    E_ACCESSDENIED（0x80070005）を返したりする。そのときは設定の場所を添える。"""
    return PRIVACY_HINT if isinstance(e, AssertionError) or "0x80070005" in str(e).lower() else ""


def _db(x) -> float:
    """ピークの dBFS。"""
    import numpy as np
    peak = float(np.max(np.abs(x))) if len(x) else 0.0
    return round(20 * math.log10(peak), 1) if peak > 1e-9 else FLOOR_DB


class Recorder:
    """1 回分の録音。start() で別スレッドが動き、stop() で止まる。"""

    def __init__(self, path: Path, source: str = "mic", mic_id: str | None = None, backend=None):
        if source not in ("mic", "both"):
            raise RecorderError("入力は mic か both")
        self.path = Path(path)
        self.source = source
        self.mic_id = mic_id
        self.channels = 2 if source == "both" else 1
        self.frames = 0
        self.levels = [FLOOR_DB] * self.channels
        self.error = ""
        self.started_at = time.time()
        self._backend = backend
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="rec-sketch", daemon=True)

    # ---------- 外から ----------
    def start(self, wait: float = 8.0) -> None:
        """録り始める。入口が開けなかったときはここで RecorderError（ファイルは残さない）。"""
        self._thread.start()
        self._ready.wait(wait)
        if self.error and not self.frames:
            self._thread.join(2)
            raise RecorderError(self.error)

    def stop(self, wait: float = 5.0) -> None:
        self._stop.set()
        self._thread.join(wait)

    @property
    def running(self) -> bool:
        return self._thread.is_alive()

    @property
    def seconds(self) -> float:
        return self.frames / RATE

    # ---------- 録るスレッド ----------
    def _devices(self, sc):
        mic = sc.get_microphone(self.mic_id) if self.mic_id else sc.default_microphone()
        if mic is None:
            raise RecorderError("マイクが見つかりません（Windows の設定 → サウンド → 入力 を確かめてください）")
        loop = None
        if self.source == "both":
            spk = sc.default_speaker()
            if spk is None:
                raise RecorderError("PC の音の出口（スピーカー・ヘッドホン）が見つかりません")
            loop = sc.get_microphone(str(spk.id), include_loopback=True)
        return mic, loop

    def _run(self) -> None:
        import numpy as np
        wf = None
        try:
            sc = self._backend or load_backend()
            with com_thread():
                mic, loop = self._devices(sc)
                with contextlib.ExitStack() as stack:
                    rm = stack.enter_context(mic.recorder(samplerate=RATE, channels=1, blocksize=BUFFER))
                    rl = stack.enter_context(loop.recorder(samplerate=RATE, channels=2, blocksize=BUFFER)) if loop else None
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    wf = wave.open(str(self.path), "wb")
                    wf.setnchannels(self.channels)
                    wf.setsampwidth(2)
                    wf.setframerate(RATE)
                    self._ready.set()
                    while not self._stop.is_set():
                        a = np.asarray(rm.record(numframes=BLOCK), dtype=np.float32).reshape(-1, 1)[:, :1]
                        if rl is not None:
                            b = np.asarray(rl.record(numframes=BLOCK), dtype=np.float32)
                            b = b.reshape(len(b), -1).mean(axis=1, keepdims=True)   # PC の音（左右）を 1 本に
                            n = min(len(a), len(b))
                            x = np.hstack([a[:n], b[:n]])
                        else:
                            x = a
                        self.levels = [_db(x[:, c]) for c in range(self.channels)]
                        wf.writeframes((np.clip(x, -1.0, 1.0) * 32767).astype("<i2").tobytes())
                        self.frames += len(x)
        except RecorderError as e:
            self.error = str(e)
        except Exception as e:  # noqa: BLE001 — 途中で抜かれた・使えなくなった、など。録れた所までは残す
            self.error = f"録音が止まりました: {describe(e)}{privacy_hint(e)}"
        finally:
            if wf is not None:
                try:
                    wf.close()
                except Exception:  # noqa: BLE001
                    pass
            if not self.frames:
                with contextlib.suppress(OSError):
                    self.path.unlink()
            self._ready.set()


def wav_info(path: Path) -> dict:
    """一覧用。長さ・チャンネル数（壊れていても落ちない）。"""
    try:
        with wave.open(str(path), "rb") as w:
            return {"seconds": round(w.getnframes() / (w.getframerate() or 1), 1), "channels": w.getnchannels()}
    except (wave.Error, EOFError, OSError):
        return {"seconds": None, "channels": None}
