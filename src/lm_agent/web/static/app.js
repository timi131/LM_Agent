"use strict";

const $ = (sel) => document.querySelector(sel);
const els = {
  chat: $("#chat"), messages: $("#messages"), empty: $("#empty"), thinking: $("#thinking"),
  form: $("#composer"), input: $("#input"), send: $("#send"), model: $("#model"), status: $("#status"),
  drawer: $("#drawer"), scrim: $("#scrim"),
};

// --- хранилище браузера (может быть недоступно — тогда просто без запоминания) ---
const store = {
  get(key) { try { return localStorage.getItem(key); } catch { return null; } },
  set(key, value) { try { localStorage.setItem(key, value); } catch { /* приватный режим */ } },
};

function newId() {
  return (crypto.randomUUID && crypto.randomUUID()) || Math.random().toString(36).slice(2) + Date.now().toString(36);
}

const params = new URLSearchParams(location.search);
if (params.get("token")) {
  store.set("lm-agent-token", params.get("token"));
  history.replaceState(null, "", location.pathname);  // токен не должен оставаться в адресе и закладках
}
const token = store.get("lm-agent-token") || "";

// --- вход по токену (когда агент открыт в локальной сети) ---
let gateShown = false;
function showGate(message = "") {
  if (gateShown) return;
  gateShown = true;
  $("#gate").hidden = false;
  $("#gate-error").textContent = message;
  $("#gate-token").focus();
}
$("#gate-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const value = $("#gate-token").value.trim();
  const resp = await fetch("/api/info", { headers: { "X-Token": value } }).catch(() => null);
  if (resp && resp.ok) {
    store.set("lm-agent-token", value);
    location.reload();
  } else {
    $("#gate-error").textContent = resp ? "Неверный токен" : "Сервер не отвечает";
  }
});
let sessionId = store.get("lm-agent-session") || newId();
store.set("lm-agent-session", sessionId);

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (token) headers["X-Token"] = token;
  if (options.json !== undefined) {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(options.json);
  }
  const resp = await fetch(path, { ...options, headers });
  if (resp.status === 401) showGate(token ? "Токен не подошёл — введите актуальный" : "");
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
  return data;
}

// --- мини-markdown: только безопасное подмножество, весь текст экранируется ---
function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function inline(text) {
  const codes = [];
  let s = escapeHtml(text).replace(/`([^`]+)`/g, (_, c) => `\u0000${codes.push(c) - 1}\u0000`);
  s = s
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
    .replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>');
  return s.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${codes[i]}</code>`);
}

