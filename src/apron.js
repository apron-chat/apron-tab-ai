// Minimal Apron Chat Protocol client (PROTOCOL.md v6) for a bot.
//
// Handles framing, request/reply matching, auth, ping, reconnects, and keeps
// just enough state for a bot: its identity, user names, joined rooms, and a
// short scrollback per room.

const PROTOCOL = 6;
const CLIENT = "apron-tab-ai/0.1";

export class ApronError extends Error {
  constructor(error) {
    super(error.message || `Error ${error.code}`);
    this.code = error.code;
    this.data = error.data;
  }
}

export class ApronClient extends EventTarget {
  constructor({ url, scheme = "token", token = "", name = "", scrollback = 50 }) {
    super();
    this.url = url;
    this.scheme = scheme;
    this.token = token;
    this.name = name;
    this.scrollback = scrollback;

    this.ws = null;
    this.server = null; // latest `server` params
    this.you = null; // our identity
    this.users = new Map(); // user_id -> merged current user object
    this.rooms = new Map(); // room_id -> room record (joined rooms)
    this.messages = new Map(); // room_id -> [snapshot], ascending, latest per message_id
    this.pending = new Map(); // request id -> {resolve, reject, method}
    this.stopped = true;
    this.backoff = 1000;
    this.pingTimer = null;
    this.reconnectTimer = null;
  }

  get caps() {
    return new Set(this.server?.caps || []);
  }

  start() {
    this.stopped = false;
    this.connect();
  }

  stop() {
    this.stopped = true;
    clearTimeout(this.reconnectTimer);
    clearInterval(this.pingTimer);
    const ws = this.ws;
    this.ws = null;
    ws?.close();
    this.emit("status", { state: "stopped" });
  }

  emit(type, detail) {
    this.dispatchEvent(new CustomEvent(type, { detail }));
  }

  log(dir, frame) {
    this.emit("frame", { dir, frame });
  }

  connect() {
    const ws = new WebSocket(this.url);
    this.ws = ws;
    this.emit("status", { state: "connecting" });

    ws.onopen = () => {
      this.backoff = 1000;
      // `auth` is a barrier (§3.2): pipeline the room listing right behind it.
      const auth = { scheme: this.scheme, client: CLIENT };
      if (this.token) auth.token = this.token;
      if (this.name) auth.name = this.name;
      this.request("auth", auth)
        .then((r) => this.onAuthed(r))
        .catch((e) => {
          this.emit("status", { state: "error", error: e });
          if (e.code === -32001) this.stop(); // denied: wait for the user (§1.1)
        });
    };

    ws.onmessage = (ev) => {
      let frame;
      try {
        frame = JSON.parse(ev.data);
      } catch {
        return;
      }
      this.log("in", frame);
      this.onFrame(frame);
    };

    ws.onclose = () => {
      clearInterval(this.pingTimer);
      for (const p of this.pending.values()) p.reject(new ApronError({ code: -1, message: "Disconnected" }));
      this.pending.clear();
      if (this.ws !== ws) return;
      this.ws = null;
      this.emit("status", { state: "disconnected" });
      if (!this.stopped) {
        this.reconnectTimer = setTimeout(() => this.connect(), this.backoff);
        this.backoff = Math.min(this.backoff * 2, 30000);
      }
    };
  }

