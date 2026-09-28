# apron-tab-ai

An Apron Chat AI bot that runs in a browser tab.

`index.html` is a static page with no build step. Give it an OpenAI-compatible
endpoint (such as [Darkbloom](https://docs.darkbloom.dev)) and an Apron Chat
server, and it connects as a bot, watches for mentions, and answers them with
the model. Everything runs in the tab: close the tab and the bot leaves.

## Run

```sh
python3 -m http.server 8000   # or any static file server
open http://localhost:8000
```

Fill in the form and press **Start bot**. Settings are saved in
`localStorage`; the API key and Apron token are saved only if you tick
"Remember". Any field can also be set from the URL hash, e.g.
`#apronUrl=ws://localhost:8765&scheme=guest&model=...`.

### Darkbloom needs a CORS proxy

`api.darkbloom.dev` only allows browser requests from its own console, so the
page cannot call it directly. `proxy/` is a small Cloudflare Worker that
forwards to Darkbloom and adds CORS headers. It holds no credentials: the
page's `Authorization` header is passed through. Set `ALLOWED_ORIGINS` to
restrict which pages may use it.

```sh
cd proxy && npx wrangler deploy
```

Then use `https://apron-tab-ai-proxy.<you>.workers.dev/v1` as the base URL.
Endpoints that allow CORS, such as a local Ollama or LM Studio server, need no
proxy.

## How it works

- **Protocol** (`src/apron.js`): a small client for
  [PROTOCOL.md](https://github.com/shazow/apron/blob/main/PROTOCOL.md) v6.
  It authenticates (`token` or `guest`), lists joined rooms with cap `rooms`,
  answers `ping`, reconnects with backoff, and keeps user names and a short
  per-room scrollback (newest snapshot per message wins).
- **Triggers** (`src/bot.js`): a message whose `body.mentions` lists the bot,
  a reply to one of the bot's messages, or, optionally, `@user_id` in the
  text. Each message triggers at most once; responses are serialized per room
  and rate-limited.
- **Context**: the system prompt, the bot's identity, server caps and rooms,
  then recent messages in the room (fetched with `history` the first time,
  when the server has it) as `[message_id ↩reply_to] Name (@user_id): text`.
- **Actions**: the model's final text is posted as a reply to the mention,
  with any `@user_id` of a known user added to `mentions`. `NO_REPLY` stays
  silent. While it works the bot shows as typing (cap `activity`). Two ways to
  do more than reply:
  - **Tool calls**: OpenAI function calling with tools for the server's caps:
    `send_message`, `react`, `edit_message`, `delete_message`,
    `get_history`, `list_rooms`, `join_room`, `leave_room`, `set_name`, and
    `apron_request` for any raw protocol request.
  - **Raw frames**: for models or endpoints without tool calling. The model
    writes ` ```apron ` blocks holding JSON request frames, which the page sends
    as its own requests and feeds the results back.
  - **Auto** (default) tries tool calls and switches to raw frames if the
    endpoint rejects the `tools` parameter.
- `<think>…</think>` reasoning is stripped before posting.

## Test

End-to-end tests run the page in headless Chromium against the reference
Python server (from a sibling checkout of
[shazow/apron](https://github.com/shazow/apron), run with `uv`) and a scripted
mock LLM, covering tool calls, raw frames, and the auto fallback:

```sh
npm install
npm test          # or: node test/e2e.mjs tools|raw|auto
```

Set `APRON_SERVER` to point at `apron_server.py` elsewhere.

## Limits

- One bot per tab; background tabs may throttle timers, but the WebSocket
  keeps receiving.
- Mentions that arrive while disconnected are not replayed.
- Responses are not streamed (no `embed:stream` yet).