function markdown(src) {
  const out = [];
  const lines = src.replace(/\r\n/g, "\n").split("\n");
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (/^```/.test(line)) {
      const buf = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
      i++;
      out.push(`<pre><code>${escapeHtml(buf.join("\n"))}</code></pre>`);
    } else if (/^#{1,6}\s/.test(line)) {
      const level = Math.min(line.match(/^#+/)[0].length, 3);
      out.push(`<h${level}>${inline(line.replace(/^#+\s*/, ""))}</h${level}>`);
      i++;
    } else if (/^\s*([-*•]|\d+[.)])\s+/.test(line)) {
      const ordered = /^\s*\d+[.)]/.test(line);
      const items = [];
      while (i < lines.length && /^\s*([-*•]|\d+[.)])\s+/.test(lines[i])) {
        items.push(`<li>${inline(lines[i].replace(/^\s*([-*•]|\d+[.)])\s+/, ""))}</li>`);
        i++;
      }
      out.push(ordered ? `<ol>${items.join("")}</ol>` : `<ul>${items.join("")}</ul>`);
    } else if (!line.trim()) {
      i++;
    } else {
      const buf = [];
      while (i < lines.length && lines[i].trim() && !/^(```|#{1,6}\s|\s*([-*•]|\d+[.)])\s+)/.test(lines[i])) buf.push(lines[i++]);
      out.push(`<p>${buf.map(inline).join("<br>")}</p>`);
    }
  }
  return out.join("");
}

// --- отрисовка ---
function scrollDown() {
  requestAnimationFrame(() => { els.chat.scrollTop = els.chat.scrollHeight; });
}

function updateEmpty() {
  els.empty.hidden = els.messages.children.length > 0;
}

function addMessage(role, text) {
  const wrap = document.createElement("div");
  wrap.className = `msg ${role}`;
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  if (role === "assistant") bubble.innerHTML = markdown(text);
  else bubble.textContent = text;
  wrap.append(bubble);
  els.messages.append(wrap);
  updateEmpty();
  scrollDown();
}

function short(value, limit = 120) {
  const s = typeof value === "string" ? value : JSON.stringify(value);
  return s.length > limit ? s.slice(0, limit) + "…" : s;
}

function addTool(name, args, result) {
  const d = document.createElement("details");
  d.className = "tool" + (/^Отклонено/.test(result) ? " denied" : "");
  const summary = document.createElement("summary");
  const nameEl = document.createElement("span");
  nameEl.className = "name";
  nameEl.textContent = "🔧 " + name;
  const argsEl = document.createElement("span");
  argsEl.className = "args";
  argsEl.textContent = args && Object.keys(args).length ? short(args) : "";
  summary.append(nameEl, argsEl);
  const pre = document.createElement("pre");
  pre.textContent = result;
  d.append(summary, pre);
  els.messages.append(d);
  const file = /Файл готов: (.+?) \((\d[^)]*)\)/.exec(result || "");
  if (file) els.messages.append(fileCard(file[1], file[2]));
  updateEmpty();
  scrollDown();
}

function fileCard(path, size) {
  const name = path.split("/").pop();
  const ext = (name.split(".").pop() || "").toLowerCase();
  const card = document.createElement("a");
  card.className = "file-card";
  card.href = `/api/files/${path.split("/").map(encodeURIComponent).join("/")}${token ? `?token=${encodeURIComponent(token)}` : ""}`;
  card.download = name;
  const icon = document.createElement("span");
  icon.className = `file-icon ${ext}`;
  icon.textContent = { docx: "W", xlsx: "X", pdf: "PDF", md: "MD" }[ext] || "📄";
  const text = document.createElement("span");
  text.className = "file-text";
  const b = document.createElement("b");
  b.textContent = name;
  const small = document.createElement("small");
  small.textContent = `${size} · Скачать`;
  text.append(b, small);
  card.append(icon, text);
  return card;
}

function addApproval(ev) {
  const card = document.createElement("div");
  card.className = "approval";
  card.dataset.id = ev.id;
  const h = document.createElement("h3");
  h.textContent = `⚠️ Агент хочет выполнить ${ev.name}`;
  const dl = document.createElement("dl");
  for (const [key, value] of Object.entries(ev.arguments || {})) {
    const dt = document.createElement("dt");
    dt.textContent = key;
    const dd = document.createElement("dd");
    const pre = document.createElement("pre");
    pre.textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
    dd.append(pre);
    dl.append(dt, dd);
  }
  const buttons = document.createElement("div");
  buttons.className = "buttons";
  const allow = document.createElement("button");
  allow.className = "allow";
  allow.textContent = "Разрешить";
  const deny = document.createElement("button");
  deny.className = "deny";
  deny.textContent = "Отклонить";
  const decide = (ok) => {
    send({ type: "approve", id: ev.id, allow: ok });
    setVerdict(card, ok ? "yes" : "no", ok ? "✓ Разрешено" : "✕ Отклонено");
  };
  allow.onclick = () => decide(true);
  deny.onclick = () => decide(false);
  buttons.append(allow, deny);
  card.append(h, dl, buttons);
  els.messages.append(card);
  updateEmpty();
  scrollDown();
  allow.focus({ preventScroll: true });
}

function setVerdict(card, kind, text) {
  const buttons = card.querySelector(".buttons");
  if (!buttons) return;
  const verdict = document.createElement("div");
  verdict.className = `verdict ${kind}`;
  verdict.textContent = text;
  buttons.replaceWith(verdict);
}

let streaming = null; // ответ, который сейчас набирается: { bubble, text }
let sentAt = 0;

function streamDelta(text) {
  if (!streaming) {
    addMessage("assistant", "");
    streaming = { bubble: els.messages.lastElementChild.querySelector(".bubble"), text: "" };
  }
  streaming.text += text;
  streaming.bubble.innerHTML = markdown(streaming.text);
  els.thinking.hidden = true;
  scrollDown();
}

function finishAnswer(text) {
  let bubble;
  if (streaming) {
    bubble = streaming.bubble;
    if (text) bubble.innerHTML = markdown(text);
    streaming = null;
  } else if (text) {
    addMessage("assistant", text);
    bubble = els.messages.lastElementChild.querySelector(".bubble");
  }
  if (bubble && sentAt) {
    const meta = document.createElement("div");
    meta.className = "elapsed";
    meta.textContent = `⏱ ${((performance.now() - sentAt) / 1000).toFixed(1)} с`;
    bubble.after(meta);
    sentAt = 0;
  }
}

let busy = false;
function setBusy(value) {
  busy = value;
  els.thinking.hidden = !value;
  els.send.disabled = value;
  if (value) scrollDown();
}

// --- соединение ---
let ws = null;
let retry = 0;
const outbox = [];

function send(payload) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(payload));
  else outbox.push(payload);
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const q = new URLSearchParams({ session: sessionId });
  if (token) q.set("token", token);
  ws = new WebSocket(`${proto}://${location.host}/ws?${q}`);
  ws.onopen = () => {
    retry = 0;
    els.status.className = "status online";
    els.status.title = "Подключено";
    while (outbox.length) ws.send(JSON.stringify(outbox.shift()));
  };
  ws.onclose = (e) => {
    if (e.code === 1008) { showGate(); return; }  // сервер отклонил: нет или неверный токен
    els.status.className = "status offline";
    els.status.title = "Нет соединения — переподключаюсь…";
    setBusy(false);
    setTimeout(connect, Math.min(1000 * 2 ** retry++, 15000));
  };
  ws.onmessage = (e) => handle(JSON.parse(e.data));
}

function handle(ev) {
  switch (ev.type) {
    case "history":
      streaming = null;
      els.messages.replaceChildren();
      for (const m of ev.messages) {
        if (m.role === "tool") addTool(m.name, m.arguments, m.content);
        else addMessage(m.role, m.content);
      }
      updateEmpty();
      setBusy(!!ev.busy);
      break;
    case "busy":
      setBusy(ev.value);
      break;
    case "delta":
      streamDelta(ev.text);
      break;
    case "tool":
      if (streaming && !streaming.text.trim()) streaming.bubble.parentElement.remove();
      streaming = null;
      addTool(ev.name, ev.arguments, ev.result);
      if (busy) els.thinking.hidden = false;
      break;
    case "approval":
      addApproval(ev);
      break;
    case "approval_expired": {
      const card = els.messages.querySelector(`.approval[data-id="${CSS.escape(ev.id)}"]`);
      if (card) setVerdict(card, "no", "⌛ Время на ответ истекло — отклонено");
      break;
    }
    case "answer":
      finishAnswer(ev.text);
      break;
    case "error":
      streaming = null;
      addMessage("error", ev.text);
      break;
  }
}

// --- ввод ---
function autosize() {
  els.input.style.height = "auto";
  els.input.style.height = Math.min(els.input.scrollHeight, 200) + "px";
}

function submit(text) {
  text = text.trim();
  if (!text || busy) return;
  addMessage("user", text);
  sentAt = performance.now();
  send({ type: "message", text });
  setBusy(true);
  els.input.value = "";
  autosize();
}

els.form.addEventListener("submit", (e) => { e.preventDefault(); submit(els.input.value); });
els.input.addEventListener("input", autosize);
els.input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    submit(els.input.value);
  }
});
document.querySelectorAll(".suggestions .chip").forEach((b) => b.addEventListener("click", () => submit(b.textContent)));

