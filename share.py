"""share.py — 録音（WAV）を人に送るための M4A（AAC）にする。Hub とは別のプロセスで動かす（api.py が起動する）。

    python share.py --check
    python share.py <録音.wav> <出力.m4a> [--kbps 64]

・PyAV（BSD-3-Clause）の中の FFmpeg 自身の AAC 圧縮だけを使う（ホイールに入っている x264 などの GPL のライブラリは使わない）。faster-whisper と一緒に入るので、多くの場合は追加で入れる物がない
・2ch（左＝自分・右＝相手）は聞きやすいように 1 本（モノラル）に混ぜる。音が割れないように半分ずつ足す
・WAV は数秒ずつ読むので、長い録音でもメモリを使わない
・進み具合は 1 行 1 つの JSON で標準出力に出す（transcribe.py と同じ形）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import wave
from pathlib import Path


def emit(**kw) -> None:
    print(json.dumps(kw), flush=True)


def check() -> int:
    try:
        import av
        emit(type="check", ok=True, python=sys.version.split()[0], version=getattr(av, "__version__", ""))
        return 0
    except Exception as e:  # noqa: BLE001
        emit(type="check", ok=False, python=sys.version.split()[0], error=str(e))
        return 1


def to_m4a(src: Path, dst: Path, kbps: int = 64) -> None:
    import av
    import numpy as np

    fd, tmp = tempfile.mkstemp(dir=dst.parent, prefix=".rec-", suffix=".m4a")
    os.close(fd)
    try:
        with wave.open(str(src), "rb") as w:
            ch, sr, sw, total = w.getnchannels(), w.getframerate(), w.getsampwidth(), w.getnframes()
            if sw != 2:
                raise ValueError("16 bit の WAV だけ書き出せます")
            out = av.open(tmp, "w", format="ipod")       # ipod = .m4a の入れ物
            st = out.add_stream("aac", rate=sr)
            st.bit_rate = kbps * 1000
            try:  # 入れ物の既定はステレオ。声を送るだけなのでモノラルにする（大きさが半分）
                st.layout = "mono"
            except (AttributeError, TypeError, ValueError):
                st.codec_context.layout = "mono"
            done = 0
            emit(type="stage", stage="share")
            while True:
                b = w.readframes(sr * 5)
                if not b:
                    break
                x = np.frombuffer(b, "<i2").reshape(-1, ch)
                mono = x[:, 0] if ch == 1 else (x.astype(np.int32).sum(axis=1) // ch).astype("<i2")
                fr = av.AudioFrame.from_ndarray(np.ascontiguousarray(mono)[None, :], format="s16", layout="mono")
                fr.sample_rate = sr
                for p in st.encode(fr):
                    out.mux(p)
                done += len(x)
                emit(type="progress", p=round(min(0.99, done / max(1, total)), 3))
            for p in st.encode(None):
                out.mux(p)
            out.close()
        os.replace(tmp, dst)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Rec Sketch: 録音を共有用の M4A にする（PyAV）")
    ap.add_argument("--check", action="store_true", help="PyAV が使えるかだけ調べる")
    ap.add_argument("wav", nargs="?")
    ap.add_argument("out", nargs="?")
    ap.add_argument("--kbps", type=int, default=64)
    a = ap.parse_args(argv)
    if a.check:
        return check()
    if not a.wav or not a.out:
        ap.error("録音と出力のファイルを指定してください")
    try:
        to_m4a(Path(a.wav), Path(a.out), max(32, min(256, a.kbps)))
        emit(type="done", out=str(a.out))
        return 0
    except Exception as e:  # noqa: BLE001 — 画面に理由を出す
        emit(type="error", error=f"{type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
