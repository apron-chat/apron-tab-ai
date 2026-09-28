// The bot: watches an Apron connection for mentions and answers them with an
// OpenAI-compatible model, either through tool calls or raw protocol frames.

import { chat, stripThinking, LLMError } from "./llm.js";

const NO_REPLY = "NO_REPLY";
const MENTION_RE = /(?<![A-Za-z0-9])@(@?[A-Za-z0-9_.-]*[A-Za-z0-9_])/g; // Appendix A.3
const NOTIFICATIONS = new Set(["activity"]);

export class Bot {
  constructor(client, config, log) {
    this.client = client;
    this.config = config;
    this.log = log;
    this.handled = new Set(); // message_ids already answered
    this.queues = new Map(); // room_id -> promise chain
    this.loadedHistory = new Set(); // rooms whose history we fetched
    this.recent = []; // timestamps of recent responses, for rate limiting
    this.mode = config.mode === "auto" ? null : config.mode; // resolved mode
    this.onMessage = (ev) => this.consider(ev.detail);
  }

  start() {
    this.client.addEventListener("message", this.onMessage);
  }

  stop() {
    this.client.removeEventListener("message", this.onMessage);
  }

  // --- triggering ---------------------------------------------------------

  isTrigger(m) {
    const you = this.client.you;
    if (!you || !m.message_id || m.deleted || !m.from) return false;
    if (m.from.user_id === you.user_id || m.from.user_id.startsWith("@")) return false;
    if (this.handled.has(m.message_id)) return false;
    if ((m.body?.mentions || []).includes(you.user_id)) return "mention";
    if (this.config.matchText && new RegExp(`(?<![A-Za-z0-9])@${escapeRe(you.user_id)}(?![A-Za-z0-9_])`).test(m.body?.text || ""))
      return "text";
    if (this.config.replyToReplies && m.reply_to?.message_id) {
      const target = this.client.findMessage(m.reply_to.message_id);
      if (target?.from?.user_id === you.user_id) return "reply";
    }
    return false;
  }

  consider(m) {
    const why = this.isTrigger(m);
    if (!why) return;
    this.handled.add(m.message_id);
    const now = Date.now();
    this.recent = this.recent.filter((t) => now - t < 60000);
    if (this.recent.length >= (this.config.maxPerMinute || 6)) {
      this.log("warn", `Rate limit reached; ignoring ${m.message_id}`);
      return;
    }
    this.recent.push(now);
    this.log("trigger", `${why} from ${this.client.displayName(m.from)} in ${this.client.roomTitle(m.room_id)}: ${m.body?.text || ""}`);
    const room = m.room_id || "";
    const prev = this.queues.get(room) || Promise.resolve();
    const next = prev.then(() => this.respond(m)).catch((e) => this.log("error", e.message || String(e)));
    this.queues.set(room, next);
  }

  // --- responding ---------------------------------------------------------

  async respond(trigger) {
    const c = this.client;
    const room_id = trigger.room_id;
    const typing = c.caps.has("activity");
    const setTyping = (s) => {
      try {
        if (typing) c.notify("activity", { room_id, typing: s });
      } catch {}
    };
    setTyping(30);
    const keepTyping = setInterval(() => setTyping(30), 20000);
    try {
      if (room_id && !this.loadedHistory.has(room_id) && c.caps.has("history")) {
        this.loadedHistory.add(room_id);
        await c.loadHistory(room_id, this.config.contextMessages || 30).catch((e) => this.log("warn", `history: ${e.message}`));
      }
      const ctx = { trigger, room_id, replied: false };
      let mode = this.mode || "tools";
      try {
        await (mode === "tools" ? this.runTools(ctx) : this.runRaw(ctx));
      } catch (e) {
        // In auto mode, fall back to raw frames if the endpoint rejects tools.
        if (!this.mode && e instanceof LLMError && e.status >= 400 && e.status < 500 && e.status !== 401 && e.status !== 402) {
          this.log("warn", `Tool calling rejected (${e.message}); switching to raw frames`);
          this.mode = "raw";
          await this.runRaw(ctx);
        } else throw e;
      }
      if (!this.mode) this.mode = mode;
    } finally {
      clearInterval(keepTyping);
      setTyping(0);
    }
  }

