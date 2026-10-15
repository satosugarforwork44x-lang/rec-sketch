// rec/ui/viewer.js — 振り返り窓（rec:shots）。録音中・録音後の画面の静止画を、会議の画面と並べて見返す
// ・上に大きな画像（ホイールで拡大・ドラッグで移動・ダブルクリックで元の大きさ）、下に時刻つきの小さな画像の帯
// ・録音中は「最新に追従」: 新しい静止画が増えたら最新を出す。昔の画像を選んだら追従を止める（「最新へ」で戻る）
// ・開いている間は数秒ごとに api に知らせる（Hub の窓を取り込みから外せない Hub では、その間だけ自動の静止画を止めるため）
import { TOKEN, selectBox } from "/ui/js/hub.js";
import { icons } from "/ui/js/icons.js";

const EN = (document.documentElement.lang || navigator.language || "ja").startsWith("en");
const L = EN ? {
  title: "Look back", none: "No screen stills yet. Turn on \"Screen\" and record, or press the bookmark key.",
  follow: "Follow the latest", latest: "Latest", folder: "Show in Explorer", live: "recording", mark: "Bookmark",
  hint: "← → to move · wheel to zoom · double-click to fit",
  paused: "While this window is open, automatic stills are paused (Hub 0.10 or later keeps Hub's windows out of the stills instead). Bookmarks are still kept.",
} : {
  title: "振り返り", none: "まだ画面の静止画がありません。「画面も」をオンにして録音するか、しおりのキーを押してください。",
  follow: "最新に追従", latest: "最新へ", folder: "エクスプローラーで表示", live: "録音中", mark: "しおり",
  hint: "← → で前後 · ホイールで拡大 · ダブルクリックで元に戻す",
  paused: "この窓を開いている間は、自動の静止画を止めています（Hub 0.10 以上なら、Hub の窓を写さずに続けられます）。しおりは残ります。",
};

const clock = (sec) => {
  const s = Math.floor(sec || 0), p = (n) => String(n).padStart(2, "0");
  return `${p(Math.floor(s / 3600))}:${p(Math.floor(s / 60) % 60)}:${p(s % 60)}`;
};
const blob = (k) => `/blob/rec/${encodeURIComponent(k)}?t=${encodeURIComponent(TOKEN)}`;