$("#btn-new").addEventListener("click", () => {
  if (busy) return;
  send({ type: "reset" });
  els.input.focus();
});

// --- голосовой ввод: запись в браузере → локальный Whisper на сервере → текст в поле ---
const mic = { btn: $("#mic"), time: $("#mic .mic-time"), rec: null, chunks: [], started: 0, timer: 0, cancelled: false };
const MAX_RECORD_MS = 5 * 60 * 1000;

function pickMime() {
  for (const type of ["audio/webm;codecs=opus", "audio/ogg;codecs=opus", "audio/mp4", "audio/webm"]) {
    if (window.MediaRecorder && MediaRecorder.isTypeSupported(type)) return type;
  }
  return "";
}

function micState(state) {
  mic.btn.classList.toggle("recording", state === "recording");
  mic.btn.classList.toggle("busy", state === "busy");
  mic.btn.disabled = state === "busy";
  mic.btn.title = {
    recording: "Идёт запись — нажмите ещё раз или Enter, чтобы закончить; Esc — отменить",
    busy: "Распознаю…",
  }[state] || "Голосовой ввод: нажмите и говорите";
  if (state !== "recording") mic.time.textContent = state === "busy" ? "…" : "";
}

async function startRecording() {
  if (!navigator.mediaDevices || !window.MediaRecorder) {
    addMessage("error", "Микрофон недоступен в этом браузере. Голосовой ввод работает на http://127.0.0.1 или по https.");
    return;
  }
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (e) {
    addMessage("error", "Нет доступа к микрофону: разрешите его для этой страницы в настройках браузера.");
    return;
  }
  const mimeType = pickMime();
  mic.rec = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
  mic.chunks = [];
  mic.cancelled = false;
  mic.rec.ondataavailable = (e) => { if (e.data.size) mic.chunks.push(e.data); };
  mic.rec.onstop = () => {
    stream.getTracks().forEach((t) => t.stop());
    clearInterval(mic.timer);
    const type = mic.rec.mimeType || mimeType || "audio/webm";
    mic.rec = null;
    if (mic.cancelled || !mic.chunks.length) return micState("idle");
    transcribe(new Blob(mic.chunks, { type }));
  };
  mic.rec.start();
  mic.started = performance.now();
  micState("recording");
  mic.timer = setInterval(() => {
    const ms = performance.now() - mic.started;
    const sec = Math.floor(ms / 1000);
    mic.time.textContent = `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, "0")}`;
    if (ms > MAX_RECORD_MS) stopRecording();
  }, 250);
}

