"""screen.py — 録音中に画面の静止画を残す（自動の静止画としおり）。api.py から使う。

・mss（MIT）で選んだモニターを 0.5 秒ごとに取り、Pillow（HPND）で小さく縮めて比べる
・自動の静止画: 前に残した画面から「広い範囲」が変わり、そのあと 1.5 秒止まったときだけ JPEG で残す
  - 隅の小さな顔の映像などは「広い範囲」にならないので残らない
  - 動き続ける画面（動画・顔の一覧）は止まらないので残らない
  - 前に残したものとほぼ同じ（前のスライドに戻った）なら残さない。最短 10 秒あける
・しおり: すぐ取る（止まるのを待たない）。PNG
・時刻は録音の頭からの秒（clock() で渡す。音と同じ時計にするため、録音した長さを使う）
・残した物は <録音名>\\shots.json に並べる（文字起こしがこれを読んで .md に差し込む）
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path

INTERVAL = 0.5          # 何秒ごとに画面を見るか
STABLE_SEC = 1.5        # この間ほぼ動かなければ「止まった」
MIN_GAP = 10.0          # 自動の静止画の最短の間隔
SMALL = (64, 48)        # 比べるための大きさ（8×6 の区画 × 8×8 画素）
GRID = (8, 6)
TILE_DIFF = 10.0        # 区画の明るさの差（0〜255）がこれより大きければ「変わった」
WIDE = 0.2              # 変わった区画の割合がこれ以上で「広い範囲が変わった」
STILL_DIFF = 3.0        # 前の回との差がこれより小さい区画は「動いていない」
STILL_MOVING = 0.1      # 動いている区画がこの割合以下なら「止まった」（48 区画のうち 4 つまで）
DUP_DIFF = 4.0          # 残した物との平均の差がこれより小さければ「同じ画面」
RECENT = 30             # 重複を調べる、最近残した物の数


class ScreenError(Exception):
    pass


def load_backend():
    try:
        import mss
        from PIL import Image  # noqa: F401
    except ImportError as e:
        raise ScreenError(f"画面を残すには mss と Pillow が必要です（{e.name} が入っていません）。"
                          "py -m pip install mss pillow を実行して、Hub を再起動してください") from None
    return mss


def available() -> bool:
    try:
        load_backend()
        return True
    except ScreenError:
        return False


def _open(mss_mod):
    return (getattr(mss_mod, "MSS", None) or mss_mod.mss)()


def monitors(mss_mod=None) -> list[dict]:
    """モニターの一覧（1 から）。"""
    m = mss_mod or load_backend()
    with _open(m) as s:
        return [{"index": i, "width": d["width"], "height": d["height"], "left": d["left"], "top": d["top"]}
                for i, d in enumerate(s.monitors) if i > 0]


# ---------- 比べる（numpy なしで、Pillow の小さな画像だけで） ----------
def small_of(img) -> list[float]:
    """画面を 64×48 の灰色にして、8×6 の区画ごとの平均の明るさ（48 個）にする。"""
    g = img.convert("L").resize(SMALL)
    px = list(g.getdata())
    w, h = SMALL
    cw, ch = w // GRID[0], h // GRID[1]
    out = []
    for gy in range(GRID[1]):
        for gx in range(GRID[0]):
            s = 0
            for y in range(gy * ch, (gy + 1) * ch):
                row = y * w
                s += sum(px[row + gx * cw: row + (gx + 1) * cw])
            out.append(s / (cw * ch))
    return out


def tile_diffs(a: list[float], b: list[float]) -> list[float]:
    return [abs(x - y) for x, y in zip(a, b)]


def wide_change(a: list[float] | None, b: list[float]) -> float:
    """変わった区画の割合（0〜1）。前が無ければ 1。"""
    if a is None:
        return 1.0
    d = tile_diffs(a, b)
    return sum(1 for x in d if x > TILE_DIFF) / len(d)


def is_still(a: list[float] | None, b: list[float]) -> bool:
    """ほとんどの区画が止まっていれば「止まった」。隅の顔の映像のような小さな動きは無視する。"""
    if a is None:
        return False
    d = tile_diffs(a, b)
    return sum(1 for x in d if x >= STILL_DIFF) / len(d) <= STILL_MOVING


def is_dup(recent: list[list[float]], b: list[float]) -> bool:
    return any(sum(tile_diffs(r, b)) / len(b) < DUP_DIFF for r in recent)


def stamp(sec: float) -> str:
    s = int(sec)
    return f"{s // 3600:02d}{s // 60 % 60:02d}{s % 60:02d}"


class Decider:
    """自動の静止画を残すかどうかを決める（画面を取る部分と分けて、確かめやすくする）。"""

    def __init__(self):
        self.prev: list[float] | None = None
        self.still_since: float | None = None
        self.saved: list[float] | None = None
        self.recent: list[list[float]] = []
        self.last_auto = -1e9

    def feed(self, small: list[float], t: float) -> bool:
        """t は録音の頭からの秒。残すべきなら True。"""
        if is_still(self.prev, small):
            if self.still_since is None:
                self.still_since = t
        else:
            self.still_since = None
        self.prev = small
        if self.still_since is None or t - self.still_since < STABLE_SEC - 1e-6:
            return False
        if t - self.last_auto < MIN_GAP:
            return False
        if wide_change(self.saved, small) < WIDE or is_dup(self.recent, small):
            return False
        self.remember(small)
        self.last_auto = t
        return True

    def remember(self, small: list[float]) -> None:
        self.saved = small
        self.recent = (self.recent + [small])[-RECENT:]


class Watcher:
    """録音中に動く。start() / stop()、mark() でしおり。"""

    def __init__(self, folder: Path, clock, monitor: int = 1, mss_mod=None, auto=True, interval: float = INTERVAL):
        self.folder = Path(folder)
        self.clock = clock                    # () -> 録音の頭からの秒
        self.monitor = int(monitor)
        self.auto = auto
        self.interval = interval
        self.shots: list[dict] = []
        self.error = ""
        self._mss = mss_mod
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._marks: list[float] = []
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name="rec-sketch-screen", daemon=True)

    def start(self) -> None:
        self._mss = self._mss or load_backend()
        self.folder.mkdir(parents=True, exist_ok=True)
        self._thread.start()

    def stop(self, wait: float = 3.0) -> None:
        self._stop.set()
        self._wake.set()
        self._thread.join(wait)

    @property
    def running(self) -> bool:
        return self._thread.is_alive()

    def mark(self) -> float:
        """しおり。押した時刻を覚えて、画面を取るスレッドをすぐ起こす（mss はスレッドをまたいで使えないため）。"""
        t = self.clock()
        with self._lock:
            self._marks.append(t)
        self._wake.set()
        return t

    def counts(self) -> dict:
        return {"auto": sum(1 for s in self.shots if s["kind"] == "auto"),
                "mark": sum(1 for s in self.shots if s["kind"] == "mark")}

    # ---------- 画面を取るスレッド ----------
    def _grab(self, s):
        from PIL import Image
        mons = s.monitors
        mon = mons[self.monitor] if 0 < self.monitor < len(mons) else mons[1]
        shot = s.grab(mon)
        return Image.frombytes("RGB", shot.size, shot.rgb)

    def _save(self, img, t: float, kind: str) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        ext = "png" if kind == "mark" else "jpg"
        name = f"t{stamp(t)}-{kind}.{ext}"
        p = self.folder / name
        n = 2
        while p.exists():
            p = self.folder / f"t{stamp(t)}-{kind}-{n}.{ext}"
            n += 1
        tmp = p.with_name(p.name + ".tmp")
        img.save(tmp, format="PNG" if ext == "png" else "JPEG", **({} if ext == "png" else {"quality": 88}))
        os.replace(tmp, p)
        self.shots.append({"t": round(t, 2), "kind": kind, "file": p.name})
        self._write_index()

    def _write_index(self) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.folder, prefix=".shots-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"format": "rec-shots/1", "shots": self.shots}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.folder / "shots.json")

    def _run(self) -> None:
        dec = Decider()
        try:
            with _open(self._mss) as s:
                while not self._stop.is_set():
                    with self._lock:
                        marks, self._marks = self._marks, []
                    img = self._grab(s)
                    small = small_of(img)
                    for t in marks:                       # しおりは待たずに残す（自動の重複にもしない）
                        self._save(img, t, "mark")
                        dec.remember(small)
                    auto = self.auto() if callable(self.auto) else self.auto   # 振り返り窓が開いている間は止めることがある
                    if not marks and auto and dec.feed(small, self.clock()):
                        self._save(img, self.clock(), "auto")
                    self._wake.wait(self.interval)
                    self._wake.clear()
                with self._lock:                          # 止める直前に押されたしおり
                    marks, self._marks = self._marks, []
                if marks:
                    img = self._grab(s)
                    for t in marks:
                        self._save(img, t, "mark")
        except Exception as e:  # noqa: BLE001 — 録音は止めない。理由を画面に出す
            self.error = f"画面を残せなくなりました: {type(e).__name__}: {e}"


def read_index(folder: Path) -> list[dict]:
    """<録音名>\\shots.json を読む（無ければ空）。"""
    p = Path(folder) / "shots.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return [s for s in data.get("shots", []) if isinstance(s, dict) and "t" in s and "file" in s]
    except (OSError, ValueError, AttributeError):    # 無い・壊れた・形の違う shots.json
        return []
