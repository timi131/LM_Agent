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
if (params.get("token")) store.set("lm-agent-token", params.get("token"));
const token = store.get("lm-agent-token") || "";
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
  updateEmpty();
  scrollDown();
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
  ws.onclose = () => {
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
    case "tool":
      addTool(ev.name, ev.arguments, ev.result);
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
      addMessage("assistant", ev.text);
      break;
    case "error":
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

// --- боковая панель: память и документы ---
function openDrawer(tab) {
  els.drawer.hidden = false;
  els.scrim.hidden = false;
  document.querySelectorAll(".tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === tab)));
  document.querySelectorAll(".panel").forEach((p) => { p.hidden = p.dataset.panel !== tab; });
  if (tab === "memory") loadMemory();
  else loadDocs();
}
function closeDrawer() {
  els.drawer.hidden = true;
  els.scrim.hidden = true;
}
$("#btn-memory").addEventListener("click", () => openDrawer("memory"));
$("#btn-docs").addEventListener("click", () => openDrawer("docs"));
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

// --- старт ---
api("/api/info")
  .then((info) => {
    els.model.textContent = info.model;
    $("#docs-dir").textContent = info.docs_dir;
    if (!info.memory) $("#btn-memory").hidden = true;
    if (!info.docs) $("#btn-docs").hidden = true;
    if (info.auto_approve) els.model.textContent += " · без подтверждений";
  })
  .catch((e) => addMessage("error", `Сервер недоступен: ${e.message}`));
connect();
els.input.focus();
