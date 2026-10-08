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
A referenced reply target appears once, explicitly marked as an earlier message,
immediately before the trigger so it is not buried or evicted by ambient context.
Anonymous participant labels distinguish senders without sending metadata names or real IDs. Participant references already
present in the question can be linked to those anonymous labels as user-role data.
Each queued request freezes its bounded context; later edits cannot change it.
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
| `BOT_PRIOR_SPEND_USD` | Defaults to cumulative reservation `0.213136125`; cannot be lowered; update after later runs |
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
price of the **entire 262,144-token context plus 32,768 generation tokens**, even though
inputs are much smaller. At verified rates of $0.075/$0.50 per million input/output
tokens, this is $0.0360448 per attempt. Reservations are never refunded, even on
errors, timeouts, or uncertain outcomes. There are no inference retries, automatic
reconnects, or balance/usage-history queries.

Final aggregate status includes `cumulative_reserved_usd`. **Before any manual
restart**, set `BOT_PRIOR_SPEND_USD` to that total. If the report is lost, account
for the entire previous per-run budget. Foreground runs do not persist a ledger: cross-run correctness requires this
operator step and the provider's cap. Supervised mode below uses a durable ledger. In-flight price
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
IDs for reply routing. Other participants use the user role with anonymous labels; only this bot uses
the assistant role. Conversation text cannot supply system instructions. Inputs are capped at 8,000 bytes; generation at 32,768 tokens including reasoning and posted outputs at
1,000 characters. Complete overlong answers end at a sentence boundary with an
explicit shortening marker; if no complete sentence fits, a fixed helpful notice
is used. Length-stopped or empty completions always produce a fixed failure
notice, never partial generated text. There are no regeneration or network
retries. The queue holds eight frozen contexts; overflow and rate-limited
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

The first live session acknowledged the disclosure hello, then stopped after
one inference failed generic output validation. The next five-minute session
loaded room history and acknowledged one reply, but the user reported that reply
was only a fragment. Retained structural metadata showed a length stop and 123
reasoning tokens under the old 128-token allowance. This strongly supports token
exhaustion as the fragment's cause; the original generic rejection cannot be
classified retroactively. Transport acknowledgement did not establish quality.

Generation now allows 4,096 tokens including reasoning, with instructions for
one or two concise complete sentences and explicit configured model identity.
The validator rejects tool requests, credential matches, malformed responses,
and unknown completion finishes. Length-stopped or empty output is discarded in
favor of a fixed helpful failure notice, without a retry or additional charge.
Only allowlisted structural metadata is emitted; no live messages, replies,
reasoning text, unknown fields, or credentials are logged.

Thirty synthetic regression tests cover the guards, completion exhaustion,
fragment suppression, complete and overlong answers, participant attribution,
frozen context, reply-target retention, room isolation, replay suppression,
budgeting, and shutdown. Four paid API calls used newly synthetic prompts only,
never live room content. Model identity and participant attribution passed; an
initial reply-reference semantic check failed, then passed after placing the
marked earlier reference immediately before the trigger. All four requests
finished with `stop`; the 4,096-token request parameter was accepted. These small
checks do not guarantee response quality for every conversation.

**Next-run default and minimum cumulative reservation: $0.213136125.**
This includes the initial estimate, earlier live attempts, four synthetic API
checks, and the latest live session. It is a **reservation, not actual charged
cost**. The remaining ceiling is $4.786863875. Use
`BOT_PRIOR_SPEND_USD=0.213136125` or a higher up-to-date total after further key use.

The latest live session started at 01:33:09 UTC on 2026-10-08 with 4,096 tokens.
It confirmed authentication, room history, and hello, then stopped at approximately
01:34:57 UTC with sanitized `api_request_failed`: four eligible messages, four
inference attempts, three acknowledged replies. The final attempt remains fully
reserved. The sanitized error does not identify a timeout versus another transport
or HTTP failure. No automatic retry or reconnect was attempted.

## Verified maximum for the next run