  llm(messages, tools) {
    const { baseUrl, apiKey, model, temperature, maxTokens } = this.config;
    this.log("llm", `→ ${model} (${messages.length} messages${tools ? `, ${tools.length} tools` : ""})`, messages);
    return chat({ baseUrl, apiKey, model, messages, tools, temperature, maxTokens }).then((r) => {
      this.log("llm", `← ${r.message.tool_calls?.length ? `${r.message.tool_calls.length} tool call(s)` : "text"}`, r);
      return r.message;
    });
  }

  async runTools(ctx) {
    const tools = this.toolDefs();
    const messages = [
      { role: "system", content: this.systemPrompt("tools") },
      { role: "user", content: this.contextPrompt(ctx.trigger) },
    ];
    for (let step = 0; step < (this.config.maxSteps || 6); step++) {
      const msg = await this.llm(messages, tools);
      messages.push({ role: "assistant", content: msg.content ?? "", ...(msg.tool_calls?.length ? { tool_calls: msg.tool_calls } : {}) });
      if (!msg.tool_calls?.length) {
        await this.finalText(ctx, msg.content);
        return;
      }
      for (const call of msg.tool_calls) {
        let result;
        try {
          const args = call.function.arguments ? JSON.parse(call.function.arguments) : {};
          this.log("tool", `${call.function.name}(${JSON.stringify(args)})`);
          result = await this.runTool(ctx, call.function.name, args);
        } catch (e) {
          result = { error: e.message || String(e), ...(e.code ? { code: e.code } : {}) };
        }
        messages.push({ role: "tool", tool_call_id: call.id, content: JSON.stringify(result ?? {}) });
      }
    }
    this.log("warn", "Step limit reached");
  }

  async runRaw(ctx) {
    const messages = [
      { role: "system", content: this.systemPrompt("raw") },
      { role: "user", content: this.contextPrompt(ctx.trigger) },
    ];
    for (let step = 0; step < (this.config.maxSteps || 6); step++) {
      const msg = await this.llm(messages);
      const content = msg.content || "";
      messages.push({ role: "assistant", content });
      const { frames, prose, errors } = parseFrames(stripThinking(content));
      if (!frames.length && !errors.length) {
        await this.finalText(ctx, prose);
        return;
      }
      const results = errors.map((e) => ({ error: e }));
      for (const f of frames) results.push(await this.runFrame(f));
      // Prose alongside frames is the reply; only loop again when the model
      // needs to see results: nothing said yet, or something failed.
      const failed = results.some((r) => r.error);
      if (prose && prose !== NO_REPLY) await this.finalText(ctx, prose);
      if ((prose && !failed) || prose === NO_REPLY) return;
      messages.push({ role: "user", content: "Results:\n```json\n" + JSON.stringify(results, null, 1) + "\n```" });
    }
    this.log("warn", "Step limit reached");
  }

  async finalText(ctx, text) {
    text = stripThinking(text);
    if (!text || text === NO_REPLY || text.endsWith(NO_REPLY)) {
      this.log("info", "No reply");
      return;
    }
    if (ctx.replied) return; // already posted through a tool; don't double up
    await this.post({ room_id: ctx.room_id, text, reply_to: ctx.trigger.message_id });
    ctx.replied = true;
  }

  async runFrame(f) {
    if (!f || typeof f.method !== "string") return { error: "Each frame needs a string `method`" };
    if (f.method === "auth") return { method: f.method, error: "auth is not allowed" };
    this.log("tool", `raw ${f.method} ${JSON.stringify(f.params || {})}`);
    try {
      if (NOTIFICATIONS.has(f.method)) {
        this.client.notify(f.method, f.params || {});
        return { method: f.method, result: "sent" };
      }
      return { method: f.method, result: trimResult(await this.client.request(f.method, f.params || {})) };
    } catch (e) {
      return { method: f.method, error: e.message, code: e.code };
    }
  }

  // --- actions ------------------------------------------------------------

  // Posts a message, listing the known users it @mentions (§3.5).
  async post({ room_id, text, reply_to, format = "markdown" }) {
    const mentions = [...new Set([...text.matchAll(MENTION_RE)].map((x) => x[1]).filter((id) => this.client.users.has(id)))];
    const params = { body: { text, format, ...(mentions.length ? { mentions } : {}) } };
    if (room_id) params.room_id = room_id;
    if (reply_to) params.reply_to = { message_id: reply_to };
    this.log("send", text);
    return this.client.request("message", params);
  }

