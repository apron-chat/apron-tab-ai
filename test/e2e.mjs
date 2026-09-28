// End-to-end: reference Python server + mock LLM + the page in headless Chromium.
// Run: node test/e2e.mjs   (needs uv, and playwright on NODE_PATH or installed)
import { spawn } from "node:child_process";
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { chromium } from "playwright";
import { startMockLLM } from "./mock-llm.mjs";

const root = path.resolve(path.dirname(new URL(import.meta.url).pathname), "..");
const serverPy = process.env.APRON_SERVER || path.resolve(root, "../apron/servers/python-minimal/apron_server.py");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function serveStatic() {
  const types = { ".html": "text/html", ".js": "text/javascript" };
  const s = http.createServer((req, res) => {
    const p = path.join(root, decodeURIComponent(new URL(req.url, "http://x").pathname));
    const f = fs.existsSync(p) && fs.statSync(p).isDirectory() ? path.join(p, "index.html") : p;
    if (!f.startsWith(root) || !fs.existsSync(f)) return res.writeHead(404).end();
    res.writeHead(200, { "Content-Type": types[path.extname(f)] || "text/plain" }).end(fs.readFileSync(f));
  });
  return new Promise((r) => s.listen(0, "127.0.0.1", () => r({ s, port: s.address().port })));
}

async function human(url, name) {
  const ws = new WebSocket(url);
  const frames = [];
  ws.onmessage = (e) => frames.push(JSON.parse(e.data));
  await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });
  ws.send(JSON.stringify({ method: "auth", id: "a", params: { scheme: "guest", name } }));
  return { ws, frames, send: (f) => ws.send(JSON.stringify(f)) };
}

async function waitFor(fn, ms = 10000) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    const v = fn();
    if (v) return v;
    await sleep(50);
  }
  throw new Error("timeout");
}

async function scenario(name, { rejectTools, mode }) {
  const port = 20000 + Math.floor(Math.random() * 20000);
  const apron = spawn("uv", ["run", "-q", serverPy, "--host", "127.0.0.1", "--port", String(port)], { stdio: ["ignore", "ignore", "inherit"], detached: true });
  const llm = await startMockLLM({ rejectTools });
  const web = await serveStatic();
  const browser = await chromium.launch();
  try {
    const wsUrl = `ws://127.0.0.1:${port}`;
    let alice;
    for (let i = 0; ; i++) {
      try { alice = await human(wsUrl, "Alice"); break; } catch { if (i > 100) throw new Error("server did not start"); await sleep(100); }
    }
    const page = await browser.newPage();
    page.on("pageerror", (e) => console.error("pageerror", e));
    const hash = new URLSearchParams({ apronUrl: wsUrl, scheme: "guest", baseUrl: `http://127.0.0.1:${llm.port}/v1`, model: "mock-1", mode, botName: "Botty" });
    await page.goto(`http://127.0.0.1:${web.port}/#${hash}`);
    await page.click("#toggle");
    const you = await page.waitForFunction(() => window.apronTabAI.client?.you?.user_id).then((h) => h.jsonValue());

    alice.send({ method: "message", id: "m0", params: { body: { text: "just chatting" } } });
    alice.send({ method: "message", id: "m1", params: { body: { text: `@${you} what's up?`, mentions: [you] } } });
    const reply = await waitFor(() => alice.frames.find((f) => f.method === "message" && f.params.from?.user_id === you));
    const mention = alice.frames.find((f) => f.id === "m1").result.message_id;
    const text = reply.params.body.text;
    const expect = rejectTools || mode === "raw" ? "raw saw results" : "tools saw";
    if (!text.startsWith(expect) || !text.includes("what's up?")) throw new Error(`unexpected reply: ${text}`);
    if (reply.params.reply_to?.message_id !== mention) throw new Error("reply_to missing");
    if (text.includes("<think>")) throw new Error("thinking leaked");
    await sleep(300);
    const replies = alice.frames.filter((f) => f.method === "message" && f.params.from?.user_id === you);
    if (replies.length !== 1) throw new Error(`expected 1 reply, got ${replies.length}`);
    console.log(`ok  ${name}: ${JSON.stringify(text)} (${llm.requests.length} LLM calls)`);
    alice.ws.close();
  } catch (e) {
    const pages = browser.contexts().flatMap((c) => c.pages());
    if (pages[0]) console.error(await pages[0].innerText("#log"));
    throw e;
  } finally {
    await browser.close();
    llm.server.close();
    web.s.close();
    process.kill(-apron.pid);
  }
}

const only = process.argv[2];
const scenarios = {
  tools: ["tool calls", { mode: "tools" }],
  raw: ["raw frames", { mode: "raw" }],
  auto: ["auto falls back to raw", { mode: "auto", rejectTools: true }],
};
for (const [key, [name, opts]] of Object.entries(scenarios)) if (!only || only === key) await scenario(name, opts);
process.exit(0);
