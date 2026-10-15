"""check_video.py — この PC で「画面の動画」に使える部品を確かめる（Rec Sketch の画面機能を作る前の下調べ）。

    py -3.14 tools\\check_video.py

・画面を取る部品（mss）と、静止画を作る部品（Pillow）が入っているか
・動画の圧縮器が実際に動くか（一覧にあっても、グラフィックの機能が無い PC では動かない）
  GPL の x264 は使わないので調べない。H.264 はグラフィックの機能（NVIDIA / Intel / AMD / Windows）、無ければ VP9
結果を貼ってもらえれば、既定の形式を決められる。何も書き換えない。
"""
from __future__ import annotations

import io
import sys
import time

CANDIDATES = [  # (圧縮器, 入れ物, 説明)
    ("h264_nvenc", "mp4", "H.264 / NVIDIA"),
    ("h264_qsv", "mp4", "H.264 / Intel"),
    ("h264_amf", "mp4", "H.264 / AMD"),
    ("h264_mf", "mp4", "H.264 / Windows (Media Foundation)"),
    ("libvpx-vp9", "webm", "VP9 (BSD)"),
]


def line(ok: bool, name: str, note: str) -> None:
    print(f"  {'OK' if ok else 'NG'}  {name:<12} {note}")


def main() -> int:
    print(f"Python {sys.version.split()[0]}")
    print("部品:")
    try:
        import mss
        with (getattr(mss, "MSS", None) or mss.mss)() as m:
            mons = m.monitors[1:]
            shot = m.grab(mons[0])
        line(True, "mss", f"{getattr(mss, '__version__', '')}  画面 {len(mons)} 枚、1 枚目 {shot.width}x{shot.height}")
    except ImportError:
        line(False, "mss", "入っていない → py -m pip install mss（MIT）")
    except Exception as e:  # noqa: BLE001
        line(False, "mss", f"画面を取れない: {type(e).__name__}: {e}")
    try:
        import PIL
        line(True, "Pillow", PIL.__version__)
    except Exception as e:  # noqa: BLE001
        line(False, "Pillow", f"{e}  → py -m pip install pillow（HPND）")
    try:
        import av
        import numpy as np
    except Exception as e:  # noqa: BLE001
        line(False, "av", f"{e}  → py -m pip install av")
        return 1
    line(True, "av", av.__version__)
    print("動画の圧縮器（1280x720 を 20 枚、10fps で試す）:")
    frame = np.zeros((720, 1280, 3), np.uint8)
    for codec, fmt, note in CANDIDATES:
        if codec not in av.codecs_available:
            line(False, codec, f"{note}  — この PyAV には入っていない")
            continue
        try:
            buf = io.BytesIO()
            t0 = time.perf_counter()
            with av.open(buf, "w", format=fmt) as out:
                st = out.add_stream(codec, rate=10)
                st.width, st.height, st.pix_fmt = 1280, 720, "yuv420p" if codec != "h264_qsv" else "nv12"
                for i in range(20):
                    frame[:, :, 0] = (i * 12) % 255
                    vf = av.VideoFrame.from_ndarray(frame, format="rgb24")
                    for p in st.encode(vf):
                        out.mux(p)
                for p in st.encode(None):
                    out.mux(p)
            ms = (time.perf_counter() - t0) * 1000
            line(True, codec, f"{note}  {ms:.0f} ms / 20 枚、{len(buf.getvalue()) // 1024} KB")
        except Exception as e:  # noqa: BLE001
            line(False, codec, f"{note}  — {type(e).__name__}: {str(e)[:80]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