  toolDefs() {
    const caps = this.client.caps;
    const fn = (name, description, properties = {}, required = []) => ({
      type: "function",
      function: { name, description, parameters: { type: "object", properties, required } },
    });
    const str = (description) => ({ type: "string", description });
    const defs = [
      fn(
        "send_message",
        "Post a message. Mention people as @user_id in the text. Defaults to the current room, replying to the message that mentioned you.",
        {
          text: str("Message text, Markdown"),
          room_id: str("Room to post in; defaults to the current room"),
          reply_to: str("message_id to reply to; defaults to the triggering message; empty string for none"),
        },
        ["text"],
      ),
      fn("set_name", "Change your display name.", { name: str("New display name") }, ["name"]),
      fn("apron_request", "Send any raw Apron protocol request and get its result. Use when no other tool fits.", {
        method: str("Protocol method, e.g. room_list"),
        params: { type: "object", description: "Request params" },
      }, ["method"]),
    ];
    if (caps.has("history"))
      defs.push(fn("get_history", "Fetch recent messages of a room.", { room_id: str("Room; defaults to the current room"), limit: { type: "integer" } }));
    if (caps.has("reactions"))
      defs.push(fn("react", "Set your emoji reactions on a message (replaces your previous set).", {
        message_id: str("Message to react to"),
        emojis: { type: "array", items: { type: "string" } },
      }, ["message_id", "emojis"]));
    if (caps.has("edit")) {
      defs.push(fn("edit_message", "Replace the text of one of your messages.", { message_id: str("Your message"), text: str("New text") }, ["message_id", "text"]));
      defs.push(fn("delete_message", "Delete one of your messages.", { message_id: str("Your message") }, ["message_id"]));
    }
    if (caps.has("rooms")) {
      defs.push(fn("list_rooms", "List rooms you have joined and rooms you could join."));
      defs.push(fn("join_room", "Join a room.", { room_id: str("Room") }, ["room_id"]));
      defs.push(fn("leave_room", "Leave a room.", { room_id: str("Room") }, ["room_id"]));
    }
    return defs;
  }

  async runTool(ctx, name, a) {
    const c = this.client;
    switch (name) {
      case "send_message": {
        const reply_to = a.reply_to === undefined ? ctx.trigger.message_id : a.reply_to || undefined;
        const r = await this.post({ room_id: a.room_id || ctx.room_id, text: a.text, reply_to });
        if (!a.room_id || a.room_id === ctx.room_id) ctx.replied = true;
        return r;
      }
      case "set_name":
        return c.request("me", { name: a.name });
      case "apron_request":
        return (await this.runFrame({ method: a.method, params: a.params })).result ?? { error: "failed" };
      case "get_history": {
        const msgs = await c.loadHistory(a.room_id || ctx.room_id, Math.min(a.limit || 20, 100));
        return { messages: msgs.map((m) => this.formatMessage(m)) };
      }
      case "react":
        return c.request("reactions", { message_id: a.message_id, emojis: a.emojis });
      case "edit_message":
      case "delete_message": {
        const m = c.findMessage(a.message_id);
        if (!m) return { error: "Unknown message_id (not in scrollback)" };
        const params = { message_id: m.message_id, room_id: m.room_id };
        if (m.reply_to) params.reply_to = { message_id: m.reply_to.message_id };
        if (m.ext) params.ext = m.ext;
        if (name === "delete_message") params.deleted = true;
        else params.body = { ...m.body, text: a.text };
        return c.request("message", params);
      }
      case "list_rooms": {
        const r = await c.request("room_list", {});
        const brief = (x) => ({ room_id: x.room_id, title: x.title, parent_room_id: x.parent_room_id });
        return { joined: (r.joined || []).map(brief), not_joined: (r.not_joined || []).map(brief) };
      }
      case "join_room":
        return c.request("room_join", { room_id: a.room_id });
      case "leave_room":
        return c.request("room_leave", { room_id: a.room_id });
    }
    return { error: `Unknown tool ${name}` };
  }

  // --- prompts ------------------------------------------------------------

  formatMessage(m) {
    const who = `${this.client.displayName(m.from)} (@${m.from?.user_id})`;
    const reply = m.reply_to?.message_id ? ` ↩${m.reply_to.message_id}` : "";
    const embeds = (m.body?.embeds || []).map((e) => ` [${e.kind}${e.title ? `: ${e.title}` : ""}${e.url ? ` ${e.url}` : ""}]`).join("");
    const text = m.deleted ? "(deleted)" : (m.body?.text || "") + embeds;
    return `[${m.message_id}${reply}] ${who}: ${text}`;
  }