An authenticated read of the official `https://api.darkbloom.dev/v1/models`
endpoint on 2026-10-08 reported `max_output_length: 32768` and
`context_length: 262144` for `ternary-bonsai-2-27b`. These are separate limits;
the context window is not used as the output allowance. The next-run configuration
now requests 32,768 generation tokens, retains the concise-response instruction,
and conservatively reserves $0.0360448 per attempt at the verified rates.
The [model-list documentation](https://docs.darkbloom.dev/api/models) describes
this discovery endpoint; [chat-completion documentation](https://docs.darkbloom.dev/api/chat-completions)
describes `max_tokens` and its separate 8,192-token default when omitted.

Completion HTTP timeout is now 120 seconds (pricing remains 30 seconds), with
no retries. The session's existing runtime and hard shutdown still apply; a high
token ceiling does not guarantee a full 32,768-token generation before those time
limits. Responses remain bounded and length-stopped fragments are never posted.
The 32,768 configuration and timeout passed synthetic tests only; no paid maximum-
size probe or new live session was started. Thirty-seven regression tests pass.

## API failure diagnostics and recovery

The previously retained `api_request_failed` confirms the fourth reserved
completion failed before model-output validation, but the old generic exception
handler discarded its type. A timeout is plausible under the old 30-second HTTP
limit, not established. HTTP errors, network errors, and invalid JSON were also
mapped to that same status. No live payload or historical response was inspected.

New `api_failure` diagnostics contain only a fixed phase (`pricing` or
`completion`), fixed category, numeric HTTP status (zero when unavailable), and
recoverable boolean. Categories distinguish HTTP, rate limit, authentication,
timeout, transport, parsing, TLS verification, and unknown failures. Remote error
text, response bodies, headers, URLs, and exception representations are never
logged. HTTP error bodies are closed without reading; no Retry-After header is
read or exposed.

During message processing, HTTP 408/425/429/5xx, transport/timeouts, and malformed
JSON skip the affected trigger and preserve the listener. Each failed paid
attempt remains fully reserved; the trigger is never retried. Pending triggers
are discarded, and new triggers during a local exponential backoff are dropped.
Backoff starts at 30 seconds (60 for rate limits), caps at 120 seconds, and resets
after an acknowledged reply. A subsequent new trigger can run after backoff if
budget, call-count, and runtime limits still permit it. Failures fetching pricing
do not initiate or reserve a completion.

Authentication (401/403), billing/configuration and other nontransient HTTP errors,
TLS verification, unknown failures, and existing output-privacy guards still stop
the session. Startup pricing failure stops before hello because no safe budget
reservation can be established. Apron send/acknowledgment failures are not retried.
This improves bounded-session resilience; it does not install a persistent service,
add automatic restarts, or relax the hard cumulative $5 ceiling.

Synthetic tests cover safe failure categories, no error-body reads, successful
listener recovery on a later trigger, no failed-trigger replay or duplicate send,
unrefunded reservations, pricing failure without inference, authentication/config
shutdown, and increasing backoff bounded by the existing budget. No new live bot
or paid inference was run for this change. Cumulative reservation remains
$0.213136125 (not actual charged cost).

## Best-effort supervised service

The service uses a separate detached production worktree at
`/workspace/apron-tab-ai-production`; development stays in `/workspace/apron-tab-ai`.
The production checkout and its exact commit are recorded in a deployment manifest.
Start/reconnect refuses a dirty or changed checkout. Editing development files does
not hot-reload the running process. Only explicit stop, checkout, and deploy actions
activate a tested update. Nothing changes or merges main.

Persistent safe metadata lives in `/workspace/apron-service` (mode 0700): atomic
`budget.json`, initialization marker, singleton/budget locks, pinned deployment,
status, stop marker, and `metadata.log` (64 KiB with three rotated backups).
No credentials, room/message IDs, chat messages, model replies, or reasoning text
are saved. Credentials are inherited in memory from the configured environment;
there is no environment file. Core dumps and library payload logging are disabled.

Initial setup, once, from the clean tested production checkout:

```sh
python3 -B /workspace/apron-tab-ai-production/service.py init --seed 0.213136125
python3 -B /workspace/apron-tab-ai-production/service.py start
```

Ongoing control:

```sh
python3 -B /workspace/apron-tab-ai-production/service.py status
python3 -B /workspace/apron-tab-ai-production/service.py stop
```

The daemon holds a single-instance flock inherited by its worker; a second start
fails. The worker watches its supervisor and terminates if orphaned. Stop sends
SIGTERM, allows graceful cancellation, then bounds shutdown with the worker's
five-second alarm and supervisor's seven-second kill deadline. Status reports PIDs,
process-start ticks, pinned commit, readiness, safe counters, and cumulative
reservation. Status from the ledger is authoritative; previous README totals are
only checkpoints and must never reset a higher persisted value.

Every paid request atomically reserves its full conservative cost and fsyncs the
ledger before dispatch. There are no refunds. Concurrent updates are serialized;
missing/corrupt ledgers and failed durable writes fail closed. Initialization cannot
reset an existing service, even if its ledger was deleted. The $5 ceiling persists
across reconnects, restarts, and deployments. Provider-side shared-key limits remain
necessary for unrelated key usage. No balance or conversation-history export is made.

Supervised mode removes the five-minute runtime and per-process call ceiling while
retaining the finite $5 budget, 15-second rate spacing, context/output bounds, and
32,768-token maximum generation allowance. API failures use the tested per-message
backoff without retrying uncertain paid calls. Disconnects and transient startup
faults restart a fresh connection with bounded 5–300 second backoff; auth/config,
privacy, or budget failures halt. A greeting is claimed durably before its first
send, so an ambiguous greeting or restart never loops greetings. Reconnects load
recent same-room history and set a fresh cutoff without replaying prior triggers.
If the server cannot provide a safe history cutoff, resume stops rather than guessing.

The process is detached from the command session and will be checked from later
commands and an agent turn. This is best-effort hosting: VM destruction or platform
process cleanup can still stop it. No startup unit/cron or VM-recreation autostart is
installed or promised. Restart requires the same valid ledger, deployment and
configured credentials. For an explicit update: stop and confirm both PIDs are gone,
check out the reviewed bot-branch commit in production, run its tests, then run
`service.py deploy` and `service.py start`; the budget and hello marker are retained.

Run synthetic service tests with `PYTHONDONTWRITEBYTECODE=1 python3 test_service.py -v`.
They cover atomic budget updates, restart persistence, corruption/missing-ledger
shutdown, concurrent ceiling enforcement, singleton inheritance, log filtering,
transient worker restart, greeting suppression, replay exclusion, and graceful
shutdown without a service runtime timer. No live network is used in these tests.

## Participant references in questions

The bot now resolves structured mentions to stable participant IDs inside its
runtime, excluding its own mention from the target list. A selected-room-only
`room_list` request with `members: true`, same-room message authors, and updates
to already-known participants provide a bounded identity index (128 participants).
Handle/display-name matching is a fallback for references in a question that has
already triggered the bot; plain text never becomes a notification trigger.
Structured mention IDs disambiguate duplicate labels and take priority over
another user's coincidentally matching handle. Ambiguous or unknown references
produce a direct clarification rather than a room-wide summary.

For up to four resolved participants, the bot selects the latest retained earlier
text by stable author ID and creation order, excluding deleted messages, future
edits, the current trigger, and other rooms. These messages are pinned near the
question, exactly once, before trimming unrelated context. Identity resolution and
payload selection happen inside the bot only. Participant IDs and metadata names
are not sent as system instructions; only anonymous labels, references already in
the user's question, and relevant room text enter user/assistant roles. Malformed
or instruction-like metadata is never promoted into the system prompt.

The final answer includes a recent-history caveat. Missing participant history
produces a fixed explanation without an LLM call; there is no claim to cover all
past messages. Long/ambiguous selections fail clearly within the existing context
limit. No extra model tools, cross-room lookups, or private-context sources exist.

Sixty synthetic tests cover prior safeguards plus multi-author mentions, matching
handle collisions, ambiguous names, unknown members, members with no text,
ordering, history limits, cross-room exclusion, malicious names, and static
no-inference failure responses. Two fictional-input API checks returned the correct
single-author and multi-author latest topics. Their reservations used the existing
atomic service ledger alongside production, never a separate budget. At the last
checkpoint its total was $0.357315325; this is not a charged-cost claim and the
live ledger remains authoritative as the service continues.

Development stays separate from the running production checkout until the tested
commit is promoted with stop/deploy/start. The existing ledger and greeting claim
are preserved, so deployment does not reset spending or post another greeting.
