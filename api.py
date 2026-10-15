"""rec/api.py — レックスケッチ。マイク（と PC の音）を録って WAV に残し、PC の中で文字にする。

・保存先はフォルダ窓の置き場所の下の「Rec Sketch」フォルダ（設定 modules.rec.folder で変えられる）
  録音 <日時> <名前>.wav と、文字起こし <同じ名前>.md。Claude は今ある folders_read_text で .md を読んで議事録にまとめる
  （音声を Claude に送る道具は作らない）
・録音は Python 側の別スレッド（recorder.py）。窓を閉じても止まらない。マイク＋PC の音は 2ch（左=自分・右=相手）
・文字起こしは別プロセス（transcribe.py）。faster-whisper が入っている Python を自動で探す（Hub と同じ Python でなくてよい）
  見つからなければ文字起こしのボタンを出さないだけで、録音は使える
・人に送るときは「共有用に書き出す」で M4A（AAC 64kbps・モノラル）にする（share.py、別プロセス、PyAV）
  録音そのものは WAV のまま（少しずつ書けて途中で落ちても聞ける・文字起こしにそのまま使える）
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

_ctx = None

FOLDER_NAME = "Rec Sketch"
GUIDE = "REC_FORMAT.md"
MODELS = ("small", "medium", "large-v3-turbo")
LANGS = ("ja", "en", "auto")
NAME_BAD = re.compile(r"[\\/:*?\"<>|\x00-\x1f]")
NO_WINDOW = 0x08000000 if os.name == "nt" else 0      # 黒い画面を出さない


class RecError(Exception):
    pass


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"hubmod_rec_{name}", Path(__file__).with_name(f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_recmod = _load("recorder")
TRANSCRIBE = Path(__file__).with_name("transcribe.py")
SHARE = Path(__file__).with_name("share.py")

_rec = None                 # 今の録音（recorder.Recorder）
_last_error = ""            # 最後の録音が途中で止まった理由
_job: dict = {}             # 今の文字起こし
_proc: subprocess.Popen | None = None
_share: dict = {}           # 今の共有用の書き出し
_share_proc: subprocess.Popen | None = None
_find: dict = {"state": "idle"}
_lock = threading.Lock()


def setup(ctx):
    global _ctx
    _ctx = ctx


# ---------- 保存先 ----------
def _folder() -> Path:
    p = (_ctx.config or {}).get("folder")
    if not p:
        root = ((getattr(_ctx.host, "config", None) or {}).get("modules") or {}).get("folders", {}).get("root") or "~/Hub"
        p = Path(root).expanduser() / FOLDER_NAME
    f = Path(p).expanduser()
    f.mkdir(parents=True, exist_ok=True)
    guide, ours = f / GUIDE, Path(__file__).with_name(GUIDE)
    try:  # Claude が読む説明書。同梱の版と違えば置き直す
        if ours.is_file() and (not guide.is_file() or guide.read_bytes() != ours.read_bytes()):
            _write_atomic(guide, ours.read_text(encoding="utf-8"))
    except OSError:
        pass
    return f


def _write_atomic(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".rec-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def _unique(path: Path) -> Path:
    """上書きしない: 'name.wav' があれば 'name (2).wav'。"""
    if not path.exists():
        return path
    stem, suf = path.name[: -len(path.suffix)], path.suffix
    for i in range(2, 1000):
        p = path.with_name(f"{stem} ({i}){suf}")
        if not p.exists():
            return p
    raise RecError("同じ名前のファイルが多すぎます")


def _wav(file: str) -> Path:
    """画面から来た名前を、保存先の中の .wav に限る。"""
    name = Path(str(file)).name
    if name != str(file) or not name.lower().endswith(".wav"):
        raise RecError(f"録音の名前が違います: {file}")
    p = _folder() / name
    if not p.is_file():
        raise RecError(f"録音が見つかりません: {name}")
    return p


def _prefs() -> dict:
    p = _ctx.store.get("rec", "prefs", {}) or {}
    return {"source": p.get("source") if p.get("source") in ("mic", "both") else "both",
            "mic": p.get("mic") or "", "model": p.get("model") if p.get("model") in MODELS else "small",
            "lang": p.get("lang") if p.get("lang") in LANGS else "ja"}


def set_prefs(source: str | None = None, mic: str | None = None, model: str | None = None, lang: str | None = None) -> dict:
    p = _prefs()
    if source in ("mic", "both"):
        p["source"] = source
    if mic is not None:
        p["mic"] = str(mic)[:300]
    if model in MODELS:
        p["model"] = model
    if lang in LANGS:
        p["lang"] = lang
    _ctx.store.set("rec", "prefs", p)
    return p


def consent() -> bool:
    """初回の「録る前に参加者の同意を」を読んだ印。"""
    _ctx.store.set("rec", "consent", True)
    return True


# ---------- 一覧 ----------
def _files() -> list[dict]:
    f = _folder()
    cur = _rec.path.name if _rec and _rec.running else None
    out = []
    for p in sorted(f.glob("*.wav"), key=lambda x: x.stat().st_mtime, reverse=True):
        if p.name == cur:
            continue
        md, m4a = p.with_suffix(".md"), p.with_suffix(".m4a")
        out.append({"file": p.name, "name": p.stem, "mtime": p.stat().st_mtime, "bytes": p.stat().st_size,
                    **_recmod.wav_info(p), "text": md.name if md.is_file() else None,
                    "textPath": str(md) if md.is_file() else None,
                    "m4a": m4a.name if m4a.is_file() else None, "m4aPath": str(m4a) if m4a.is_file() else None})
    return out[:300]


def state() -> dict:
    _whisper_autofind()
    return {"folder": str(_folder()), "files": _files(), "prefs": _prefs(),
            "consent": bool(_ctx.store.get("rec", "consent", False)), **live()}


def live() -> dict:
    """録音中に画面が短い間隔で読む分だけ。"""
    rec = None
    if _rec is not None and (_rec.running or _rec.error):
        rec = {"running": _rec.running, "file": _rec.path.name, "source": _rec.source, "seconds": round(_rec.seconds, 1),
               "levels": _rec.levels, "started": _rec.started_at, "error": _rec.error}
    return {"rec": rec, "lastError": _last_error, "job": dict(_job) if _job else None, "whisper": _whisper_state(),
            "share": dict(_share) if _share else None, "canShare": _share_python() is not None}


def mics() -> list[dict]:
    try:
        return _recmod.list_mics()
    except _recmod.RecorderError as e:
        raise RecError(str(e)) from None


# ---------- 録る ----------
def start(source: str = "both", mic: str = "", title: str = "") -> dict:
    global _rec, _last_error
    with _lock:
        if _rec is not None and _rec.running:
            raise RecError("もう録音しています")
        title = NAME_BAD.sub("_", str(title or "")).strip()[:60] or "録音"
        path = _unique(_folder() / f"{time.strftime('%Y-%m-%d %H%M')} {title}.wav")
        set_prefs(source=source, mic=mic)
        r = _recmod.Recorder(path, source=source, mic_id=mic or None)
        try:
            r.start()
        except _recmod.RecorderError as e:
            raise RecError(str(e)) from None
        _rec, _last_error = r, ""
    return live()


def stop() -> dict:
    global _rec, _last_error
    with _lock:
        r = _rec
        if r is None:
            return state()
        r.stop()
        _last_error = r.error
        _rec = None
    return {**state(), "saved": r.path.name if r.path.is_file() else None}


def status() -> dict | None:
    """バー用: 録音中は「REC 12:34」、文字起こし中は「TXT 42%」。"""
    if _rec is not None and _rec.running:
        s = int(_rec.seconds)
        t = f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"
        return {"icon": "record", "metric": True, "items": [["REC", t]]}
    if _job.get("state") == "running":
        return {"icon": "transcript", "metric": True, "items": [["TXT", f"{int(_job.get('progress', 0) * 100)}%"]]}
    return None


def blob_path(key: str) -> Path:
    """一覧で聞くための WAV（/blob/rec/<ファイル名>）。保存先の .wav だけ。"""
    return _wav(key)


# ---------- faster-whisper の入った Python を探す ----------
def _candidates() -> list[str]:
    c: list[str] = []
    conf = (_ctx.config or {}).get("python")
    if conf:
        c.append(str(conf))
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").is_file():
        exe = exe.with_name("python.exe")
    c.append(str(exe))
    if os.name == "nt" and shutil.which("py"):
        try:  # py ランチャーが知っている Python（新しい順）
            out = subprocess.run(["py", "-0p"], capture_output=True, text=True, timeout=15, creationflags=NO_WINDOW).stdout
            c += [m.group(1).strip() for m in re.finditer(r"(\S.*?python\.exe)\s*$", out, re.M | re.I)]
        except (OSError, subprocess.SubprocessError):
            pass
    seen, uniq = set(), []
    for x in c:
        k = os.path.normcase(os.path.abspath(x)) if x else ""
        if k and k not in seen and Path(x).is_file():
            seen.add(k)
            uniq.append(x)
    return uniq


def _check(exe: str) -> dict | None:
    try:
        r = subprocess.run([exe, str(TRANSCRIBE), "--check"], capture_output=True, text=True, timeout=180,
                           creationflags=NO_WINDOW, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        info = json.loads((r.stdout or "").strip().splitlines()[-1])
        return info if info.get("ok") else None
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def _find_run() -> None:
    try:
        for exe in _candidates():
            info = _check(exe)
            if info:
                _ctx.store.set("rec", "whisper", {"python": exe, "pyver": info.get("python", ""), "version": info.get("version", "")})
                return
        _ctx.store.set("rec", "whisper", None)
    finally:
        _find["state"] = "done"


def whisper_find() -> dict:
    """探し直す（入れた直後など）。"""
    if _find.get("state") != "running":
        _find["state"] = "running"
        threading.Thread(target=_find_run, daemon=True).start()
    return _whisper_state()


def _whisper_autofind() -> None:
    """まだ一度も探していなければ、裏で探す。前に見つけた Python が消えていたら探し直す。"""
    w = _ctx.store.get("rec", "whisper", "unset")
    if _find.get("state") == "idle" and (w == "unset" or (isinstance(w, dict) and not Path(w.get("python", "")).is_file())):
        whisper_find()


def _whisper_state() -> dict:
    w = _ctx.store.get("rec", "whisper", None)
    if _find.get("state") == "running":
        return {"state": "checking"}
    if isinstance(w, dict) and w.get("python"):
        return {"state": "ok", **w}
    return {"state": "none"}


# ---------- 文字起こし ----------
def transcribe(file: str, model: str | None = None, lang: str | None = None) -> dict:
    global _job, _proc
    w = _whisper_state()
    if w["state"] != "ok":
        raise RecError("faster-whisper が見つかりません。py -m pip install faster-whisper で入れてから「探し直す」を押してください")
    wav = _wav(file)
    p = set_prefs(model=model, lang=lang)
    with _lock:
        if _job.get("state") == "running":
            raise RecError("ほかの録音を文字にしています。終わってからもう一度押してください")
        out = _unique(wav.with_suffix(".md"))
        _job = {"state": "running", "file": wav.name, "out": out.name, "stage": "start", "progress": 0.0,
                "model": p["model"], "error": "", "started": time.time()}
        args = [w["python"], str(TRANSCRIBE), str(wav), str(out), "--model", p["model"], "--lang", p["lang"], "--title", wav.stem]
        try:
            _proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                     errors="replace", creationflags=NO_WINDOW, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        except OSError as e:
            _job.update(state="error", error=f"起動できません: {e}")
            raise RecError(_job["error"]) from None
        threading.Thread(target=_watch, args=(_proc, _job), daemon=True).start()
    return dict(_job)


def _watch(pr: subprocess.Popen, job: dict) -> None:
    tail: list[str] = []
    err_lines: list[str] = []
    threading.Thread(target=lambda: err_lines.extend(pr.stderr or []), daemon=True).start()
    for line in pr.stdout or []:
        try:
            m = json.loads(line)
        except ValueError:
            tail = (tail + [line.strip()])[-4:]
            continue
        t = m.get("type")
        if t == "stage":
            job["stage"] = m.get("stage")
            if m.get("who"):
                job["who"] = m["who"]
        elif t == "progress":
            job["progress"] = float(m.get("p", 0))
        elif t == "done":
            job.update(state="done", progress=1.0, out=Path(m.get("out", job["out"])).name)
        elif t == "error":
            job.update(state="error", error=m.get("error", ""))
    code = pr.wait()
    for f in (pr.stdout, pr.stderr):
        if f:
            f.close()
    if job.get("state") == "running":
        if job.get("cancelled"):
            job.update(state="cancelled")
        else:
            msg = (job.get("error") or "\n".join([*tail, *[x.strip() for x in err_lines[-3:]]]).strip())
            job.update(state="error", error=msg or f"文字起こしが止まりました（{code}）")


def transcribe_cancel() -> dict | None:
    if _job.get("state") == "running" and _proc is not None:
        _job["cancelled"] = True
        _proc.kill()
    return dict(_job) if _job else None


# ---------- 共有用に書き出す（M4A） ----------
def _share_python() -> str | None:
    """PyAV の入った Python。Hub と同じ Python に入っていればそれを、無ければ faster-whisper の入った Python（PyAV も入っている）。"""
    if importlib.util.find_spec("av") is not None:
        exe = Path(sys.executable)
        if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").is_file():
            exe = exe.with_name("python.exe")
        return str(exe)
    w = _whisper_state()
    return w.get("python") if w.get("state") == "ok" else None


def share(file: str) -> dict:
    """録音を共有用の M4A（AAC 64kbps・モノラル）にする。同じフォルダに <名前>.m4a（上書きしない）。"""
    global _share, _share_proc
    py = _share_python()
    if not py:
        raise RecError("共有用の書き出しには PyAV が必要です。py -m pip install av で入れて、Hub を再起動してください"
                       "（faster-whisper を入れると一緒に入ります）")
    wav = _wav(file)
    with _lock:
        if _share.get("state") == "running":
            raise RecError("ほかの録音を書き出しています。終わってからもう一度押してください")
        out = _unique(wav.with_suffix(".m4a"))
        _share = {"state": "running", "file": wav.name, "out": out.name, "stage": "share", "progress": 0.0, "error": "",
                  "started": time.time()}
        try:
            _share_proc = subprocess.Popen([py, str(SHARE), str(wav), str(out)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW,
                                           env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        except OSError as e:
            _share.update(state="error", error=f"起動できません: {e}")
            raise RecError(_share["error"]) from None
        threading.Thread(target=_watch, args=(_share_proc, _share), daemon=True).start()
    return dict(_share)


def share_path(file: str) -> str:
    """書き出した M4A の場所（エクスプローラーで見せる用）。"""
    m = _wav(file).with_suffix(".m4a")
    if not m.is_file():
        raise RecError("共有用のファイルがまだありません")
    return str(m)
