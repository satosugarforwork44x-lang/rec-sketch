// rec/ui/panel.js — レックスケッチ。録音ボタン・経過・音量・録音の一覧（聞く・文字にする）
// ・録音そのものは api.py の別スレッド。窓を閉じても続く。開き直すと今の状態を読んで続きから見せる
// ・録音中は live() を短い間隔で読む（経過と音量）。止まっているときは、文字起こし中だけ 1 秒ごとに読む
import { TOKEN, selectBox } from "/ui/js/hub.js";
import { icons } from "/ui/js/icons.js";

const EN = (document.documentElement.lang || navigator.language || "ja").startsWith("en");
const L = EN ? {
  mic: "Mic", both: "Mic + PC sound", micDev: "Microphone", micDefault: "Default (Windows)", title: "Name (optional)",
  titlePh: "e.g. Weekly meeting", start: "Start recording", stop: "Stop and save", me: "Me", them: "Them",
  bothTip: "Records the other side of a web meeting too. Saved as 2 channels: left = you, right = PC sound",
  keepGoing: "Recording continues even if you close this window.", headphones: "Tip: use headphones so the other side's voice isn't picked up twice.",
  saved: (f) => `Saved ${f}`, stopped: "Recording stopped", list: "Recordings", empty: "No recordings yet",
  openFolder: "Show in Explorer", play: "Play", pause: "Pause", toText: "Transcribe", openText: "Open text", cancel: "Cancel",
  ch2: "Mic + PC", ch1: "Mic", textHead: "Transcription", model: "Model", lang: "Language",
  langs: { ja: "Japanese", en: "English", auto: "Auto" },
  models: { small: "small · fast", medium: "medium", "large-v3-turbo": "large-v3-turbo · accurate" },
  modelTip: "Downloaded on first use: small ≈ 0.5 GB, medium ≈ 1.5 GB, large-v3-turbo ≈ 1.6 GB",
  wOk: (w) => `faster-whisper ${w.version} · Python ${w.pyver}`, wChecking: "Looking for faster-whisper…",
  wNone: "faster-whisper not found — recording works without it. To transcribe: py -m pip install faster-whisper",
  find: "Search again", stage: { start: "Starting…", model: "Loading the model (first time: downloading)…", audio: "Reading the audio…", text: "Transcribing" },
  done: (f) => `Text saved: ${f}`, failed: "Transcription failed", cancelled: "Cancelled",
  share: "Export to share (M4A, small enough to send)", shareShow: "Show the shared M4A in Explorer", sharing: "Exporting M4A",
  shared: (f) => `Exported ${f}`, shareFailed: "Export failed",
  consentT: "Before you record a meeting", consent: "Tell everyone that you are recording and get their consent. Recordings and text stay on this PC (in the Rec Sketch folder).",
  ok: "OK", claude: "Ask Claude to write the minutes from the .md file (share the Rec Sketch folder in Settings → Claude).",
} : {
  mic: "マイク", both: "マイク＋PC の音", micDev: "マイク", micDefault: "既定（Windows の設定）", title: "名前（任意）",
  titlePh: "例: 定例会議", start: "録音を始める", stop: "止めて保存", me: "自分", them: "相手",
  bothTip: "Web 会議の相手の声も録ります。左＝自分・右＝PC の音の 2ch で保存します",
  keepGoing: "窓を閉じても録音は続きます。", headphones: "ヘッドホンを使うと、相手の声が「自分」の側に混ざりません。",
  saved: (f) => `保存しました: ${f}`, stopped: "録音が止まりました", list: "録音", empty: "まだ録音はありません",
  openFolder: "エクスプローラーで表示", play: "聞く", pause: "止める", toText: "文字にする", openText: "文字を開く", cancel: "やめる",
  ch2: "マイク＋PC", ch1: "マイク", textHead: "文字起こし", model: "モデル", lang: "言語",
  langs: { ja: "日本語", en: "英語", auto: "自動" },
  models: { small: "small・速い", medium: "medium", "large-v3-turbo": "large-v3-turbo・正確" },
  modelTip: "初回に取得します: small 約 0.5GB / medium 約 1.5GB / large-v3-turbo 約 1.6GB",
  wOk: (w) => `faster-whisper ${w.version} · Python ${w.pyver}`, wChecking: "faster-whisper を探しています…",
  wNone: "faster-whisper が見つかりません（録音はそのまま使えます）。文字にするには: py -m pip install faster-whisper",
  find: "探し直す", stage: { start: "準備しています…", model: "モデルを読み込み中（初回は取得します）…", audio: "音を読み込み中…", text: "文字にしています" },
  done: (f) => `文字にしました: ${f}`, failed: "文字起こしに失敗しました", cancelled: "やめました",
  share: "共有用に書き出す（M4A。小さくして送れる）", shareShow: "共有用の M4A をエクスプローラーで表示", sharing: "共有用に書き出しています",
  shared: (f) => `共有用に書き出しました: ${f}`, shareFailed: "書き出しに失敗しました",
  consentT: "会議を録る前に", consent: "参加者に録音することを伝えて、同意をもらってください。録音と文字はこの PC の中（Rec Sketch フォルダ）にだけ残ります。",
  ok: "わかりました", claude: "議事録は、.md を Claude に読ませてまとめてもらえます（設定 → Claude 連携 で Rec Sketch フォルダを見せる）。",
};