function stopRecording(cancel = false) {
  if (!mic.rec) return;
  mic.cancelled = cancel;
  mic.rec.stop();
}

async function transcribe(blob) {
  micState("busy");
  const ext = blob.type.includes("mp4") ? "m4a" : blob.type.includes("ogg") ? "ogg" : "webm";
  const form = new FormData();
  form.append("audio", blob, `voice.${ext}`);
  try {
    const { text } = await api("/api/transcribe", { method: "POST", body: form });
    if (!text) {
      addMessage("error", "Не разобрал речь — попробуйте ещё раз, ближе к микрофону.");
    } else {
      const current = els.input.value.trim();
      els.input.value = current ? `${current} ${text}` : text;
      autosize();
      els.input.focus();
      els.input.setSelectionRange(els.input.value.length, els.input.value.length);
    }
  } catch (e) {
    addMessage("error", `Голосовой ввод: ${e.message}`);
  } finally {
    micState("idle");
  }
}

mic.btn.addEventListener("click", () => (mic.rec ? stopRecording() : startRecording()));
// во время записи Enter и Esc управляют записью, а не отправляют сообщение (перехват до поля ввода)
document.addEventListener("keydown", (e) => {
  if (!mic.rec || (e.key !== "Escape" && e.key !== "Enter")) return;
  e.preventDefault();
  e.stopImmediatePropagation();
  stopRecording(e.key === "Escape");
}, true);

// --- боковая панель: память и документы ---
function openDrawer(tab) {
  els.drawer.hidden = false;
  els.scrim.hidden = false;
  document.querySelectorAll(".tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === tab)));
  document.querySelectorAll(".panel").forEach((p) => { p.hidden = p.dataset.panel !== tab; });
  if (tab === "memory") loadMemory();
  else if (tab === "tasks") loadTasks();
  else loadDocs();
}
function closeDrawer() {
  els.drawer.hidden = true;
  els.scrim.hidden = true;
}
$("#btn-memory").addEventListener("click", () => openDrawer("memory"));
$("#btn-docs").addEventListener("click", () => openDrawer("docs"));
$("#btn-tasks").addEventListener("click", () => openDrawer("tasks"));
$("#drawer-close").addEventListener("click", closeDrawer);
els.scrim.addEventListener("click", closeDrawer);
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !els.drawer.hidden) closeDrawer(); });
document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => openDrawer(b.dataset.tab)));

function listItem(text, meta, onDelete) {
  const li = document.createElement("li");
  const t = document.createElement("span");
  t.className = "text";
  t.textContent = text;
  const m = document.createElement("span");
  m.className = "meta";
  m.textContent = meta;
  const del = document.createElement("button");
  del.title = "Удалить";
  del.textContent = "✕";
  del.onclick = onDelete;
  li.append(t, m, del);
  return li;
}