  send(frame) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) throw new Error("Not connected");
    this.log("out", frame);
    this.ws.send(JSON.stringify(frame));
  }

  request(method, params = {}) {
    const id = "b" + crypto.randomUUID().replaceAll("-", "").slice(0, 16);
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject, method });
      try {
        this.send({ method, id, params });
      } catch (e) {
        this.pending.delete(id);
        reject(e);
      }
    });
  }

  notify(method, params = {}) {
    this.send({ method, params });
  }

  async onAuthed(result) {
    this.you = result.you;
    this.mergeUser(result.you);
    this.emit("status", { state: "connected", you: this.you });
    if (this.caps.has("rooms")) {
      const list = await this.request("room_list", { filter: "joined", members: true });
      for (const u of list.users || []) this.mergeUser(u);
      for (const r of list.joined || []) this.installRoom(r);
      this.emit("rooms");
    }
    this.emit("ready");
  }

  onFrame(frame) {
    if (frame.id !== undefined && ("result" in frame || "error" in frame)) {
      const p = this.pending.get(frame.id);
      if (!p) return;
      this.pending.delete(frame.id);
      if (frame.error) p.reject(new ApronError(frame.error));
      else p.resolve(frame.result || {});
      return;
    }
    if (frame.error) {
      this.emit("status", { state: "error", error: new ApronError(frame.error) });
      return;
    }
    const p = frame.params || {};
    switch (frame.method) {
      case "server":
        this.server = p;
        clearInterval(this.pingTimer);
        if (p.ping > 0) {
          this.pingTimer = setInterval(() => {
            if (this.ws?.readyState === WebSocket.OPEN) this.ws.send('{"method":"ping"}');
          }, p.ping * 1000);
        }
        this.emit("server", p);
        break;
      case "user":
        if (p.you) {
          this.you = { ...this.you, ...p.you };
          this.mergeUser(p.you);
        }
        if (p.new) this.mergeUser(p.new);
        break;
      case "room_update":
        for (const u of p.users || []) this.mergeUser(u);
        for (const r of p.joined || []) this.installRoom(r);
        for (const r of p.updated || []) if (this.rooms.has(r.room_id)) this.installRoom(r);
        for (const r of p.left || []) this.rooms.delete(r.room_id);
        this.emit("rooms");
        break;
      case "message":
        if (p.from) this.fallbackUser(p.from);
        if (p.message_id) this.installMessage(p);
        this.emit("message", p);
        break;
      case "reactions":
      case "membership":
      case "activity":
        this.emit(frame.method, p);
        break;
    }
  }

  mergeUser(u) {
    if (!u?.user_id) return;
    const kept = { ...(this.users.get(u.user_id) || {}) };
    for (const [k, v] of Object.entries(u)) {
      if (v === "" || (v && typeof v === "object" && !Object.keys(v).length)) delete kept[k];
      else kept[k] = v;
    }
    this.users.set(u.user_id, kept);
  }

  // Recorded objects only fill in users we know nothing about (§3.3).
  fallbackUser(u) {
    if (u?.user_id && !this.users.has(u.user_id)) this.users.set(u.user_id, { ...u });
  }

  displayName(u) {
    const kept = this.users.get(u?.user_id);
    return kept?.name || u?.name || u?.user_id || "?";
  }

  installRoom(r) {
    const { members, ...record } = r;
    const prev = this.rooms.get(r.room_id);
    record.members = members ? members.map((m) => m.user_id) : prev?.members || [];
    for (const m of members || []) this.fallbackUser(m);
    this.rooms.set(r.room_id, record);
  }

  roomTitle(room_id) {
    return this.rooms.get(room_id)?.title || room_id;
  }

  // Keep the snapshot with the greatest log_id per message (§2).
  installMessage(m) {
    const list = this.messages.get(m.room_id) || [];
    const i = list.findIndex((x) => x.message_id === m.message_id);
    if (i >= 0) {
      if (BigInt(list[i].log_id || 0) > BigInt(m.log_id || 0)) return;
      list[i] = m;
    } else {
      list.push(m);
      list.sort((a, b) => Number(BigInt(a.message_id) - BigInt(b.message_id)));
      if (list.length > this.scrollback) list.splice(0, list.length - this.scrollback);
    }
    // A move re-homes the message (§4.2).
    if (m.prev_room_id && m.prev_room_id !== m.room_id) {
      const old = this.messages.get(m.prev_room_id);
      const j = old?.findIndex((x) => x.message_id === m.message_id) ?? -1;
      if (j >= 0) old.splice(j, 1);
    }
    this.messages.set(m.room_id, list);
  }

  findMessage(message_id) {
    for (const list of this.messages.values()) {
      const m = list.find((x) => x.message_id === message_id);
      if (m) return m;
    }
    return null;
  }

  // Fill a room's scrollback from history, if the server has it (§4.1).
  async loadHistory(room_id, limit = 30) {
    if (!this.caps.has("history")) return [];
    const params = { limit };
    if (room_id) params.room_id = room_id;
    const page = await this.request("history", params);
    for (const m of page.messages || []) {
      if (m.from) this.fallbackUser(m.from);
      this.installMessage(m);
    }
    return page.messages || [];
  }
}
