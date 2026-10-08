"""Synthetic-only tests: no real credentials, conversations, or network calls."""
import asyncio
from collections import Counter
from decimal import Decimal
import io
from contextlib import redirect_stdout
import json
import signal
import unittest
from unittest.mock import patch
import bot


def config(**changes):
    values = dict(token="synthetic-apron-secret", api_key="synthetic-api-secret",
                  room="test-room", humans=frozenset({"human"}), budget=Decimal("0.10"))
    values.update(changes)
    return bot.Config(**values)


def message(ident="101", **changes):
    params = {"room_id": "test-room", "message_id": ident, "log_id": ident,
              "from": {"user_id": "human"}, "body": {"text": "Synthetic test", "mentions": ["self"]}}
    params.update(changes)
    return {"method": "message", "params": params}


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.p = bot.Policy(config(), Counter())
        self.p.you, self.p.watermark = "self", 100

    def test_config_budget_and_runtime(self):
        env = dict(APRON_TOKEN="fake", DARKBLOOM_API_KEY="fake2",
                   DARKBLOOM_BASE_URL=bot.API_URL, APRON_ROOM_ID="test-room",
                   APRON_HUMAN_IDS="human", BOT_BUDGET_USD="0.10",
                   BOT_PRIOR_SPEND_USD=str(bot.KNOWN_PRIOR_RESERVATION_USD))
        self.assertEqual(bot.Config.from_env(env).runtime, 300)
        self.assertNotIn("fake", repr(bot.Config.from_env(env)))
        for key, bad in [("APRON_TOKEN", ""), ("BOT_BUDGET_USD", "5"),
                         ("BOT_BUDGET_USD", "NaN"), ("BOT_PRIOR_SPEND_USD", "0"), ("BOT_PRIOR_SPEND_USD", "0.000016125"),
                         ("BOT_MAX_RUNTIME_SECONDS", "999999"),
                         ("DARKBLOOM_BASE_URL", "https://invalid.example")]:
            with self.subTest(key=key), self.assertRaises(bot.Stop):
                bot.Config.from_env({**env, key: bad})

    def test_replay_history_edits_and_startup(self):
        self.assertIsNotNone(self.p.accept(message()))
        self.assertIsNone(self.p.accept(message()))
        self.assertIsNone(self.p.accept(message("99")))
        self.assertIsNone(self.p.accept(message("102", log_id="103")))
        self.assertIsNone(self.p.accept({"id": "history", "result": {"messages": [message()]}}))
        self.p.watermark = None
        self.assertIsNone(self.p.accept(message("105")))

    def test_sender_and_loop_filters(self):
        cases = [{"from": {"user_id": "self"}}, {"from": {"user_id": "~server"}},
                 {"from": {"user_id": "human", "roles": ["bot"]}},
                 {"from": {"user_id": "unknown"}}, {"room_id": "other"},
                 {"deleted": True},
                 {"prev_room_id": "other"}, {"body": {"text": "hi", "embeds": [{}]}}]
        for i, case in enumerate(cases, 101):
            with self.subTest(case=case):
                self.assertIsNone(self.p.accept(message(str(i), **case)))

    def test_input_and_secret_limits(self):
        for i, text in enumerate(["", "x"*8001, "synthetic-api-secret", "synthetic-apron-secret"], 101):
            self.assertIsNone(self.p.accept(message(str(i), body={"text": text})))

    def test_default_budget_and_optional_setup(self):
        c = bot.Config.from_env(dict(APRON_TOKEN="fake", DARKBLOOM_API_KEY="fake2",
                                     DARKBLOOM_BASE_URL=bot.API_URL))
        self.assertEqual(c.budget + c.prior_spend, Decimal("5"))
        self.assertEqual(c.prior_spend, bot.KNOWN_PRIOR_RESERVATION_USD)
        self.assertEqual(c.room, "")
        self.assertFalse(c.humans)

    def test_apron_key_preferred_with_token_fallback(self):
        env = dict(DARKBLOOM_API_KEY="synthetic-darkbloom", DARKBLOOM_BASE_URL=bot.API_URL)
        self.assertEqual(bot.Config.from_env({**env, "APRON_KEY": "synthetic-key",
                                             "APRON_TOKEN": "synthetic-fallback"}).token,
                         "synthetic-key")
        self.assertEqual(bot.Config.from_env({**env, "APRON_TOKEN": "synthetic-fallback"}).token,
                         "synthetic-fallback")
        self.assertEqual(bot.Config.from_env({**env, "APRON_KEY": "",
                                             "APRON_TOKEN": "synthetic-fallback"}).token,
                         "synthetic-fallback")
        with self.assertRaises(bot.Stop):
            bot.Config.from_env(env)

    def test_public_mode_requires_structured_mention(self):
        self.p.config.humans = frozenset()
        self.assertIsNone(self.p.accept(message("101", body={"text": "unmentioned"})))
        self.assertIsNone(self.p.accept(message("102", body={"text": "@self hi"})))
        self.assertIsNotNone(self.p.accept(message("103", body={"text": "Synthetic hi", "mentions": ["self"]})))
        self.assertIsNone(self.p.accept(message("104", body={"text": "hi", "mentions": ["self"]},
                                               **{"from": {"user_id": "anotherbot", "roles": ["bot"]}})))

    def test_room_context_order_dedup_and_no_future(self):
        for ident, text in [("99", "earlier"), ("98", "oldest"), ("99", "duplicate")]:
            self.p.observe(message(ident, body={"text": text})["params"])
        self.assertIsNone(self.p.accept(message("100", body={"text": "ambient"})))
        self.p.observe(message("105", body={"text": "future"})["params"])
        self.assertIsNotNone(self.p.accept(message("101")))
        context = self.p.messages("human", "trigger", "101")
        self.assertEqual([m["content"] for m in context[1:]], ["Participant 1: oldest", "Participant 1: earlier", "Participant 1: ambient", "Participant 1: trigger"])
        self.assertTrue(all(m["role"] == "user" for m in context[1:]))

    def test_reply_to_bot_vs_other_and_unknown(self):
        self.p.config.humans = frozenset()
        self.p.observe(message("90", **{"from": {"user_id": "self"}}, body={"text": "bot turn"})["params"])
        self.p.observe(message("91", body={"text": "other turn"})["params"])
        for ident, target, accepted in [("101", "90", True), ("102", "91", False), ("103", "89", False)]:
            frame = message(ident, body={"text": "reply"}, reply_to={"message_id": target})
            self.assertEqual(self.p.accept(frame) is not None, accepted)
        self.assertEqual(self.p.messages("human", "trigger", "101")[1]["role"], "assistant")
        self.assertIsNone(self.p.accept(message("104", **{"from": {"user_id": "robot", "roles": ["bot"]}},
                                                reply_to={"message_id": "90"})))

    def test_history_isolation_and_limits(self):
        other = bot.Policy(config(room="other"), Counter())
        other.you = "self"
        for i in range(1, 200):
            snapshot = message(str(i), body={"text": "x"*1000})["params"]
            self.p.observe(snapshot)
            other.observe(snapshot)
        self.assertFalse(other.history)
        self.assertLessEqual(len(self.p.history), 64)
        self.assertLessEqual(sum(len(row[2].encode()) for row in self.p.history.values()), 12000)
        self.assertLessEqual(sum(len(m["content"].encode()) for m in self.p.messages("human", "x"*8000)[1:]), 12000)
        self.p.observe(message("200", body={"text": "synthetic-api-secret"})["params"])
        self.assertNotIn("synthetic-api-secret", str(self.p.history))
        self.p.observe(message("201", body={"text": "x"*8001})["params"])
        self.assertFalse(self.p.history[201][2])

    def test_history_edits_deletion_and_move(self):
        self.p.observe(message("90", body={"text": "original"})["params"])
        self.p.observe(message("90", log_id="91", body={"text": "edited"})["params"])
        self.p.observe(message("90", body={"text": "stale"})["params"])
        self.assertEqual(self.p.history[90][2], "edited")
        self.p.observe(message("90", log_id="92", deleted=True)["params"])
        self.assertFalse(self.p.history[90][2])
        self.p.observe(message("93")["params"])
        self.p.observe(message("93", room_id="other", prev_room_id="test-room")["params"])
        self.assertNotIn(93, self.p.history)

    def test_reservation_and_call_ceiling(self):
        self.p.reserve(Decimal("0.06"), 1)
        with self.assertRaises(bot.Stop):
            self.p.reserve(Decimal("0.06"), 20)
        self.assertEqual(self.p.reserved, Decimal("0.06"))
        self.assertEqual(self.p.next_call, 16)
        self.p.config.max_calls = 1
        with self.assertRaises(bot.Stop):
            self.p.reserve(Decimal("0"), 20)

    def test_configured_identity_reaches_system_role(self):
        api = bot.Darkbloom(config())
        response = {"model": bot.MODEL, "choices": [{"finish_reason": "stop", "message": {"content": "Synthetic reply"}}]}
        messages = self.p.messages("human", "Synthetic identity question", "101")
        with patch.object(api, "request", return_value=response) as request:
            api.complete(messages)
        payload = request.call_args.args[1]
        self.assertEqual(payload["messages"][0]["role"], "system")
        identity = payload["messages"][0]["content"]
        self.assertIn(payload["model"], identity)
        self.assertIn("Bonsai 2 27B", identity)
        self.assertIn("served via Darkbloom", identity)
        self.assertEqual(sum(m["role"] == "system" for m in payload["messages"]), 1)

    def test_pricing_payload_and_output(self):
        api = bot.Darkbloom(config())
        with patch.object(api, "request", return_value={"prices": [
                {"model": bot.MODEL, "input_usd": "$0.0750", "output_usd": "$0.5000"}]}):
            self.assertEqual(api.reservation(), Decimal("0.0217088"))
        response = {"model": bot.MODEL, "choices": [{"finish_reason": "stop", "message": {"content": "Synthetic reply"}}]}
        with patch.object(api, "request", return_value=response) as request:
            self.assertEqual(api.complete(self.p.messages("human", "hi")), "Synthetic reply")
            payload = request.call_args.args[1]
            self.assertEqual(set(payload), {"model", "messages", "stream", "max_tokens"})
            self.assertFalse(payload["stream"])
            self.assertEqual(payload["max_tokens"], 4096)
            self.assertNotIn("synthetic-api-secret", json.dumps(payload))
        for text in ("synthetic-api-secret", "synthetic-apron-secret"):
            response["choices"][0]["message"]["content"] = text
            with patch.object(api, "request", return_value=response), self.assertRaises(bot.Stop):
                api.complete([])
        response["choices"][0]["message"] = {"content": "x"*5000}
        with patch.object(api, "request", return_value=response):
            self.assertEqual(api.complete([]), "The answer exceeded the reply limit. Please ask for one specific point.")
        response["choices"][0]["message"]["tool_calls"] = [{"name": "shell"}]
        with patch.object(api, "request", return_value=response), self.assertRaises(bot.Stop):
            api.complete([])

    def test_output_diagnostics_are_allowlisted(self):
        api = bot.Darkbloom(config())
        result = {"model": bot.MODEL, "choices": [{"finish_reason": "length", "message": {"content": ""}}],
                  "usage": {"completion_tokens_details": {"reasoning_tokens": 128}}}
        output = io.StringIO()
        with patch.object(api, "request", return_value=result), redirect_stdout(output):
            self.assertEqual(api.complete([]), bot.INCOMPLETE_REPLY)
        record = json.loads(output.getvalue())
        self.assertEqual(record, {"status": "model_output_metadata", "finish_reason": "length",
            "content_type": "text", "content_nonempty": False, "output_length": 0, "reasoning_tokens": 128})
        result["choices"][0] = {"finish_reason": "synthetic-private", "message": {"content": "synthetic-api-secret"}}
        output = io.StringIO()
        with patch.object(api, "request", return_value=result), redirect_stdout(output):
            with self.assertRaisesRegex(bot.Stop, "^model_output_secret_guard$"):
                api.complete([])
        self.assertNotIn("synthetic", output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["finish_reason"], "unknown")

    def test_completion_exhaustion_empty_fragment_and_complete(self):
        api = bot.Darkbloom(config())
        cases = [("length", "", bot.INCOMPLETE_REPLY),
                 ("length", "I'm a", bot.INCOMPLETE_REPLY),
                 ("stop", "", bot.INCOMPLETE_REPLY),
                 ("stop", None, bot.INCOMPLETE_REPLY),
                 ("stop", "A complete synthetic answer.", "A complete synthetic answer.")]
        for finish, content, expected in cases:
            result = {"model": bot.MODEL, "choices": [{"finish_reason": finish, "message": {"content": content}}],
                "usage": {"completion_tokens_details": {"reasoning_tokens": 4096 if finish == "length" else 8}}}
            with patch.object(api, "request", return_value=result) as request:
                self.assertEqual(api.complete([]), expected)
                request.assert_called_once()
        result["choices"][0] = {"finish_reason": "length", "message": {"content": "synthetic-api-secret"}}
        with patch.object(api, "request", return_value=result), self.assertRaisesRegex(bot.Stop, "^model_output_secret_guard$"):
            api.complete([])

    def test_long_complete_reply_uses_sentence_boundary(self):
        answer = "A complete synthetic sentence. " * 80
        bounded = bot.bounded_reply(answer)
        self.assertLessEqual(len(bounded), 1000)
        self.assertTrue(bounded.endswith(". [Answer shortened.]"))
        self.assertTrue(answer.startswith(bounded.removesuffix(" [Answer shortened.]")))
        self.assertNotIn("x"*50, bot.bounded_reply("x"*2000))

    def test_anonymous_sender_attribution(self):
        for ident, user in [("90", "first-real-id"), ("91", "second-real-id"), ("92", "first-real-id")]:
            self.p.observe(message(ident, body={"text": "Synthetic."}, **{"from": {"user_id": user}})["params"])
        context = self.p.messages("second-real-id", "Trigger.", "101")
        self.assertTrue(context[1]["content"].startswith("Participant 1: "))
        self.assertTrue(context[2]["content"].startswith("Participant 2: "))
        self.assertTrue(context[3]["content"].startswith("Participant 1: "))
        self.assertTrue(context[-1]["content"].startswith("Participant 2: "))
        self.assertNotIn("real-id", json.dumps(context))

    def test_reply_target_survives_context_pruning(self):
        self.p.observe(message("90", body={"text": "Pinned bot answer."}, **{"from": {"user_id": "self"}})["params"])
        target = 90, self.p.history[90]
        for i in range(91, 108):
            self.p.observe(message(str(i), body={"text": "x"*1000})["params"])
        self.assertNotIn(90, self.p.history)
        context = self.p.messages("human", "trigger"*1000, "109", target)
        self.assertIn({"role": "assistant", "content": "[Message being replied to] Pinned bot answer."}, context)
        self.assertLessEqual(sum(len(m["content"].encode()) for m in context[1:]), 12000)
        self.assertEqual(sum("Pinned bot answer." in m["content"] for m in context), 1)
        self.assertIn("Pinned bot answer.", context[-2]["content"])
        self.assertIn("Reply to the referenced message", context[-1]["content"])

    def test_later_edit_not_in_earlier_trigger_context(self):
        self.p.observe(message("90", log_id="110", body={"text": "future edit"})["params"])
        self.assertNotIn("future edit", json.dumps(self.p.messages("human", "trigger", "101")))

    def test_sanitized_failure_no_retry_no_redirect(self):
        api = bot.Darkbloom(config())
        with patch.object(api.opener, "open", side_effect=RuntimeError("sensitive body")) as request:
            with self.assertRaisesRegex(bot.Stop, "^api_request_failed$"):
                api.request("/chat/completions", {})
            self.assertEqual(request.call_count, 1)
        self.assertIsNone(bot.NoRedirect().redirect_request(None, None, None, None, None, None))
        with self.assertRaises(bot.Stop):
            api.request("https://invalid.example")


