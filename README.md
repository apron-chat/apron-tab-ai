# Apron → Darkbloom test bot

A foreground Python bot for short public-chat and protocol tests. Validated with synthetic fixtures and bounded live sessions.
Confirm runtime setup before launching another bounded session.

It authenticates using the existing `APRON_KEY` (or `APRON_TOKEN` fallback), resolves its room through
protocol metadata (or an optional explicit selection), and sends:

> Hello! I'm a chat-testing bot. My responses use Darkbloom AI.

It replies to new messages that structurally mention its assigned user ID
(`body.mentions`) or reply to one of its own messages. Plain `@name` text may
not create a protocol mention. An optional human allowlist restricts senders;
it does not bypass these triggers. Replies to others require a mention.

Context includes non-trigger messages and the bot's own messages from the selected
room, in chronological order, deduplicated by message ID and latest snapshot.
At startup, capability `history` permits one recent same-room page (64 records).
History ingestion never triggers responses. Without history support, context is
limited to this connection. Unknown reply targets can be resolved with exact,
same-room history queries (one concurrent, at most ten, at least 15 seconds apart).
Unavailable, rate-limited, or unresolved targets are conservatively ignored unless
there is a mention. Nothing joins or reads another room automatically.

It ignores its own messages as triggers, identities labeled `bot`, system notices,
other rooms, edits, deletions, attachments, startup traffic, and replays.

## Configuration and use

Python 3.12 and `websockets==16.0` are available in the inspected environment.
The dependency is pinned in `requirements.txt`; nothing was installed during
construction. On another host, install it in your own managed environment.

Configure credentials through host secret settings, never in chat, source files,
command arguments, `.env` files, or logs. The bot reads process environment
variables and never generates or rotates credentials.

| Variable | Required value or behavior |
| --- | --- |
| `APRON_KEY` | Preferred process environment variable for Apron token auth |
| `APRON_TOKEN` | Fallback when `APRON_KEY` is absent or empty |
| `DARKBLOOM_API_KEY` | Existing Darkbloom credential |
| `DARKBLOOM_BASE_URL` | Exactly `https://api.darkbloom.dev/v1` |
| `APRON_ROOM_ID` | Optional; needed only if joined-room metadata is ambiguous |
| `APRON_HUMAN_IDS` | Optional sender restriction, maximum 16; mentions/replies still required |
| `BOT_BUDGET_USD` | Optional lower budget; defaults to $5 minus prior reservation |
| `BOT_PRIOR_SPEND_USD` | Defaults to cumulative reservation `0.039465725`; cannot be lowered; update after later runs |
| `BOT_MAX_RUNTIME_SECONDS` | Default 300; allowed 1–600 |
| `BOT_MIN_INTERVAL_SECONDS` | Default 15; minimum 10 between inference starts |
| `BOT_MAX_CALLS` | Default 10; maximum 20 per process |

Current code requires both credentials in process environment variables plus the
expected Darkbloom base URL. Budget defaults incorporate the known ceiling and
earlier test. Protocol version 8 and token auth are required. The dedicated bot
identity must not be shared with a human.

**Credential naming:** use `APRON_KEY`; `APRON_TOKEN` remains a fallback.
An absent `APRON_TOKEN` alone does not establish that the configured credential
is absent. A separate executor successfully authenticated using regular
`APRON_KEY`. Preserve any configured secret scope and do not expose values.

Apron's protocol token scheme authenticates after the WebSocket connection opens:
the client sends an `auth` JSON frame with `params.scheme` equal to `token` and
the bearer token in `params.token`. The documented scheme does not specify an
HTTP/WebSocket handshake Authorization header as a substitute. This implementation
currently reads `APRON_KEY` or `APRON_TOKEN` through configuration and sends that
JSON frame; it has no separate domain-secret adapter. If platform injection supports only
HTTP headers and not token substitution in WebSocket JSON frames, this transport
cannot use the scoped credential without a separately supported adapter. Do not
invent a placeholder, change auth schemes, or broaden access to work around it.

Token auth identifies the account, not a current-room preference. With `rooms`,
the bot requests `room_list` with `filter: joined`, without members or history,
and chooses the sole joined room. With zero or several joined rooms it stops
before hello and reports only the count; an operator must choose/configure a
room ID and ensure membership. Optional `APRON_ROOM_ID` narrows this metadata
lookup. Without `rooms`, the protocol permits posting to the server's default
room; the bot learns its ID from its own hello's broadcast metadata. It never
examines other message text to select a room.

Local validation returns a fixed status without using the network:

```sh
cd /workspace/apron-tab-ai
PYTHONDONTWRITEBYTECODE=1 python3 bot.py --check
```

After launch authorization, setup confirmation, and carrying forward the last reservation:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 bot.py --run
```

Run in the foreground. **Ctrl+C or SIGTERM stops it.** Initial aggregate status
includes its PID; `kill -TERM <pid>` stops that exact process from another
terminal. Default active runtime is five minutes. Cancellation stops new work
and closes the socket. A five-second hard shutdown timer bounds process exit if
an HTTP worker is blocked. An already-sent request may finish remotely and stays
fully reserved against the budget. Do not use automatic restart, tracing,
debugger inspection, process dumps, or payload logs.

This task runner offers resumable command sessions, but no documented guarantee
that a process survives the agent turn or an environment disconnect. Indefinite
hosting is not promised. Persistent operation needs a separately approved host.

## Fixed network and spending boundaries

Endpoints and model are fixed in code: `wss://server.apron.chat/`, Darkbloom's
`/v1/pricing` and `/v1/chat/completions`, and `ternary-bonsai-2-27b`. No redirects
are followed. Host-managed egress proxies are respected; HTTPS/WSS still verify
the destination certificate. This environment refused a direct public HTTP
connection without its managed proxy. Bounded live authentication, hello, history loading, and a reply have been confirmed.

