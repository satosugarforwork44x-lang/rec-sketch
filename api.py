"""rec/api.py — レックスケッチ。マイク（と PC の音）を録って WAV に残し、PC の中で文字にする。

・保存先はフォルダ窓の置き場所の下の「Rec Sketch」フォルダ（設定 modules.rec.folder で変えられる）
  録音 <日時> <名前>.wav と、文字起こし <同じ名前>.md。Claude は今ある folders_read_text で .md を読んで議事録にまとめる
  （音声を Claude に送る道具は作らない）
・録音は Python 側の別スレッド（recorder.py）。窓を閉じても止まらない。マイク＋PC の音は 2ch（左=自分・右=相手）
・文字起こしは別プロセス（transcribe.py）。faster-whisper が入っている Python を自動で探す（Hub と同じ Python でなくてよい）
  見つからなければ文字起こしのボタンを出さないだけで、録音は使える
・「画面も」を入れて録ると、画面の静止画を「<録音名>」フォルダに残す（screen.py。広く変わって止まったとき自動で・しおりのキーですぐ）
  しおりのキー（既定 F9）は録音中だけ Windows 全体に登録する（hotkey.py）。画面を入れていなくても、しおりは文字起こしに印として残る
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
_screen = _load("screen")
_hotkey = _load("hotkey")
TRANSCRIBE = Path(__file__).with_name("transcribe.py")
SHARE = Path(__file__).with_name("share.py")

_rec = None                 # 今の録音（recorder.Recorder）
_last_error = ""            # 最後の録音が途中で止まった理由
_watcher = None             # 今の録音の画面（screen.Watcher）。画面を入れていなければ None
_keys = None                # 今の録音の全体キー（hotkey.Hotkeys）
_marks: list[dict] = []     # 画面を入れていない録音のしおり（時刻だけ）
_key_errors: list[str] = []
_exclude = {"on": False, "supported": None}   # Hub の窓を画面の取り込みから外しているか（Hub 0.10 以上）
_viewer_seen = 0.0          # 振り返り窓が最後に「開いている」と知らせた時刻（閉じたのに知らせが来なくても、数秒で戻る）
VIEWER_TTL = 5.0
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
            "lang": p.get("lang") if p.get("lang") in LANGS else "ja",
            "screen": bool(p.get("screen", False)), "monitor": int(p.get("monitor") or 1),
            "markKey": p.get("markKey") or "F9", "clipKey": p.get("clipKey") or "F10"}


def set_prefs(source: str | None = None, mic: str | None = None, model: str | None = None, lang: str | None = None,
              screen: bool | None = None, monitor: int | None = None, markKey: str | None = None) -> dict:  # noqa: N803
    p = _prefs()
    if screen is not None:
        p["screen"] = bool(screen)
    if monitor is not None:
        try:
            p["monitor"] = max(1, min(16, int(monitor)))
        except (TypeError, ValueError):
            pass
    if markKey is not None:
        try:
            k = _hotkey.normalize(markKey)
        except _hotkey.HotkeyError as e:
            raise RecError(str(e)) from None
        if k == p.get("clipKey", "F10"):
            raise RecError(f"{k} は動画のキーと同じです")
        p["markKey"] = k
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


def screen_consent() -> bool:
    """「画面も」の初回のお知らせを読んだ印。"""
    _ctx.store.set("rec", "screenConsent", True)
    return True


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
        md, m4a, shots = p.with_suffix(".md"), p.with_suffix(".m4a"), _screen.read_index(p.with_suffix(""))
        out.append({"file": p.name, "name": p.stem, "mtime": p.stat().st_mtime, "bytes": p.stat().st_size,
                    **_recmod.wav_info(p), "text": md.name if md.is_file() else None,
                    "textPath": str(md) if md.is_file() else None,
                    "m4a": m4a.name if m4a.is_file() else None, "m4aPath": str(m4a) if m4a.is_file() else None,
                    "shots": sum(1 for s in shots if s.get("file")), "marks": sum(1 for s in shots if s.get("kind") == "mark"),
                    "shotsDir": str(p.with_suffix("")) if shots else None})
    return out[:300]


def state() -> dict:
    _whisper_autofind()
    return {"folder": str(_folder()), "files": _files(), "prefs": _prefs(),
            "consent": bool(_ctx.store.get("rec", "consent", False)),
            "screenConsent": bool(_ctx.store.get("rec", "screenConsent", False)),
            "canScreen": _screen.available(), "keyChoices": _hotkey.CHOICES, **live()}


def live() -> dict:
    """録音中に画面が短い間隔で読む分だけ。"""
    rec = None
    if _rec is not None and (_rec.running or _rec.error):
        rec = {"running": _rec.running, "file": _rec.path.name, "source": _rec.source, "seconds": round(_rec.seconds, 1),
               "levels": _rec.levels, "started": _rec.started_at, "error": _rec.error,
               "screen": ({"on": True, **_watcher.counts(), "error": _watcher.error} if _watcher is not None else {"on": False}),
               "marks": (_watcher.counts()["mark"] if _watcher is not None else len(_marks)),
               "markKey": _prefs()["markKey"], "keyErrors": list(_key_errors), "exclude": dict(_exclude),
               "autoPaused": _watcher is not None and not _auto_ok()}
    return {"rec": rec, "lastError": _last_error, "job": dict(_job) if _job else None, "whisper": _whisper_state(),
            "share": dict(_share) if _share else None, "canShare": _share_python() is not None}


def mics() -> list[dict]:
    try:
        return _recmod.list_mics()
    except _recmod.RecorderError as e:
        raise RecError(str(e)) from None


# ---------- 録る ----------
def monitors() -> list[dict]:
    try:
        return _screen.monitors()
    except _screen.ScreenError as e:
        raise RecError(str(e)) from None
    except Exception as e:  # noqa: BLE001
        raise RecError(f"画面を調べられません: {e}") from None


def start(source: str = "both", mic: str = "", title: str = "", screen: bool = False, monitor: int = 1) -> dict:
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
        set_prefs(screen=bool(screen), monitor=monitor)
        _start_extras(r, bool(screen), monitor)
    return live()


def _start_extras(r, screen: bool, monitor: int) -> None:
    """画面としおりのキー。うまくいかなくても録音は続ける（理由は live() の keyErrors・screen.error で画面に出す）。"""
    global _watcher, _keys, _marks, _key_errors
    _watcher, _marks, _key_errors = None, [], []
    if screen:
        w = _screen.Watcher(r.path.with_suffix(""), clock=lambda: r.seconds, monitor=monitor, auto=_auto_ok)
        try:
            w.start()
            _watcher = w
        except _screen.ScreenError as e:
            _key_errors.append(str(e))
    keys = _hotkey.Hotkeys({_prefs()["markKey"]: mark})
    _key_errors += keys.start()
    _keys = keys
    if _watcher is not None:
        _set_exclude(True)


def _set_exclude(on: bool) -> None:
    """Hub の窓（バー・パネル・振り返り窓）を画面の取り込みから外す / 戻す。Hub 0.10 の口が無ければ何もしない。"""
    fn = getattr(getattr(_ctx, "host", None), "set_capture_excluded", None)
    if fn is None:
        _exclude.update(on=False, supported=False)
        return
    try:
        r = fn(bool(on), owner="rec") or {}
        _exclude.update(on=bool(r.get("on", on)) and bool(r.get("supported", True)), supported=bool(r.get("supported", True)))
    except Exception:  # noqa: BLE001 — 外せなくても録音は続ける
        _exclude.update(on=False, supported=False)


def _auto_ok() -> bool:
    """自動の静止画を残してよいか。Hub の窓を外せないときは、振り返り窓が開いている間だけ止める（窓が写り込むため）。"""
    return _exclude["on"] or time.time() - _viewer_seen > VIEWER_TTL


def viewer_open(on: bool = True) -> dict:
    """振り返り窓が開いている（数秒ごとに知らせる）/ 閉じた。"""
    global _viewer_seen
    _viewer_seen = time.time() if on else 0.0
    v = _ctx.store.get("rec", "view", {}) or {}
    return {"exclude": dict(_exclude), "autoPaused": _watcher is not None and not _auto_ok(), "viewRev": v.get("rev", 0)}


def view(file: str | None = None) -> dict:
    """振り返り窓に出す録音を決める（一覧のボタンから）。None なら録音中・最新。開いている窓は viewRev が変わったのを見て読み直す。"""
    v = _ctx.store.get("rec", "view", {}) or {}
    v = {"file": Path(str(file)).name if file else None, "rev": int(v.get("rev", 0)) + 1}
    _ctx.store.set("rec", "view", v)
    return v


def mark() -> dict:
    """しおり（キーかボタン）。画面を入れていれば、その瞬間の静止画も残る。"""
    r = _rec
    if r is None or not r.running:
        raise RecError("録音していないときは、しおりは付けられません")
    if _watcher is not None:
        t = _watcher.mark()
    else:
        t = round(r.seconds, 2)
        _marks.append({"t": t, "kind": "mark", "file": None})
        _write_marks(r.path.with_suffix(""))
    return {"t": t}


def _write_marks(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    _write_atomic(folder / "shots.json", json.dumps({"format": "rec-shots/1", "shots": _marks}, ensure_ascii=False, indent=1))


def stop() -> dict:
    global _rec, _last_error
    with _lock:
        r = _rec
        if r is None:
            return state()
        global _watcher, _keys
        if _keys is not None:
            _keys.stop()
            _keys = None
        if _watcher is not None:
            _watcher.stop()
            _watcher = None
            _set_exclude(False)
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
    """/blob/rec/<key>: 一覧で聞く WAV（<ファイル名>.wav）、振り返り窓の静止画（s|<録音名>|<画像>）と小さな見本（th|…）。"""
    key = str(key)
    if key.startswith(("s|", "th|")):
        kind, stem, name = (key.split("|") + ["", ""])[:3]
        img = _shot_path(stem, name)
        return _thumb(img) if kind == "th" else img
    return _wav(key)


def _shot_path(stem: str, name: str) -> Path:
    """静止画は shots.json に載っている物だけ渡す（ほかのファイルは渡さない）。"""
    folder = _folder() / Path(stem).name
    if Path(name).name != name or not any(s.get("file") == name for s in _screen.read_index(folder)):
        raise RecError("その静止画はありません")
    p = folder / name
    if not p.is_file():
        raise RecError("その静止画はありません")
    return p


def _thumb(img: Path) -> Path:
    """振り返り窓の帯に出す小さな見本（長辺 320 の JPEG）。data_dir に作って覚えておく。"""
    from PIL import Image
    cache = Path(_ctx.data_dir) / "thumbs" / img.parent.name
    cache.mkdir(parents=True, exist_ok=True)
    out = cache / (img.stem + ".jpg")
    if not out.exists() or out.stat().st_mtime < img.stat().st_mtime:
        with Image.open(img) as im:
            im = im.convert("RGB")
            im.thumbnail((320, 320))
            tmp = out.with_suffix(".tmp")
            im.save(tmp, format="JPEG", quality=80)
            os.replace(tmp, out)
    return out


def shots(file: str | None = None) -> dict:
    """振り返り窓用: 録音の静止画の一覧。file を省くと、録音中ならその録音、無ければ静止画のある最新の録音。"""
    cur = _rec.path if _rec is not None and _rec.running else None
    picked = (_ctx.store.get("rec", "view", {}) or {}).get("file")
    if not file and picked and (_folder() / picked).is_file():
        file = picked
    if file:
        wav = _folder() / Path(str(file)).name
    elif cur is not None:
        wav = cur
    else:
        wav = next((_folder() / f["file"] for f in _files() if f.get("shots")), None)
    if wav is None:
        return {"file": None, "name": None, "shots": [], "live": False, "choices": [], "exclude": dict(_exclude)}
    items = [{**s, "key": f"{wav.stem}|{s['file']}"} for s in _screen.read_index(wav.with_suffix("")) if s.get("file")]
    choices = ([{"file": cur.name, "name": cur.stem, "live": True}] if cur is not None else []) + \
              [{"file": f["file"], "name": f["name"], "live": False} for f in _files() if f.get("shots")]
    return {"file": wav.name, "name": wav.stem, "dir": str(wav.with_suffix("")), "shots": items,
            "live": cur is not None and wav == cur, "choices": choices, "exclude": dict(_exclude),
            "autoPaused": _watcher is not None and not _auto_ok(),
            "viewRev": (_ctx.store.get("rec", "view", {}) or {}).get("rev", 0)}


def instance_title(key: str) -> str:
    """別窓のタイトル（タスクバーに出る名前）。"""
    return "振り返り — レックスケッチ" if key == "shots" else f"{key} — レックスケッチ"


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