const css = document.createElement("link");
css.rel = "stylesheet";
css.href = new URL("./rec.css", import.meta.url);
document.head.append(css);

const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const clock = (sec) => {
  const s = Math.max(0, Math.floor(sec || 0));
  const p = (n) => String(n).padStart(2, "0");
  return s >= 3600 ? `${Math.floor(s / 3600)}:${p(Math.floor(s / 60) % 60)}:${p(s % 60)}` : `${p(Math.floor(s / 60))}:${p(s % 60)}`;
};
const when = (t) => {
  const d = new Date(t * 1000);
  return d.toLocaleString(EN ? "en-US" : "ja-JP", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
};
const pct = (db) => Math.max(0, Math.min(100, ((db + 60) / 60) * 100));   // -60 dB 〜 0 dB を棒に

export async function mount(el, ctx) {
  el.innerHTML = `<div class="rc">
    <section class="rc-card">
      <div class="seg rc-src" role="radiogroup">
        <button type="button" role="radio" data-v="mic">${icons.mic}<span>${L.mic}</span></button>
        <button type="button" role="radio" data-v="both" title="${esc(L.bothTip)}">${icons.voiceMemo}<span>${L.both}</span></button>
      </div>
      <label class="rc-field"><span>${L.micDev}</span><div class="rc-mic"></div></label>
      <label class="rc-field"><span>${L.title}</span><input class="rc-title" type="text" maxlength="60" placeholder="${esc(L.titlePh)}"></label>
      <div class="rc-main">
        <button class="rc-btn" type="button" aria-label="${L.start}" title="${L.start}"><span class="rc-dot"></span></button>
        <div class="rc-time" aria-live="off">00:00</div>
      </div>
      <div class="rc-meters"></div>
      <p class="rc-note"></p>
    </section>
    <div class="rc-listhead"><span>${L.list}</span><button class="icon-btn rc-reveal" type="button" title="${L.openFolder}">${icons.reveal}</button></div>
    <div class="rc-list" role="list"></div>
    <section class="rc-foot">
      <div class="rc-foot-row"><span class="rc-foot-h">${icons.transcript}${L.textHead}</span>
        <div class="rc-model" title="${esc(L.modelTip)}"></div><div class="rc-lang"></div></div>
      <div class="rc-foot-row"><span class="rc-w"></span><button class="btn rc-find" type="button">${icons.refresh}<span>${L.find}</span></button></div>
    </section>
    <div class="rc-consent" hidden><div class="rc-consent-box" role="dialog" aria-modal="true" aria-labelledby="rc-ct">
      <h2 id="rc-ct">${L.consentT}</h2><p>${L.consent}</p><p class="rc-sub">${L.claude}</p>
      <div class="btn-row"><button class="btn primary rc-ok" type="button">${L.ok}</button></div></div></div>
  </div>`;
  const $ = (s) => el.querySelector(s);
  if (EN) el.querySelector(".rc").style.setProperty("--rc-lab", "112px");   // 英語は見出しが長い
  const audio = new Audio();
  let st = null, playing = null, fastTimer = 0, slowTimer = 0, micBox = null;
  const last = { job: null, share: null };   // 前に見た文字起こし・書き出しの状態（終わったときのお知らせ用）

  // ---------- 入力 ----------
  const setSource = (v) => {
    el.querySelectorAll(".rc-src button").forEach((b) => b.setAttribute("aria-checked", String(b.dataset.v === v)));
    st.prefs.source = v;
  };
  el.querySelectorAll(".rc-src button").forEach((b) => b.addEventListener("click", () => {
    if (st.rec?.running) return;
    setSource(b.dataset.v);
    ctx.call("set_prefs", { source: b.dataset.v }).catch(() => {});
    drawMeters();
  }));

  async function loadMics() {
    let list = [];
    try { list = await ctx.call("mics"); st.micError = ""; } catch (e) { st.micError = e.message; }
    drawRec();
    const opts = [{ value: "", label: L.micDefault }, ...list.map((m) => ({ value: m.id, label: m.name }))];
    micBox = selectBox({ options: opts, value: st.prefs.mic || "", onChange: (v) => { st.prefs.mic = v; ctx.call("set_prefs", { mic: v }).catch(() => {}); } });
    $(".rc-mic").replaceChildren(micBox);
  }

  // ---------- 録音 ----------
  $(".rc-btn").addEventListener("click", async () => {
    const b = $(".rc-btn");
    b.disabled = true;
    try {
      if (st.rec?.running) {
        const r = await ctx.call("stop");
        Object.assign(st, r);
        if (r.saved) ctx.toast(L.saved(r.saved));
        if (r.lastError) ctx.toast(r.lastError, "error", 8000);
        $(".rc-title").value = "";
      } else {
        Object.assign(st, await ctx.call("start", { source: st.prefs.source, mic: st.prefs.mic || "", title: $(".rc-title").value }));
      }
    } catch (e) { ctx.toast(e.message, "error", 8000); }
    b.disabled = false;
    drawAll();
    schedule();
  });

  function drawRec() {
    const on = !!st.rec?.running;
    const b = $(".rc-btn");
    b.classList.toggle("on", on);
    b.setAttribute("aria-label", on ? L.stop : L.start);
    b.title = on ? L.stop : L.start;
    $(".rc-time").textContent = clock(on ? st.rec.seconds : 0);
    $(".rc-time").classList.toggle("on", on);
    el.querySelectorAll(".rc-src button").forEach((x) => (x.disabled = on));
    $(".rc-title").disabled = on;
    if (micBox) micBox.disabled = on;
    const err = st.lastError || st.micError || "";
    $(".rc-note").textContent = on ? `${L.keepGoing}${st.rec.source === "both" ? " " + L.headphones : ""}` : err;
    $(".rc-note").classList.toggle("err", !on && !!err);
    ctx.setStatus(on ? `● ${st.rec.file}` : st.folder || "");
    drawMeters();
  }

  function drawMeters() {
    const src = st.rec?.running ? st.rec.source : st.prefs.source;
    const names = src === "both" ? [L.me, L.them] : [L.me];
    const box = $(".rc-meters");
    if (box.childElementCount !== names.length) {
      box.innerHTML = names.map((n) => `<div class="rc-meter"><span>${n}</span><div class="rc-bar"><i></i></div><b>–</b></div>`).join("");
    }
    const lv = st.rec?.running ? st.rec.levels : [];
    box.querySelectorAll(".rc-meter").forEach((m, i) => {
      const db = lv[i];
      m.querySelector("i").style.width = `${db == null ? 0 : pct(db)}%`;
      m.classList.toggle("hot", db != null && db > -3);
      m.querySelector("b").textContent = db == null || db <= -90 ? "–" : `${Math.round(db)} dB`;
    });
  }

  // ---------- 一覧 ----------
  // 進み具合だけが変わったときは、一覧を作り直さずに棒と数字だけ直す（作り直すと、押した瞬間のボタンが消えて押せないことがある）
  let listKey = "";
  const keyOf = () => JSON.stringify([st.files.map((f) => [f.file, f.text, f.m4a]), st.job?.file, st.job?.state, st.job?.stage, st.job?.who,
    st.share?.file, st.share?.state, st.canShare, st.whisper?.state, playing, audio.paused]);
  function updateList() {
    if (keyOf() !== listKey) return drawList();
    for (const [key, j] of [["job", st.job], ["share", st.share]]) {
      if (j?.state !== "running") continue;
      const row = [...el.querySelectorAll(".rc-item")].find((x) => x.dataset.f === j.file);
      const bar = row?.querySelector(`.rc-prog[data-k="${key}"]`);
      if (!bar) continue;
      bar.querySelector("i").style.width = `${Math.round((j.progress || 0) * 100)}%`;
      bar.querySelector(".pct").textContent = key === "job" && j.stage !== "text" ? "" : `${Math.round((j.progress || 0) * 100)}%`;
    }
  }

  function drawList() {
    listKey = keyOf();
    const box = $(".rc-list");
    if (!st.files.length) { box.innerHTML = `<p class="rc-empty">${L.empty}</p>`; return; }
    const ok = st.whisper?.state === "ok";
    const job = st.job, sh = st.share;
    box.innerHTML = st.files.map((f) => {
      const busy = job?.state === "running" && job.file === f.file;
      const sharing = sh?.state === "running" && sh.file === f.file;
      const meta = [f.seconds != null ? clock(f.seconds) : "", f.channels === 2 ? L.ch2 : f.channels === 1 ? L.ch1 : "", when(f.mtime), f.m4a ? "M4A" : ""].filter(Boolean).join(" · ");
      const shareBtn = !st.canShare && !f.m4a ? "" : `<button class="icon-btn rc-share${f.m4a ? " has" : ""}" type="button" title="${esc(f.m4a ? L.shareShow : L.share)}"
        aria-label="${esc(f.m4a ? L.shareShow : L.share)}"${sharing || (!f.m4a && sh?.state === "running") ? " disabled" : ""}>${icons.upload}</button>`;
      const act = busy
        ? `<button class="btn rc-cancel" type="button">${L.cancel}</button>`
        : f.text ? `<button class="btn rc-open" type="button" title="${esc(f.text)}">${icons.transcript}<span>${L.openText}</span></button>`
        : ok ? `<button class="btn rc-totext" type="button"${job?.state === "running" ? " disabled" : ""}>${icons.text}<span>${L.toText}</span></button>` : "";
      const prog = busy ? `<div class="rc-prog" data-k="job"><div class="rc-prog-bar"><i style="width:${Math.round((job.progress || 0) * 100)}%"></i></div>
        <span>${esc(L.stage[job.stage] || L.stage.start)}${job.who ? (EN ? ` (${esc(job.who)})` : `（${esc(job.who)}）`) : ""} <b class="pct">${job.stage === "text" ? Math.round((job.progress || 0) * 100) + "%" : ""}</b></span></div>` : "";
      const sprog = sharing ? `<div class="rc-prog" data-k="share"><div class="rc-prog-bar"><i style="width:${Math.round((sh.progress || 0) * 100)}%"></i></div>
        <span>${esc(L.sharing)} <b class="pct">${Math.round((sh.progress || 0) * 100)}%</b></span></div>` : "";
      return `<div class="rc-item${playing === f.file ? " playing" : ""}" role="listitem" data-f="${esc(f.file)}">
        <button class="icon-btn rc-play" type="button" title="${playing === f.file ? L.pause : L.play}">${playing === f.file && !audio.paused ? icons.pause : icons.play}</button>
        <div class="rc-name"><b title="${esc(f.file)}">${esc(f.name)}</b><span>${esc(meta)}</span></div>
        <div class="rc-act">${act}${shareBtn}</div>${prog}${sprog}</div>`;
    }).join("");
  }

  $(".rc-list").addEventListener("click", async (e) => {
    const item = e.target.closest(".rc-item");
    if (!item) return;
    const f = st.files.find((x) => x.file === item.dataset.f);
    try {
      if (e.target.closest(".rc-play")) {
        if (playing === f.file) { audio.paused ? audio.play() : audio.pause(); }
        else {
          playing = f.file;
          audio.src = `/blob/rec/${encodeURIComponent(f.file)}?t=${encodeURIComponent(TOKEN)}`;
          await audio.play();
        }
      } else if (e.target.closest(".rc-totext")) {
        st.job = await ctx.call("transcribe", { file: f.file, model: st.prefs.model, lang: st.prefs.lang });
        schedule();
      } else if (e.target.closest(".rc-open")) {
        await ctx.host.openPath(f.textPath);
      } else if (e.target.closest(".rc-share")) {
        if (f.m4a) await ctx.host.revealPath(f.m4aPath);
        else { st.share = await ctx.call("share", { file: f.file }); schedule(); }
      } else if (e.target.closest(".rc-cancel")) {
        st.job = await ctx.call("transcribe_cancel");
      }
    } catch (err) { ctx.toast(err.message, "error", 8000); }
    drawList();
  });
  audio.addEventListener("play", drawList);
  audio.addEventListener("pause", drawList);
  audio.addEventListener("ended", () => { playing = null; drawList(); });

  $(".rc-reveal").addEventListener("click", () => ctx.host.revealPath(st.folder).catch((e) => ctx.toast(e.message, "error")));

  // ---------- 文字起こしの設定 ----------
  function drawWhisper() {
    const w = st.whisper || { state: "none" };
    $(".rc-w").textContent = w.state === "ok" ? L.wOk(w) : w.state === "checking" ? L.wChecking : L.wNone;
    $(".rc-w").classList.toggle("muted", w.state !== "none");
    $(".rc-find").disabled = w.state === "checking";
  }
  $(".rc-find").addEventListener("click", async () => {
    try { st.whisper = await ctx.call("whisper_find"); } catch (e) { ctx.toast(e.message, "error"); }
    drawWhisper();
    schedule();
  });

  // ---------- 読み直し ----------
  function drawAll() { drawRec(); drawList(); drawWhisper(); }

  // 文字起こし（job）・共有用の書き出し（share）が終わったら知らせる。変わったら true
  function jobChanged() {
    let changed = false;
    for (const key of ["job", "share"]) {
      const j = st[key], before = last[key];
      if (j && before && j.state !== before.state) {
        changed = true;
        if (before.state === "running") {
          const msg = key === "job" ? { done: L.done, failed: L.failed } : { done: L.shared, failed: L.shareFailed };
          if (j.state === "done") ctx.toast(msg.done(j.out));
          else if (j.state === "cancelled") ctx.toast(L.cancelled);
          else if (j.state === "error") ctx.toast(`${msg.failed}: ${j.error}`, "error", 12000);
        }
      }
      last[key] = j;
    }
    return changed;
  }

  async function tick() {
    try {
      const was = !!st.rec?.running;
      Object.assign(st, await ctx.call("live"));
      if (was && !st.rec?.running) { if (st.rec?.error || st.lastError) ctx.toast(st.rec?.error || L.stopped, "error", 8000); await refresh(); }
      else if (jobChanged()) await refresh();
      else { drawRec(); updateList(); drawWhisper(); }
    } catch { /* 次の回でまた読む */ }
    schedule();
  }

  async function refresh() {
    Object.assign(st, await ctx.call("state"));
    jobChanged();
    drawAll();
  }

  function schedule() {
    clearTimeout(fastTimer);
    const fast = st.rec?.running ? 150 : st.job?.state === "running" || st.share?.state === "running" || st.whisper?.state === "checking" ? 1000 : 0;
    if (fast && el.isConnected) fastTimer = setTimeout(tick, fast);
  }

  // 別の所（Claude・エクスプローラー）で増えた・消えたファイルも、ときどき読み直す
  clearInterval(slowTimer);
  slowTimer = setInterval(() => { if (!el.isConnected) return clearInterval(slowTimer); if (!st.rec?.running && document.visibilityState === "visible") refresh().catch(() => {}); }, 10000);

  // ---------- 初回 ----------
  st = await ctx.call("state");
  last.job = st.job;
  last.share = st.share;
  setSource(st.prefs.source);
  const mk = (cls, options, key) => $(cls).replaceChildren(selectBox({ options, value: st.prefs[key], onChange: (v) => { st.prefs[key] = v; ctx.call("set_prefs", { [key]: v }).catch(() => {}); } }));
  mk(".rc-model", Object.entries(L.models).map(([value, label]) => ({ value, label })), "model");
  mk(".rc-lang", Object.entries(L.langs).map(([value, label]) => ({ value, label })), "lang");
  drawAll();
  loadMics();
  schedule();
  if (!st.consent) {
    $(".rc-consent").hidden = false;
    $(".rc-ok").focus();
    $(".rc-ok").addEventListener("click", async () => { $(".rc-consent").hidden = true; await ctx.call("consent").catch(() => {}); });
  }
}