function emptyItem(text) {
  const li = document.createElement("li");
  li.className = "empty-item";
  li.textContent = text;
  return li;
}

async function loadMemory() {
  const list = $("#memory-list");
  try {
    const items = await api("/api/memory");
    list.replaceChildren(...(items.length
      ? items.slice().reverse().map((m) => listItem(m.text, `#${m.id}`, async () => { await api(`/api/memory/${m.id}`, { method: "DELETE" }); loadMemory(); }))
      : [emptyItem("Пока ничего не запомнено")]));
  } catch (e) {
    list.replaceChildren(emptyItem(e.message));
  }
}

$("#memory-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("#memory-input");
  if (!input.value.trim()) return;
  try {
    await api("/api/memory", { method: "POST", json: { fact: input.value } });
    input.value = "";
    loadMemory();
  } catch (err) {
    alert(err.message);
  }
});

function formatSize(n) {
  if (n < 1024) return `${n} Б`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} КБ`;
  return `${(n / 1024 / 1024).toFixed(1)} МБ`;
}

async function loadDocs() {
  const list = $("#docs-list");
  try {
    const files = await api("/api/docs");
    list.replaceChildren(...(files.length
      ? files.map((f) => listItem(f.name, formatSize(f.size), async () => {
          if (!confirm(`Удалить ${f.name} из папки документов?`)) return;
          await api(`/api/docs/${encodeURIComponent(f.name)}`, { method: "DELETE" });
          loadDocs();
        }))
      : [emptyItem("Документов пока нет")]));
  } catch (e) {
    list.replaceChildren(emptyItem(e.message));
  }
}

async function reindex() {
  const status = $("#docs-status");
  status.textContent = "Индексирую…";
  try {
    const { result } = await api("/api/docs/reindex", { method: "POST" });
    status.textContent = result.replace(/^Папка: [^.]+\.\s*/, "");
  } catch (e) {
    status.textContent = e.message;
  }
}

async function upload(fileList) {
  if (!fileList.length) return;
  const form = new FormData();
  for (const f of fileList) form.append("files", f);
  const status = $("#docs-status");
  status.textContent = "Загружаю…";
  try {
    await api("/api/docs", { method: "POST", body: form });
    await loadDocs();
    await reindex();
  } catch (e) {
    status.textContent = e.message;
  }
}

const dropzone = $("#dropzone");
$("#doc-files").addEventListener("change", (e) => { upload(e.target.files); e.target.value = ""; });
["dragenter", "dragover"].forEach((t) => dropzone.addEventListener(t, (e) => { e.preventDefault(); dropzone.classList.add("over"); }));
["dragleave", "drop"].forEach((t) => dropzone.addEventListener(t, () => dropzone.classList.remove("over")));
dropzone.addEventListener("drop", (e) => { e.preventDefault(); upload(e.dataTransfer.files); });
$("#reindex").addEventListener("click", reindex);

// --- задачи по расписанию ---
function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  const today = new Date();
  const time = d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  if (d.toDateString() === today.toDateString()) return `сегодня ${time}`;
  const tomorrow = new Date(today.getTime() + 864e5);
  if (d.toDateString() === tomorrow.toDateString()) return `завтра ${time}`;
  return `${d.toLocaleDateString("ru-RU", { day: "numeric", month: "short" })} ${time}`;
}

function taskCard(t) {
  const li = document.createElement("li");
  li.className = "task" + (t.enabled ? "" : " off");
  const head = document.createElement("div");
  head.className = "task-head";
  const title = document.createElement("div");
  title.className = "task-title";
  const name = document.createElement("b");
  name.textContent = t.name;
  const when = document.createElement("div");
  when.className = "task-when";
  when.textContent = t.when + (t.chat_id ? " · в Telegram" : "");
  const next = document.createElement("div");
  next.className = "task-next";
  next.textContent = t.enabled ? `Следующий запуск: ${fmtTime(t.next_run)}` : "Выключена";
  title.append(name, when, next);
  const del = document.createElement("button");
  del.className = "del";
  del.title = "Удалить задачу";
  del.textContent = "✕";
  del.onclick = async () => {
    if (!confirm(`Удалить задачу «${t.name}»?`)) return;
    await api(`/api/schedule/${t.id}`, { method: "DELETE" });
    loadTasks();
  };
  head.append(title, del);

  const prompt = document.createElement("div");
  prompt.className = "task-prompt";
  prompt.textContent = t.prompt;

  const actions = document.createElement("div");
  actions.className = "task-actions";
  const run = document.createElement("button");
  run.textContent = "▶ Запустить сейчас";
  run.onclick = async () => {
    run.disabled = true;
    run.textContent = "Выполняется…";
    try { await api(`/api/schedule/${t.id}/run`, { method: "POST" }); }
    catch (e) { alert(e.message); }
    loadTasks();
  };
  const toggle = document.createElement("label");
  const box = document.createElement("input");
  box.type = "checkbox";
  box.checked = t.enabled;
  box.onchange = async () => { await api(`/api/schedule/${t.id}`, { method: "PATCH", json: { enabled: box.checked } }); loadTasks(); };
  toggle.append(box, document.createTextNode("включена"));
  actions.append(run, toggle);
  li.append(head, prompt, actions);

  if (t.history && t.history.length) {
    const last = t.history[0];
    const det = document.createElement("details");
    const sum = document.createElement("summary");
    sum.textContent = `${last.ok ? "✓" : "✕"} Последний результат · ${fmtTime(last.at)}`;
    const body = document.createElement("div");
    body.className = "result" + (last.ok ? "" : " bad");
    body.textContent = last.text;
    det.append(sum, body);
    for (const f of last.files || []) det.append(fileCard(f, "отчёт"));
    if (last.tools && last.tools.length) {
      const used = document.createElement("div");
      used.className = "task-next";
      used.textContent = "Инструменты: " + last.tools.map((x) => x.name).join(", ");
      used.title = last.tools.map((x) => `${x.name} ${JSON.stringify(x.arguments)}`).join("\n");
      det.append(used);
    }
    li.append(det);
  }
  return li;
}

let tasksTimer = 0;
async function loadTasks() {
  const list = $("#task-list");
  clearTimeout(tasksTimer);
  try {
    const tasks = await api("/api/schedule");
    list.replaceChildren(...(tasks.length ? tasks.map(taskCard) : [emptyItem("Задач пока нет")]));
  } catch (e) {
    list.replaceChildren(emptyItem(e.message));
  }
  // пока панель открыта — обновляем: задача могла выполниться
  if (!els.drawer.hidden && !$('[data-panel="tasks"]').hidden) tasksTimer = setTimeout(loadTasks, 30000);
}

document.querySelectorAll(".preset").forEach((b) => b.addEventListener("click", () => { $("#task-when").value = b.dataset.when; }));
$("#task-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const status = $("#task-status");
  try {
    const t = await api("/api/schedule", { method: "POST", json: {
      name: $("#task-name").value, when: $("#task-when").value, prompt: $("#task-prompt").value,
    } });
    status.textContent = `Поставлена: ${t.when}`;
    $("#task-form").reset();
    loadTasks();
  } catch (err) {
    status.textContent = err.message;
  }
});

// --- живая панель браузера агента ---
const br = {
  panel: $("#browser"), img: $("#br-img"), empty: $("#br-empty"), view: $("#br-view"),
  url: $("#br-url"), agent: $("#br-agent"), ws: null, retry: 0, frameUrl: null,
  userClosed: false, // пользователь сам закрыл панель — не открывать её автоматически
};

function showBrowser(open) {
  br.panel.hidden = !open;
  $("#btn-browser").setAttribute("aria-pressed", String(open));
  store.set("lm-agent-browser-open", open ? "1" : "0");
}

function brSend(payload) {
  if (br.ws && br.ws.readyState === WebSocket.OPEN) br.ws.send(JSON.stringify(payload));
}

function connectBrowser() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const q = new URLSearchParams();
  if (token) q.set("token", token);
  br.ws = new WebSocket(`${proto}://${location.host}/ws/browser?${q}`);
  br.ws.binaryType = "blob";
  br.ws.onopen = () => { br.retry = 0; };
  br.ws.onclose = (e) => {
    if (e.code === 1008) return; // браузер отключён на сервере
    setTimeout(connectBrowser, Math.min(1000 * 2 ** br.retry++, 15000));
  };
  br.ws.onmessage = (e) => {
    if (typeof e.data !== "string") return showFrame(e.data);
    const ev = JSON.parse(e.data);
    if (ev.type === "meta") {
      if (document.activeElement !== br.url) br.url.value = ev.url === "about:blank" ? "" : ev.url;
      br.agent.hidden = !ev.agent;
      br.panel.classList.toggle("agent", !!ev.agent);
      if (ev.agent && br.panel.hidden && !br.userClosed) showBrowser(true);
    } else if (ev.type === "error") {
      addMessage("error", `Браузер: ${ev.text}`);
    }
  };
}

