// CORS proxy for an OpenAI-compatible API, so a browser tab can call it.
//
// Forwards every request to UPSTREAM (default https://api.darkbloom.dev) and
// adds CORS headers. It is not an open proxy: the upstream is fixed, and the
// caller's own Authorization header is passed through, so the proxy holds no
// credentials. Set ALLOWED_ORIGINS to a comma-separated list to restrict which
// pages may use it; unset allows any origin.

export default {
  async fetch(request, env) {
    const upstream = (env.UPSTREAM || "https://api.darkbloom.dev").replace(/\/+$/, "");
    const origin = request.headers.get("Origin") || "";
    const allowed = (env.ALLOWED_ORIGINS || "").split(",").map((s) => s.trim()).filter(Boolean);
    if (allowed.length && !allowed.includes(origin)) return new Response("Origin not allowed", { status: 403 });

    const cors = {
      "Access-Control-Allow-Origin": origin || "*",
      "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
      "Access-Control-Allow-Headers": "Authorization, Content-Type",
      "Access-Control-Max-Age": "86400",
      Vary: "Origin",
    };
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: cors });

    const url = new URL(request.url);
    const headers = new Headers();
    for (const h of ["Authorization", "Content-Type", "Accept"]) {
      const v = request.headers.get(h);
      if (v) headers.set(h, v);
    }
    const res = await fetch(upstream + url.pathname + url.search, {
      method: request.method,
      headers,
      body: ["GET", "HEAD"].includes(request.method) ? undefined : request.body,
    });
    const out = new Headers(res.headers);
    for (const [k, v] of Object.entries(cors)) out.set(k, v);
    return new Response(res.body, { status: res.status, headers: out });
  },
};