export async function mountViewer(el, ctx) {
  ctx.setTitle?.(L.title);
  el.innerHTML = `<div class="rv">
    <div class="rv-head">
      <div class="rv-pick"></div>
      <button class="btn rv-follow" type="button" aria-pressed="true">${icons.refresh}<span>${L.follow}</span></button>
      <span class="rv-count"></span>
      <button class="icon-btn rv-folder" type="button" title="${L.folder}" aria-label="${L.folder}">${icons.reveal}</button>
    </div>
    <p class="rv-note" hidden></p>
    <div class="rv-stage" tabindex="0"><img class="rv-img" alt="" draggable="false"><p class="rv-empty" hidden>${L.none}</p>
      <span class="rv-cap"></span></div>
    <div class="rv-strip" role="listbox" aria-label="${L.title}"></div>
    <div class="rv-hint">${L.hint}</div>
  </div>`;
  const $ = (s) => el.querySelector(s);
  let data = { shots: [], choices: [] }, idx = -1, follow = true, file = null, pick = null, timer = 0, rev = null;
  let zoom = 1, ox = 0, oy = 0;

  // ---------- 表示 ----------
  function drawStrip() {
    const box = $(".rv-strip");
    box.innerHTML = data.shots.map((s, i) => `<button class="rv-th${i === idx ? " on" : ""}${s.kind === "mark" ? " mark" : ""}" type="button"
      role="option" aria-selected="${i === idx}" data-i="${i}"><img src="${blob(`th|${s.key}`)}" alt="" loading="lazy">
      <span>${s.kind === "mark" ? "🔖 " : ""}${clock(s.t)}</span></button>`).join("");
    box.querySelector(".on")?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }
  function show(i) {
    if (!data.shots.length) {
      idx = -1;
      $(".rv-img").hidden = true; $(".rv-empty").hidden = false; $(".rv-cap").textContent = ""; $(".rv-count").textContent = "";
      drawStrip();
      return;
    }
    idx = Math.max(0, Math.min(data.shots.length - 1, i));
    const s = data.shots[idx];
    const img = $(".rv-img");
    img.hidden = false; $(".rv-empty").hidden = true;
    if (img.dataset.key !== s.key) { img.src = blob(`s|${s.key}`); img.dataset.key = s.key; resetZoom(); }
    $(".rv-cap").textContent = `${s.kind === "mark" ? `🔖 ${L.mark} · ` : ""}${clock(s.t)}`;
    $(".rv-count").textContent = `${idx + 1} / ${data.shots.length}`;
    drawStrip();
  }
  function setFollow(v) {
    follow = v;
    const b = $(".rv-follow");
    b.setAttribute("aria-pressed", String(v));
    b.querySelector("span").textContent = v || !data.live ? L.follow : L.latest;
    b.hidden = !data.live;
  }

  // ---------- 読み込み ----------
  async function load(first = false) {
    try {
      const d = await ctx.call("shots", file ? { file } : {});
      const grew = d.shots.length !== data.shots.length || d.file !== data.file;
      data = d;
      file = d.file;
      if (rev === null) rev = d.viewRev;
      if (first || !pick) {
        pick = selectBox({ options: d.choices.map((c) => ({ value: c.file, label: c.live ? `● ${c.name}（${L.live}）` : c.name })),
          value: d.file, labelFor: () => d.name || "—", onChange: (v) => { ctx.call("view", { file: v }).catch(() => {}); file = v; setFollow(true); load(true); } });
        $(".rv-pick").replaceChildren(pick);
      }
      $(".rv-note").hidden = !(d.live && d.autoPaused);
      $(".rv-note").textContent = L.paused;
      setFollow(follow);
      if (first || (grew && follow)) show(follow ? d.shots.length - 1 : idx);
      else if (grew) drawStrip();
    } catch (e) { ctx.toast(e.message, "error"); }
  }
  async function tick() {
    if (!el.isConnected) return;
    const v = await ctx.call("viewer_open", { on: true }).catch(() => null);
    if (v && rev !== null && v.viewRev !== rev) { rev = v.viewRev; file = null; setFollow(true); await load(true); }   // 一覧から別の録音を選ばれた
    else if (data.live || !file) await load();
    timer = setTimeout(tick, 1500);
  }
  addEventListener("pagehide", () => { ctx.call("viewer_open", { on: false }).catch(() => {}); clearTimeout(timer); });

  // ---------- 操作 ----------
  $(".rv-strip").addEventListener("click", (e) => {
    const b = e.target.closest(".rv-th");
    if (!b) return;
    const i = Number(b.dataset.i);
    setFollow(data.live && i === data.shots.length - 1);
    show(i);
  });
  $(".rv-follow").addEventListener("click", () => { setFollow(true); show(data.shots.length - 1); });
  $(".rv-folder").addEventListener("click", () => data.dir && ctx.host.revealPath(data.dir).catch((e) => ctx.toast(e.message, "error")));
  el.addEventListener("keydown", (e) => {
    if (e.target.closest("input, .select")) return;
    const step = { ArrowLeft: -1, ArrowRight: 1, Home: -1e9, End: 1e9 }[e.key];
    if (step === undefined) return;
    e.preventDefault();
    const i = Math.max(0, Math.min(data.shots.length - 1, idx + step));
    setFollow(data.live && i === data.shots.length - 1);
    show(i);
  });

  // 拡大・移動（スライドの小さな字を読む用）
  const img = $(".rv-img");
  const apply = () => { img.style.transform = `translate(${ox}px, ${oy}px) scale(${zoom})`; img.classList.toggle("zoomed", zoom > 1); };
  function resetZoom() { zoom = 1; ox = 0; oy = 0; apply(); }
  $(".rv-stage").addEventListener("wheel", (e) => {
    if (img.hidden) return;
    e.preventDefault();
    const r = $(".rv-stage").getBoundingClientRect();
    const cx = e.clientX - r.left - r.width / 2, cy = e.clientY - r.top - r.height / 2;
    const nz = Math.max(1, Math.min(6, zoom * (e.deltaY < 0 ? 1.2 : 1 / 1.2)));
    ox = cx - ((cx - ox) * nz) / zoom; oy = cy - ((cy - oy) * nz) / zoom; zoom = nz;
    if (zoom === 1) { ox = 0; oy = 0; }
    apply();
  }, { passive: false });
  let drag = null;
  img.addEventListener("pointerdown", (e) => { if (zoom > 1) { drag = { x: e.clientX - ox, y: e.clientY - oy }; img.setPointerCapture(e.pointerId); } });
  img.addEventListener("pointermove", (e) => { if (drag) { ox = e.clientX - drag.x; oy = e.clientY - drag.y; apply(); } });
  img.addEventListener("pointerup", () => { drag = null; });
  $(".rv-stage").addEventListener("dblclick", resetZoom);

  await load(true);
  $(".rv-stage").focus();
  tick();
}
