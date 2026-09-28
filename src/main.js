import { ApronClient } from "./apron.js";
import { Bot } from "./bot.js";
import { listModels } from "./llm.js";

const $ = (id) => document.getElementById(id);
const form = $("config");
const STORE = "apron-tab-ai:config";
const SECRETS = ["apiKey", "token"];
const NUMBERS = ["temperature", "maxTokens", "contextMessages", "maxSteps", "maxPerMinute"];

let client = null;
let bot = null;

// --- config ---------------------------------------------------------------

function readForm() {
  const c = {};
  for (const el of form.elements) {
    if (!el.name) continue;
    c[el.name] = el.type === "checkbox" ? el.checked : el.value.trim();
  }
  for (const k of NUMBERS) c[k] = c[k] === "" ? undefined : Number(c[k]);
  return c;
}

function save() {
  const c = readForm();
  if (!c.remember) for (const k of SECRETS) delete c[k];
  try {
    localStorage.setItem(STORE, JSON.stringify(c));
  } catch {}
}

function load() {
  let c = {};
  try {
    c = JSON.parse(localStorage.getItem(STORE) || "{}");
  } catch {}
  // URL hash overrides, e.g. #apronUrl=ws://localhost:8765&scheme=guest
  for (const [k, v] of new URLSearchParams(location.hash.slice(1))) c[k] = v;
  for (const el of form.elements) {
    if (!el.name || !(el.name in c)) continue;
    if (el.type === "checkbox") el.checked = c[el.name] === true || c[el.name] === "true";
    else el.value = c[el.name];
  }
}

// --- log ------------------------------------------------------------------

function log(kind, text, data) {
  if (kind === "frame" && !$("showFrames").checked) return;
  if (kind === "llm" && !$("showLLM").checked) data = undefined;
  const box = $("log");
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  const e = document.createElement("div");
  e.className = `e ${kind}`;
  const t = document.createElement("span");
  t.className = "t";
  t.textContent = new Date().toLocaleTimeString();
  const k = document.createElement("span");
  k.className = "k";
  k.textContent = kind;
  e.append(t, k);
  if (data !== undefined) {
    const d = document.createElement("details");
    const s = document.createElement("summary");
    s.textContent = text;
    const pre = document.createElement("pre");
    pre.textContent = JSON.stringify(data, null, 2);
    d.append(s, pre);
    e.append(d);
  } else e.append(text);
  box.append(e);
  while (box.childElementCount > 2000) box.firstChild.remove();
  if (atBottom) box.scrollTop = box.scrollHeight;
}

function setStatus(html, cls) {
  $("status").innerHTML = "";
  const b = document.createElement("b");
  b.className = cls;
  b.textContent = html;
  $("status").append(b);
}

// --- lifecycle ------------------------------------------------------------

function start() {
  const c = readForm();
  if (!c.apronUrl || !c.baseUrl || !c.model) {
    log("error", "Set the Apron server URL, the LLM base URL, and a model.");
    return;
  }
  save();
  client = new ApronClient({ url: c.apronUrl, scheme: c.scheme, token: c.token, name: c.botName, scrollback: Math.max(50, (c.contextMessages || 0) * 2) });
  bot = new Bot(client, c, log);

  client.addEventListener("frame", (ev) => {
    const { dir, frame } = ev.detail;
    log("frame", `${dir === "in" ? "←" : "→"} ${frame.method || (frame.error ? "error" : "result")} ${frame.id || ""}`, frame);
  });
  client.addEventListener("server", (ev) => log("status", `server ${ev.detail.name || ""} protocol ${ev.detail.protocol}, caps: ${(ev.detail.caps || []).join(", ") || "none"}`));
  client.addEventListener("status", (ev) => {
    const { state, you, error } = ev.detail;
    if (state === "connected") {
      setStatus(`connected as ${you.name || you.user_id} (@${you.user_id})`, "on");
      log("status", `authenticated as @${you.user_id}`);
    } else if (state === "error") {
      setStatus(error.message, "err");
      log("error", error.message);
    } else {
      setStatus(state, "off");
      log("status", state);
    }
  });
  client.addEventListener("ready", async () => {
    for (const room_id of (c.joinRooms || "").split(",").map((s) => s.trim()).filter(Boolean)) {
      if (client.rooms.has(room_id) || !client.caps.has("rooms")) continue;
      await client.request("room_join", { room_id }).then(
        () => log("status", `joined ${room_id}`),
        (e) => log("error", `join ${room_id}: ${e.message}`),
      );
    }
    const rooms = [...client.rooms.values()].map((r) => r.title || r.room_id);
    if (rooms.length) log("status", `in rooms: ${rooms.join(", ")}`);
    log("status", `listening for mentions of @${client.you.user_id}`);
  });

  bot.start();
  client.start();
  $("toggle").textContent = "Stop bot";
  $("toggle").classList.remove("primary");
}

function stop() {
  bot?.stop();
  client?.stop();
  bot = client = null;
  $("toggle").textContent = "Start bot";
  $("toggle").classList.add("primary");
}

form.addEventListener("submit", (e) => {
  e.preventDefault();
  if (client) stop();
  else start();
});
form.addEventListener("change", save);

$("loadModels").addEventListener("click", async () => {
  const c = readForm();
  try {
    const ids = await listModels({ baseUrl: c.baseUrl, apiKey: c.apiKey });
    $("models").replaceChildren(...ids.map((id) => Object.assign(document.createElement("option"), { value: id })));
    log("status", `models: ${ids.join(", ")}`);
    if (!c.model && ids[0]) $("model").value = ids[0];
  } catch (e) {
    log("error", `listing models: ${e.message}${e instanceof TypeError ? " (likely CORS: use a proxy)" : ""}`);
  }
});
$("clearLog").addEventListener("click", () => $("log").replaceChildren());

load();
window.apronTabAI = { get client() { return client; }, get bot() { return bot; }, start, stop };