  systemPrompt(mode) {
    const c = this.client;
    const you = c.you;
    const rooms = [...c.rooms.values()].map((r) => `${r.room_id} ("${r.title || r.room_id}")`).join(", ");
    const lines = [
      this.config.systemPrompt || "You are a helpful assistant.",
      "",
      `You are connected to an Apron chat server as ${you.name || you.user_id} (user_id "${you.user_id}").`,
      `Server: ${c.server?.name || "unknown"}; capabilities: ${[...c.caps].join(", ") || "core only"}.`,
      rooms ? `Rooms you are in: ${rooms}.` : "",
      "Chat lines look like `[message_id ↩replied_to_id] Name (@user_id): text`. Mention someone by writing @user_id.",
      "Messages support Markdown. Keep replies short and conversational.",
      "",
    ];
    if (mode === "tools") {
      lines.push(
        "Act through the tools. When you are done, your final text is posted as a reply to the message that mentioned you,",
        `unless you already replied with send_message. Answer exactly ${NO_REPLY} to stay silent.`,
      );
    } else {
      lines.push(
        "Your answer text is posted as a reply to the message that mentioned you. Answer exactly " + NO_REPLY + " to stay silent.",
        "To act on the chat, add fenced code blocks tagged `apron`, each holding one JSON request frame {\"method\": ..., \"params\": ...}",
        "or an array of them, from the Apron protocol. They are sent as your own requests.",
        "If you only need information first (e.g. history), send just the blocks without prose: you will get the results and can continue.",
        "If you write prose alongside blocks, the prose is posted and the conversation ends unless a frame failed.",
        "",
        "Useful requests:",
        ...rawCheatSheet(c.caps),
      );
    }
    return lines.filter((l, i, a) => l || a[i - 1]).join("\n");
  }

  contextPrompt(trigger) {
    const c = this.client;
    const n = this.config.contextMessages || 30;
    const scroll = (c.messages.get(trigger.room_id) || []).filter((m) => m.message_id !== trigger.message_id).slice(-n);
    const target = trigger.reply_to?.message_id && !scroll.some((m) => m.message_id === trigger.reply_to.message_id) ? c.findMessage(trigger.reply_to.message_id) : null;
    const parts = [`Room: ${c.roomTitle(trigger.room_id)} (room_id "${trigger.room_id}")`];
    if (target) parts.push("", "Message being replied to:", this.formatMessage(target));
    parts.push("", "Recent messages:", scroll.length ? scroll.map((m) => this.formatMessage(m)).join("\n") : "(none)");
    parts.push("", "New message for you:", this.formatMessage(trigger));
    return parts.join("\n");
  }
}

function rawCheatSheet(caps) {
  const ex = [
    '- post: {"method":"message","params":{"room_id":"R","body":{"text":"hi @bob","format":"markdown","mentions":["bob"]},"reply_to":{"message_id":"M"}}}',
    '- rename yourself: {"method":"me","params":{"name":"New Name"}}',
  ];
  if (caps.has("history")) ex.push('- read a room: {"method":"history","params":{"room_id":"R","limit":20}}');
  if (caps.has("reactions")) ex.push('- react (your complete set): {"method":"reactions","params":{"message_id":"M","emojis":["👍"]}}');
  if (caps.has("edit"))
    ex.push('- edit your message (resend every field you keep): {"method":"message","params":{"message_id":"M","room_id":"R","body":{"text":"..."}}}');
  if (caps.has("rooms"))
    ex.push(
      '- rooms: {"method":"room_list","params":{}}, {"method":"room_join","params":{"room_id":"R"}}, {"method":"room_leave","params":{"room_id":"R"}}',
      '- start a thread: {"method":"room_set","params":{"parent_room_id":"R","title":"Topic","intro_message":{"message_id":"M"}}}',
    );
  if (caps.has("command")) ex.push('- server command: {"method":"command","params":{"room_id":"R","body":{"text":"/help"}}}');
  return ex;
}

// Pulls ```apron blocks out of model output.
export function parseFrames(text) {
  const frames = [];
  const errors = [];
  const prose = text
    .replace(/```apron[^\n]*\n([\s\S]*?)```/g, (_, body) => {
      try {
        const v = JSON.parse(body);
        frames.push(...(Array.isArray(v) ? v : [v]));
      } catch (e) {
        errors.push(`Invalid JSON in apron block: ${e.message}`);
      }
      return "";
    })
    .trim();
  return { frames, prose, errors };
}

function trimResult(r) {
  const s = JSON.stringify(r);
  return s.length > 12000 ? { truncated: s.slice(0, 12000) } : r;
}

function escapeRe(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
