"""hotkey.py — どの窓を見ていても効くキー（Windows の RegisterHotKey）。追加で入れる物は無い（ctypes だけ）。

・録音中だけ登録し、止めたら外す（普段の F9・F10 の使い方は邪魔しない）
・キーは "F9" "Ctrl+Shift+F9" "Pause" のような文字で持つ（設定で替えられる）
・ほかのアプリが同じキーを使っていて登録できないときは、HotkeyError（画面に出す）
"""
from __future__ import annotations

import os
import threading

MODS = {"alt": 0x0001, "ctrl": 0x0002, "shift": 0x0004, "win": 0x0008}
MOD_NOREPEAT = 0x4000
VK = {**{f"f{i}": 0x6F + i for i in range(1, 25)},           # F1 = 0x70 … F24 = 0x87
      "pause": 0x13, "scrolllock": 0x91, "insert": 0x2D, "home": 0x24, "end": 0x23,
      "pageup": 0x21, "pagedown": 0x22, "printscreen": 0x2C}
CHOICES = ["F9", "F10", "F8", "F11", "F12", "F7", "F6", "F5", "Pause", "ScrollLock", "Insert"]
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012


class HotkeyError(Exception):
    pass


def parse(text: str) -> tuple[int, int]:
    """'Ctrl+Shift+F9' → (修飾の合計, 仮想キー)。"""
    parts = [p.strip().lower().replace(" ", "") for p in str(text or "").split("+") if p.strip()]
    if not parts:
        raise HotkeyError("キーが空です")
    *mods, key = parts
    if key not in VK:
        raise HotkeyError(f"使えないキーです: {text}")
    m = 0
    for x in mods:
        if x not in MODS:
            raise HotkeyError(f"使えない組み合わせです: {text}")
        m |= MODS[x]
    return m, VK[key]


NICE = {"alt": "Alt", "ctrl": "Ctrl", "shift": "Shift", "win": "Win", "pause": "Pause", "scrolllock": "ScrollLock",
        "insert": "Insert", "home": "Home", "end": "End", "pageup": "PageUp", "pagedown": "PageDown",
        "printscreen": "PrintScreen", **{f"f{i}": f"F{i}" for i in range(1, 25)}}


def normalize(text: str) -> str:
    """表示と保存の形にそろえる（'ctrl+shift+f9' → 'Ctrl+Shift+F9'）。使えないキーなら HotkeyError。"""
    parse(text)
    return "+".join(NICE[p.strip().lower().replace(" ", "")] for p in str(text).split("+") if p.strip())


class Hotkeys:
    """{"F9": callback, …} を登録して、押されたら callback を呼ぶ（専用のスレッドでメッセージを待つ）。"""

    def __init__(self, keys: dict[str, callable]):
        self.keys = dict(keys)
        self.errors: list[str] = []
        self._tid = 0
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="rec-sketch-hotkey", daemon=True)

    @staticmethod
    def supported() -> bool:
        return os.name == "nt"

    def start(self) -> list[str]:
        """登録して、登録できなかったキーの理由を返す（全部だめでも録音は続ける）。"""
        if not self.supported():   # Windows 以外（確かめ用の環境）では登録しない。しおりは画面のボタンで付けられる
            return self.errors
        self._thread.start()
        self._ready.wait(3)
        return self.errors

    def stop(self) -> None:
        if self._tid:
            import ctypes
            ctypes.windll.user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)   # type: ignore[attr-defined]
            self._thread.join(2)

    def _run(self) -> None:
        import ctypes
        from ctypes import wintypes
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32   # type: ignore[attr-defined]
        self._tid = kernel32.GetCurrentThreadId()
        ids: dict[int, callable] = {}
        for i, (text, fn) in enumerate(self.keys.items(), start=1):
            try:
                mod, vk = parse(text)
            except HotkeyError as e:
                self.errors.append(str(e))
                continue
            if user32.RegisterHotKey(None, i, mod | MOD_NOREPEAT, vk):
                ids[i] = fn
            else:
                self.errors.append(f"{text} はほかのアプリが使っているので登録できません。設定でほかのキーに替えてください")
        self._ready.set()
        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY and msg.wParam in ids:
                    try:
                        ids[msg.wParam]()
                    except Exception:  # noqa: BLE001 — キーの処理で落ちても、待ち続ける
                        pass
        finally:
            for i in ids:
                user32.UnregisterHotKey(None, i)