class FakeSocket:
    def __init__(self, joined=True):
        self.incoming, self.sent, self.joined = asyncio.Queue(), [], joined
        self.incoming.put_nowait(json.dumps({"method": "server", "params": {
            "apron": 8, "auth": ["token"], "capabilities": ["rooms"]}}))

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.incoming.get()

    async def send(self, raw):
        frame = json.loads(raw)
        self.sent.append(frame)
        method = frame["method"]
        if method == "auth":
            result = {"you": {"user_id": "self"}}
        elif method == "room_list":
            result = {"joined": [{"room_id": "test-room"}] if self.joined else []}
        elif method == "message":
            result = {"message_id": "100" if "reply_to" not in frame["params"] else "200"}
            self.incoming.put_nowait(json.dumps(message(result["message_id"], **{"from": {"user_id": "self"}})))
        else:
            return
        self.incoming.put_nowait(json.dumps({"id": frame["id"], "result": result}))


class FakeAPI:
    def __init__(self):
        self.calls = []

    def reservation(self):
        return Decimal("0.02")

    def complete(self, messages):
        self.calls.append(messages)
        return "Synthetic response"


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def until(self, condition):
        async with asyncio.timeout(2):
            while not condition():
                await asyncio.sleep(.005)

    async def test_handshake_reply_rate_limit_and_no_output(self):
        socket, api = FakeSocket(), FakeAPI()
        session = bot.Session(socket, config(), api)
        output = io.StringIO()
        with redirect_stdout(output):
            task = asyncio.create_task(session.run())
            try:
                await self.until(lambda: session.stats["ready"])
                socket.incoming.put_nowait(json.dumps(message("101")))
                await self.until(lambda: session.stats["replies"] == 1)
                socket.incoming.put_nowait(json.dumps(message("201")))
                await self.until(lambda: session.stats["rate_dropped"] == 1)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(len(api.calls), 1)
        self.assertEqual([f["method"] for f in socket.sent], ["auth", "room_list", "message", "message"])
        self.assertEqual(socket.sent[2]["params"]["body"]["text"], bot.HELLO)
        self.assertEqual(socket.sent[-1]["params"]["reply_to"], {"message_id": "101"})
        self.assertFalse(session.policy.history)

    async def test_missing_room_blocks_hello(self):
        socket, api = FakeSocket(False), FakeAPI()
        with self.assertRaisesRegex(bot.Stop, "room_not_joined"):
            await asyncio.wait_for(bot.Session(socket, config(), api).run(), 2)
        self.assertFalse(api.calls)
        self.assertNotIn("message", [f["method"] for f in socket.sent])

    async def test_room_metadata_auto_selection_and_core_default(self):
        for core in (False, True):
            socket, api = FakeSocket(), FakeAPI()
            if core:
                socket.incoming.get_nowait()
                socket.incoming.put_nowait(json.dumps({"method": "server", "params": {
                    "apron": 8, "auth": ["token"], "capabilities": []}}))
            session = bot.Session(socket, config(room=""), api)
            task = asyncio.create_task(session.run())
            try:
                await self.until(lambda: session.stats["ready"])
                self.assertEqual(session.config.room, "test-room")
                self.assertEqual(session.policy.watermark, 100)
                self.assertFalse(api.calls)
                if core:
                    self.assertNotIn("room_list", [f["method"] for f in socket.sent])
                    self.assertNotIn("room_id", socket.sent[-1]["params"])
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_ambiguous_room_selection_does_not_send_hello(self):
        socket = FakeSocket(False)
        session = bot.Session(socket, config(room=""), FakeAPI())
        with self.assertRaisesRegex(bot.Stop, "room_selection_required"):
            await asyncio.wait_for(session.run(), 2)
        self.assertEqual(session.stats["joined_rooms"], 0)
        self.assertNotIn("message", [f["method"] for f in socket.sent])

    async def test_bounded_queue(self):
        session = bot.Session(FakeSocket(), config(), FakeAPI())
        session.policy.you, session.policy.watermark = "self", 100
        task = asyncio.create_task(session.receive())
        try:
            for i in range(101, 121):
                session.ws.incoming.put_nowait(json.dumps(message(str(i))))
            await self.until(lambda: session.stats["eligible"] == 20)
            self.assertEqual(session.queue.qsize(), 8)
            self.assertEqual(session.stats["dropped"], 12)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_ambiguous_model_failure_stops_without_retry(self):
        socket, api = FakeSocket(), FakeAPI()
        session = bot.Session(socket, config(), api)
        with patch.object(api, "complete", side_effect=bot.Stop("api_request_failed")) as complete:
            task = asyncio.create_task(session.run())
            await self.until(lambda: session.stats["ready"])
            socket.incoming.put_nowait(json.dumps(message("101")))
            with self.assertRaisesRegex(bot.Stop, "api_request_failed"):
                await asyncio.wait_for(task, 2)
            self.assertEqual(complete.call_count, 1)
            self.assertEqual(session.policy.reserved, Decimal("0.02"))
            self.assertEqual(session.stats["replies"], 0)

    async def test_history_bootstrap_no_replay_trigger_and_reply_resolution(self):
        class HistorySocket(FakeSocket):
            async def send(self, raw):
                frame = json.loads(raw)
                if frame["method"] != "history":
                    return await super().send(raw)
                self.sent.append(frame)
                if "after" in frame["params"]:
                    rows = [message("50", **{"from": {"user_id": "self"}}, body={"text": "old bot turn"})["params"]]
                else:
                    rows = [message("80", body={"text": "room background"})["params"],
                            message("81", room_id="other")["params"], message("82")["params"]]
                self.incoming.put_nowait(json.dumps({"id": frame["id"], "result": {"messages": rows}}))
        socket, api = HistorySocket(), FakeAPI()
        socket.incoming.get_nowait()
        socket.incoming.put_nowait(json.dumps({"method": "server", "params": {
            "apron": 8, "auth": ["token"], "capabilities": ["rooms", "history"]}}))
        session = bot.Session(socket, config(), api)
        task = asyncio.create_task(session.run())
        try:
            await self.until(lambda: session.stats["ready"])
            self.assertFalse(api.calls)
            self.assertEqual(session.stats["history_loaded"], 1)
            self.assertNotIn(81, session.policy.history)
            socket.incoming.put_nowait(json.dumps(message("101", body={"text": "reply trigger"}, reply_to={"message_id": "50"})))
            await self.until(lambda: session.stats["replies"] == 1)
            self.assertEqual(session.stats["reply_lookups"], 1)
            context = api.calls[0]
            self.assertIn({"role": "user", "content": "Participant 1: room background"}, context)
            self.assertIn({"role": "assistant", "content": "[Message being replied to] old bot turn"}, context)
            self.assertEqual(context[-1], {"role": "user", "content": "[Reply to the referenced message immediately above] Participant 1: reply trigger"})
            self.assertEqual(sum(m["content"] == "[Reply to the referenced message immediately above] Participant 1: reply trigger" for m in context), 1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertFalse(session.policy.history)
        self.assertFalse(session.resolvers)

    async def test_queued_context_is_frozen_and_known_target_can_be_fetched(self):
        session = bot.Session(FakeSocket(), config(), FakeAPI())
        session.policy.you, session.policy.watermark = "self", 100
        session.server = {"capabilities": ["history"]}
        session.policy.observe(message("90", body={"text": "earlier"})["params"])
        session.enqueue(message("101"))
        item = session.queue.get_nowait()
        session.policy.observe(message("90", log_id="110", body={"text": "later"})["params"])
        self.assertIn("earlier", json.dumps(item[3]))
        self.assertNotIn("later", json.dumps(item[3]))
        session.policy.own_ids[50] = True
        self.assertTrue(session.needs_lookup(message("102", body={"text": "reply"}, reply_to={"message_id": "50"})))

    async def test_runtime_and_operator_stop(self):
        class FakeConnect:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return FakeSocket()

            async def __aexit__(self, *args):
                pass

        loop = asyncio.get_running_loop()
        for operator_stop in (False, True):
            callbacks = {}

            def register(sig, callback):
                callbacks[sig] = callback
                if operator_stop and sig == signal.SIGTERM:
                    loop.call_later(.05, callback)

            output = io.StringIO()
            with patch("websockets.asyncio.client.connect", FakeConnect), \
                 patch("bot.Darkbloom", return_value=FakeAPI()), \
                 patch("signal.signal"), patch("signal.alarm") as alarm, \
                 patch.object(loop, "add_signal_handler", register), redirect_stdout(output):
                await bot.live(config(runtime=1))
            records = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual(records[0]["status"], "ready")
            self.assertEqual(records[-1]["status"], "operator_stop" if operator_stop else "runtime_limit")
            self.assertNotIn("Synthetic", output.getvalue())
            self.assertNotIn("secret", output.getvalue())
            alarm.assert_any_call(6)
            if operator_stop:
                alarm.assert_any_call(5)


if __name__ == "__main__":
    with patch("socket.socket.connect", side_effect=AssertionError("Network prohibited in tests")), redirect_stdout(io.StringIO()):
        unittest.main()
