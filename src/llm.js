// OpenAI-compatible chat completions client (e.g. https://api.darkbloom.dev/v1).

export class LLMError extends Error {
  constructor(message, status, body) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

function endpoint(baseUrl, path) {
  return baseUrl.replace(/\/+$/, "") + path;
}

function headers(apiKey) {
  const h = { "Content-Type": "application/json" };
  if (apiKey) h.Authorization = `Bearer ${apiKey}`;
  return h;
}

export async function listModels({ baseUrl, apiKey }) {
  const res = await fetch(endpoint(baseUrl, "/models"), { headers: headers(apiKey) });
  const body = await res.json().catch(() => null);
  if (!res.ok) throw new LLMError(body?.error?.message || `HTTP ${res.status}`, res.status, body);
  return (body?.data || []).map((m) => m.id);
}

// Returns the assistant message: {role, content, tool_calls?}.
export async function chat({ baseUrl, apiKey, model, messages, tools, temperature, maxTokens, signal }) {
  const req = { model, messages };
  if (tools?.length) req.tools = tools;
  if (temperature !== undefined && temperature !== "") req.temperature = Number(temperature);
  if (maxTokens) req.max_tokens = Number(maxTokens);
  const res = await fetch(endpoint(baseUrl, "/chat/completions"), {
    method: "POST",
    headers: headers(apiKey),
    body: JSON.stringify(req),
    signal,
  });
  const text = await res.text();
  let body = null;
  try {
    body = JSON.parse(text);
  } catch {}
  if (!res.ok) throw new LLMError(body?.error?.message || text.slice(0, 300) || `HTTP ${res.status}`, res.status, body);
  const msg = body?.choices?.[0]?.message;
  if (!msg) throw new LLMError("No choices in response", res.status, body);
  return { message: msg, usage: body.usage };
}

// Some models inline their reasoning; never post it to the chat.
export function stripThinking(text) {
  return (text || "").replace(/<think>[\s\S]*?<\/think>/g, "").replace(/^[\s\S]*<\/think>/, "").trim();
}