The user's **$5 cumulative ceiling is hard**, not unlimited authorization.
`BOT_BUDGET_USD + BOT_PRIOR_SPEND_USD` must not exceed 5. Include the earlier
test's **estimated** $0.000016125 (actual charge was not reported), all later
reservations, and other use of this key. Keep the provider-side $5 key cap enabled:
the bot cannot enforce shared-key spending by other processes.

Before the startup hello and each completion, public model pricing is checked.
Unavailable or invalid pricing stops the session. Each attempt reserves the
price of the **entire 262,144-token context plus 512 generation tokens**, even though
inputs are much smaller. At verified rates of $0.075/$0.50 per million input/output
tokens, this is $0.0199168 per attempt. Reservations are never refunded, even on
errors, timeouts, or uncertain outcomes. There are no inference retries, automatic
reconnects, or balance/usage-history queries.

Final aggregate status includes `cumulative_reserved_usd`. **Before any manual
restart**, set `BOT_PRIOR_SPEND_USD` to that total. If the report is lost, account
for the entire previous per-run budget. No ledger is persisted: cross-run
correctness requires this operator step and the provider's cap. In-flight price
changes and provider billing behavior are outside local reservation guarantees.

## Privacy and reply boundaries

Real chat messages and model replies flow directly through runtime memory to
their intended destinations. The assistant and parent must not inspect them.
There are no transcripts, payload logs, raw exception output, tracing, disk
persistence, model tools/function calling, filesystem tools, shell tools, URL
fetching, or model-controlled destinations. Replies use the fixed Apron `message`
method with plain text; model output cannot become a protocol command.

Only text is sent to Darkbloom: no names, IDs, room descriptions, attachments,
embedded messages, credentials, or assistant personal context. Context is scoped to the single selected room, at most 64 snapshots and
12,000 UTF-8 bytes total. A separate bounded index retains up to 128 own message
IDs for reply routing. Other participants use the user role; only this bot uses
the assistant role. Conversation text cannot supply system instructions. Inputs are capped at 8,000 bytes; generation at 512 tokens including reasoning and posted outputs at
1,000 characters. The queue holds eight messages; overflow and rate-limited
messages are dropped. Shutdown clears conversation containers; Python does not
guarantee secure memory erasure, and providers follow their own data policies.

The hello's acknowledged message ID establishes the replay cutoff. Only later
creation snapshots qualify. Recent history is requested only for the selected room and never triggers a reply. IDs increase under the
protocol; late/out-of-order older messages are conservatively dropped. Messages
are handled at most once within the process.

The fixed prompt limits purpose to public chat/protocol tests and declines tool
use and secret disclosure. Credentials are excluded from model input, with an
additional exact-secret guard on inputs and outputs. These controls **do not
claim prompt-injection immunity** or human verification. Protocol roles are
optional and server-defined: a bot may lack a `bot` label. Mention/reply triggers, sender filters, rate limits, and finite call/runtime budgets reduce but
cannot eliminate loops with unlabeled bots that deliberately mention this bot.
If using the optional allowlist, ensure those identities belong to humans.
Malicious input can still elicit undesirable text.

An auth response may rotate its token. This process never reconnects, prints, or
persists that token; only a rotation count is reported. If the configured token
becomes invalid, obtain a fresh one through normal secret settings before rerunning.

## Synthetic tests and references

```sh
PYTHONDONTWRITEBYTECODE=1 python3 test_bot.py -v
```

Fixtures use fake sockets/APIs, with outgoing socket connections blocked. Tests
cover handshake, membership, routing, payload-output exclusion, runtime/SIGTERM
shutdown, room discovery, mention/replay/loop filters, memory/queue limits,
rate limits, budgeting, request shape, secret
guards, output limits, sanitized failures, and no HTTP retries/redirects.

Protocol: [current Apron PROTOCOL.md](https://github.com/shazow/apron/blob/main/PROTOCOL.md),
read from `main` on 2026-10-08. Billing: [Darkbloom pricing](https://docs.darkbloom.dev/billing/pricing).

## Live validation and next-run reservation

The first live session authenticated and acknowledged the disclosure hello, then
stopped after one inference failed output validation; no model reply was posted.
The next-run default and minimum cumulative reservation are
`BOT_PRIOR_SPEND_USD=0.039465725`. This is a conservative reservation, **not
actual charged cost**. Update it upward after any subsequent use of the key.

The revised validator distinguishes fixed failure reasons for model mismatch,
empty/nontext content, tool requests, secret matches, and unknown structure.
It reports only allowlisted finish-reason/content-type enums, nonempty boolean,
output length, and reasoning-token count. It never prints text or unknown fields.
An empty output remains rejected; diagnosing a token-limit issue does not weaken
the secret guard. All payloads remain in bounded runtime memory only.

The revised five-minute session ended normally: history loaded, one eligible
message, one inference, and one acknowledged reply. Cumulative reservation is
$0.039465725, including the earlier failed inference and initial estimate.
Allowlisted metadata showed a length stop with 123 reasoning tokens under the
old 128-token cap. The local generation allowance is now 512 tokens; posted
text remains capped at 1,000 characters and all output guards remain active.
This mitigation passed synthetic tests but has not yet been run live. The exact
cause of the original generic rejection cannot be established retroactively.
