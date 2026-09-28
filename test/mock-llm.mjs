// Scripted OpenAI-compatible endpoint for tests. MOCK_TOOLS=reject makes it
// reject requests that carry `tools`, to exercise the raw-frame fallback.
import http from "node:http";

export function startMockLLM({ port = 0, rejectTools = false } = {}) {
  const requests = [];
  const server = http.createServer((req, res) => {
    const cors = { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "*" };
    if (req.method === "OPTIONS") return res.writeHead(204, cors).end();
    const reply = (status, body) => res.writeHead(status, { ...cors, "Content-Type": "application/json" }).end(JSON.stringify(body));
    if (req.url.endsWith("/models")) return reply(200, { data: [{ id: "mock-1" }] });
    let data = "";
    req.on("data", (c) => (data += c));
    req.on("end", () => {
      const body = JSON.parse(data);
      requests.push(body);
      if (body.tools && rejectTools) return reply(400, { error: { message: "tools not supported" } });
      const msgs = body.messages;
      const last = msgs[msgs.length - 1];
      const trigger = msgs[1].content.split("New message for you:\n")[1] || "";
      const said = trigger.replace(/^\[[^\]]*\] [^:]*: /, "");
      let message;
      if (body.tools) {
        message = last.role === "tool"
          ? { role: "assistant", content: `tools saw ${JSON.parse(last.content).messages.length} messages; you said: ${said}` }
          : { role: "assistant", content: null, tool_calls: [{ id: "call_1", type: "function", function: { name: "get_history", arguments: '{"limit":5}' } }] };
      } else {
        message = last.content.startsWith("Results:")
          ? { role: "assistant", content: `<think>hmm</think>raw saw results; you said: ${said}` }
          : { role: "assistant", content: '```apron\n{"method":"history","params":{"limit":5}}\n```' };
      }
      reply(200, { choices: [{ message, finish_reason: "stop" }], usage: {} });
    });
  });
  return new Promise((r) => server.listen(port, "127.0.0.1", () => r({ server, port: server.address().port, requests })));
}
