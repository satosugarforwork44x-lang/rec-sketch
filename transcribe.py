"""transcribe.py — 録音を文字にする。Hub とは別のプロセスで動かす（api.py が起動する）。

    python transcribe.py --check
    python transcribe.py <録音.wav> <出力.md> [--model small] [--lang ja] [--title 名前]

・faster-whisper（MIT。中の CTranslate2 も MIT、Whisper のモデルも MIT）を使う。モデルは初回に取得してキャッシュされる
・Hub と別のプロセスにする理由: faster-whisper が入る Python を選べる（Hub が 3.15 でも 3.14 で起こせる）、
  大きなモデルのメモリが Hub に残らない、落ちても Hub は止まらない
・2ch の録音（左=自分・右=相手）はチャンネルごとに起こして、時刻順に並べ「自分／相手」を付ける
・進み具合は 1 行 1 つの JSON で標準出力に出す: {"type": "stage"|"progress"|"done"|"error", ...}
・Python 3.10 以上・標準ライブラリ＋faster-whisper（と、それが使う numpy・av）だけ
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
import tempfile
import time
import wave
from pathlib import Path

MODELS = ("small", "medium", "large-v3-turbo")
LABELS = ("自分", "相手")
SILENT_RMS = 1e-4           # これより静かなチャンネルは起こさない（PC の音が無かった録音など）
MERGE_GAP = 2.0             # 同じ人の発言が 2 秒以内に続くときは 1 段落にまとめる


def emit(**kw) -> None:
    print(json.dumps(kw), flush=True)   # ASCII だけにする（Windows のパイプの文字コードに左右されない）


def stamp(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600:02d}:{sec // 60 % 60:02d}:{sec % 60:02d}"


# ---------- 並べる（faster-whisper なしで確かめられる部分） ----------
def drop_echo(mine: list[dict], theirs: list[dict], overlap: float = 0.5, similar: float = 0.6) -> list[dict]:
    """スピーカーから出た相手の声がマイクにも入ったもの（同じ時刻・ほぼ同じ文）を「自分」から外す。"""
    out = []
    for m in mine:
        dup = False
        for t in theirs:
            ov = min(m["end"], t["end"]) - max(m["start"], t["start"])
            if ov <= 0:
                continue
            short = max(0.01, min(m["end"] - m["start"], t["end"] - t["start"]))
            if ov / short >= overlap and difflib.SequenceMatcher(None, m["text"], t["text"]).ratio() >= similar:
                dup = True
                break
        if not dup:
            out.append(m)
    return out


def merge(segs: list[dict], gap: float = MERGE_GAP) -> list[dict]:
    """時刻順に並べ、同じ人の続く発言をまとめる。"""
    out: list[dict] = []
    for s in sorted(segs, key=lambda x: (x["start"], x.get("who") or "")):
        text = s["text"].strip()
        if not text:
            continue
        last = out[-1] if out else None
        if last and last.get("who") == s.get("who") and s["start"] - last["end"] <= gap:
            sep = "" if _cjk(last["text"][-1:]) and _cjk(text[:1]) else " "
            last["text"] += sep + text
            last["end"] = max(last["end"], s["end"])
        else:
            out.append({"start": s["start"], "end": s["end"], "who": s.get("who"), "text": text})
    return out


def _cjk(ch: str) -> bool:
    return bool(ch) and ord(ch) >= 0x3000


def render_md(title: str, wav_name: str, seconds: float, two: bool, model: str, lang: str, items: list[dict]) -> str:
    head = [f"# {title}", "",
            f"- 録音: {wav_name}（{stamp(seconds)}、{'マイク＋PC の音' if two else 'マイク'}）",
            f"- 文字起こし: faster-whisper {model}（{lang or '自動'}）、{time.strftime('%Y-%m-%d %H:%M')}"]
    if two:
        head.append("- 話者: 自分＝マイク／相手＝PC の音（声で聞き分けたのではなく、録った入口で分けています）")
    head += ["- 自動の文字起こしです。固有名詞・数字は録音で確かめてください", "", "---", ""]
    body = [f"[{stamp(i['start'])}] " + (f"{i['who']}: " if i.get("who") else "") + i["text"] for i in items]
    return "\n".join(head + (body or ["（文字にできる声がありませんでした）"])) + "\n"


def write_atomic(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".rec-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


# ---------- 音を読む（48 kHz の WAV → 16 kHz、チャンネルごと） ----------
def load_16k(path: Path, taps: int = 63) -> list:
    """WAV を少しずつ読み、16 kHz・float32 のチャンネルごとの配列にする（whisper はこの形を受け取る）。
    48 kHz などの 16 kHz の整数倍は、低域だけ通すフィルター（窓付き sinc）をかけてから間引く。
    faster-whisper の decode_audio（PyAV）は版の組み合わせで動かないことがあるので、WAV は自分で読む。"""
    import numpy as np
    from numpy.lib.stride_tricks import sliding_window_view

    with wave.open(str(path), "rb") as w:
        ch, sr, sw = w.getnchannels(), w.getframerate(), w.getsampwidth()
        if sw != 2:
            raise ValueError("16 bit の WAV だけ読めます")
        out: list[list] = [[] for _ in range(ch)]
        factor = sr // 16000 if sr % 16000 == 0 else 0
        if factor:
            n = np.arange(taps) - (taps - 1) / 2
            h = np.sinc(2 * 0.45 / factor * n) * np.hamming(taps)
            h = (h / h.sum()).astype(np.float32)[::-1]
            tail = np.zeros((taps - 1, ch), np.float32)
            phase = 0
        while True:
            b = w.readframes(sr * 30)
            if not b:
                break
            x = np.frombuffer(b, "<i2").reshape(-1, ch).astype(np.float32) / 32768
            if factor:
                x = np.vstack([tail, x])
                nwin = len(x) - taps + 1
                for c in range(ch):
                    out[c].append(sliding_window_view(x[:, c], taps)[phase::factor] @ h)
                phase = (phase - nwin) % factor
                tail = x[-(taps - 1):]
            else:   # 16 kHz の整数倍でないとき（ほかのアプリの WAV など）は直線で補う
                t_new = np.arange(0, len(x) * 16000 / sr) * sr / 16000
                for c in range(ch):
                    out[c].append(np.interp(t_new, np.arange(len(x)), x[:, c]).astype(np.float32))
    return [np.concatenate(o).astype(np.float32) if o else np.zeros(0, np.float32) for o in out]


# ---------- 起こす ----------
def check() -> int:
    try:
        import faster_whisper
        emit(type="check", ok=True, python=sys.version.split()[0], version=getattr(faster_whisper, "__version__", ""))
        return 0
    except Exception as e:  # noqa: BLE001
        emit(type="check", ok=False, python=sys.version.split()[0], error=str(e))
        return 1


def run(wav: Path, out: Path, model_name: str, lang: str, title: str) -> int:
    import numpy as np
    from faster_whisper import WhisperModel

    with wave.open(str(wav), "rb") as w:
        seconds = w.getnframes() / (w.getframerate() or 1)
    emit(type="stage", stage="audio")
    chans = load_16k(wav)
    two = len(chans) == 2
    if len(chans) > 2:   # 3ch 以上（ほかのアプリの WAV）は 1 本に混ぜる
        chans = [np.mean(chans, axis=0).astype(np.float32)]
    emit(type="stage", stage="model", model=model_name)
    try:
        model = WhisperModel(model_name, device="cpu", compute_type="int8")
    except Exception as e:  # noqa: BLE001 — 初回の取得に失敗したとき（ネットにつながっていない、など）
        raise RuntimeError(f"モデル {model_name} を読み込めません（初回はネットから取得します。つながっているか確かめてください）: {e}") from None
    labels = LABELS if two else (None,)
    found: list[list[dict]] = []
    for ci, (audio, who) in enumerate(zip(chans, labels)):
        segs: list[dict] = []
        if len(audio) and float(np.sqrt(np.mean(np.square(audio[:: 16])))) >= SILENT_RMS:
            emit(type="stage", stage="text", who=who, part=ci + 1, parts=len(chans))
            dur = max(1e-6, len(audio) / 16000)
            it, _info = model.transcribe(audio, language=lang or None, vad_filter=True, beam_size=5,
                                         condition_on_previous_text=False)
            for s in it:
                segs.append({"start": float(s.start), "end": float(s.end), "who": who, "text": s.text})
                emit(type="progress", p=round((ci + min(1.0, s.end / dur)) / len(chans), 3))
        found.append(segs)
    if two:
        found[0] = drop_echo(found[0], found[1])
    items = merge([s for segs in found for s in segs])
    write_atomic(out, render_md(title or wav.stem, wav.name, seconds, two, model_name, lang, items))
    emit(type="done", out=str(out), lines=len(items))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Rec Sketch: 録音を文字にする（faster-whisper）")
    ap.add_argument("--check", action="store_true", help="faster-whisper が使えるかだけ調べる")
    ap.add_argument("wav", nargs="?")
    ap.add_argument("out", nargs="?")
    ap.add_argument("--model", default="small", choices=MODELS)
    ap.add_argument("--lang", default="ja")
    ap.add_argument("--title", default="")
    a = ap.parse_args(argv)
    if a.check:
        return check()
    if not a.wav or not a.out:
        ap.error("録音と出力のファイルを指定してください")
    try:
        return run(Path(a.wav), Path(a.out), a.model, "" if a.lang == "auto" else a.lang, a.title)
    except Exception as e:  # noqa: BLE001 — 画面に理由を出す
        emit(type="error", error=str(e) if isinstance(e, RuntimeError) else f"{type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