function showFrame(blob) {
  const url = URL.createObjectURL(blob);
  br.img.onload = () => { if (br.frameUrl) URL.revokeObjectURL(br.frameUrl); br.frameUrl = url; };
  br.img.src = url;
  br.img.hidden = false;
  br.empty.hidden = true;
}

// координаты клика на картинке → координаты страницы
function pagePoint(e) {
  const rect = br.img.getBoundingClientRect();
  return {
    x: Math.round((e.clientX - rect.left) * br.img.naturalWidth / rect.width),
    y: Math.round((e.clientY - rect.top) * br.img.naturalHeight / rect.height),
  };
}

br.img.addEventListener("click", (e) => {
  br.view.focus({ preventScroll: true });
  brSend({ type: "click", ...pagePoint(e) });
});

// печать: обычные символы копим и отправляем пачкой, служебные клавиши — сразу
const SPECIAL_KEYS = new Set(["Enter", "Backspace", "Tab", "Escape", "Delete", "ArrowUp", "ArrowDown",
  "ArrowLeft", "ArrowRight", "Home", "End", "PageUp", "PageDown"]);
let typed = "", typeTimer = 0;
function flushTyped() {
  if (typed) brSend({ type: "type", text: typed });
  typed = "";
}
br.view.addEventListener("keydown", (e) => {
  if (e.metaKey || e.ctrlKey) return; // Cmd+V и прочее обрабатываем отдельно
  if (e.key.length === 1) {
    typed += e.key;
    clearTimeout(typeTimer);
    typeTimer = setTimeout(flushTyped, 120);
  } else if (SPECIAL_KEYS.has(e.key)) {
    flushTyped();
    brSend({ type: "key", key: e.key });
  } else {
    return;
  }
  e.preventDefault();
});
br.view.addEventListener("paste", (e) => {
  const text = e.clipboardData.getData("text");
  if (text) { flushTyped(); brSend({ type: "type", text }); }
  e.preventDefault();
});

