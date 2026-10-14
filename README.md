# Rec Sketch

<p align="center"><b>English</b> · <a href="README.ja.md">日本語</a></p>

An extension app for **[Hub](https://github.com/satosugarforwork44x-lang/desktop-hub)**, the slim bar on the edge of your Windows desktop.
**Record your microphone and your PC's sound (the other side of a web meeting) to WAV, and transcribe it on your own PC.**
The text lands in a Hub folder panel, so Claude can read it and write the minutes. The audio never leaves your PC.

> License: [PolyForm Noncommercial 1.0.0](LICENSE) — free for personal and other non-commercial use.

## Why I built this

Taking notes while talking in a meeting is hard, and afterwards nobody remembers who agreed to do what by when.
There are plenty of recorder apps, but three things kept getting in the way:

- **The other side isn't recorded.** In a web meeting their voice comes out of the speakers: faint on the mic, and missing entirely with headphones.
- **Most transcription sends your audio to the cloud.** Not something you want for internal meetings.
- **Recordings just pile up.** Nobody re-listens to an hour of audio. What you need is the decisions and the to-dos.

Rec Sketch starts recording with one click from Hub, keeps **your voice and the other side's voice apart**, transcribes **on your PC**,
and puts the text **where Claude can read it**.

## UX decisions

| Decision | Why |
|---|---|
| **Mic + PC sound is saved as 2 channels (left = you, right = PC)** | Mixing loses who said what. Splitting at the source lets the transcript label every line 自分 (you) or 相手 (them) |
| **Recording runs in a background thread inside Hub** | Close the window and it keeps going. The bar shows `REC 12:34`, so you always know it's on |
| **Transcription runs as a separate process, using whichever Python has faster-whisper** | Works even when faster-whisper can't be installed into Hub's Python (e.g. Hub on 3.15, transcription on 3.14). The big model never stays in Hub's memory, and a crash can't take Hub down |
| **Transcription is optional** | Without faster-whisper the button simply isn't shown; recording works as usual |
| **No tool sends audio to Claude** | Only the text (.md) is shared. Claude reads it with Hub's existing `folders_read_text` |
| **Their voice leaking into your mic is removed from 自分 (you)** | Without headphones the mic hears the speakers too. Lines at the same time with nearly the same words are dropped as echoes |
| **No always-on noise meter** | The mic is only open while recording; the level bars are just for checking the input |
| **A one-time consent reminder** | Recording a meeting needs everyone's consent; the panel says so once |
| **Nothing is overwritten** | `name (2).wav`. The file is written as it goes, so a crash still leaves everything up to that point |

## Features

| Feature | What it does |
|---|---|
| Record | Mic only (1 ch) or mic + PC sound (2 ch). 48 kHz / 16-bit WAV. Pick the microphone from a list |
| Levels | Live level bars for you and the PC sound while recording |
| List | Play, transcribe, open the text, show the folder in Explorer |
| Transcribe | faster-whisper (small / medium / large-v3-turbo), Japanese / English / auto. A `.md` with timestamps and 自分 / 相手 (you / them) labels |
| Bar | `REC 12:34` while recording, `TXT 42%` while transcribing |

## Install

1. Put this repository into Hub's module folder as `rec`: `%LOCALAPPDATA%\Hub\modules\rec\`
2. Install what recording needs (both BSD-3-Clause):
   ```bat
   py -m pip install soundcard numpy
   ```
3. Optional, for transcription (MIT):
   ```bat
   py -m pip install faster-whisper
   ```
   It may go into a different Python than Hub's (e.g. `py -3.14 -m pip install faster-whisper`); Rec Sketch looks for it with `py -0p`.
4. Restart Hub — "Rec Sketch" appears on the bar.

Requires Windows 10/11, Python 3.10+, Hub 0.9+.

## Where files go

A **`Rec Sketch`** folder is created next to your other folder panels (change it with `modules.rec.folder`).

| File | Contents |
|---|---|
| `<date> <name>.wav` | The recording |
| `<date> <name>.md` | The transcript (`[00:01:23] 自分: …`) |
| `REC_FORMAT.md` | A short guide for Claude (placed automatically) |

To get minutes: in Hub's Settings → Claude, share the "Rec Sketch" folder panel, then ask Claude to "summarise <name>.md as minutes".

The model is downloaded once on first use (small ≈ 0.5 GB, medium ≈ 1.5 GB, large-v3-turbo ≈ 1.6 GB); after that everything runs offline on the CPU.

## Notes

- Tell everyone you are recording and get their consent.
- Automatic transcripts get names and numbers wrong sometimes — check against the audio.
- 自分 / 相手 (you / them) comes from where the sound was recorded, not from voice recognition. Everyone on the other side is 相手.
- The transcript headings are in Japanese for now.

## License

| Component | License |
|---|---|
| Rec Sketch | PolyForm Noncommercial 1.0.0 |
| soundcard, numpy | BSD-3-Clause |
| faster-whisper, CTranslate2, Whisper models | MIT |