let wheelDy = 0, wheelTimer = 0;
br.view.addEventListener("wheel", (e) => {
  if (br.img.hidden) return;
  e.preventDefault();
  wheelDy += e.deltaY;
  if (!wheelTimer) wheelTimer = setTimeout(() => {
    brSend({ type: "scroll", dy: Math.round(wheelDy) });
    wheelDy = 0;
    wheelTimer = 0;
  }, 120);
}, { passive: false });

$("#br-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const url = br.url.value.trim();
  if (url) brSend({ type: "navigate", url });
  br.url.blur();
});
$("#br-back").addEventListener("click", () => brSend({ type: "back" }));
$("#br-close").addEventListener("click", () => { br.userClosed = true; showBrowser(false); });
$("#btn-browser").addEventListener("click", () => {
  const open = br.panel.hidden;
  br.userClosed = !open;
  showBrowser(open);
  if (open) br.url.focus();
});

// --- старт ---
api("/api/info")
  .then((info) => {
    els.model.textContent = info.model;
    $("#docs-dir").textContent = info.docs_dir;
    if (!info.memory) $("#btn-memory").hidden = true;
    if (!info.docs) $("#btn-docs").hidden = true;
    mic.btn.hidden = !info.voice;
    $("#tg-note").textContent = info.telegram ? " — в веб-чате и в Telegram" : " — здесь, во вкладке «Задачи» (Telegram не подключён)";
    if (info.browser) {
      connectBrowser();
      if (store.get("lm-agent-browser-open") === "1") showBrowser(true);
    } else {
      $("#btn-browser").hidden = true;
    }
    if (info.auto_approve) els.model.textContent += " · без подтверждений";
  })
  .catch((e) => { if (!gateShown) addMessage("error", `Сервер недоступен: ${e.message}`); });
connect();
els.input.focus();
