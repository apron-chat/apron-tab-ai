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
              "from": {"user_id": "human"}, "body": {"text": "Synthetic test"}}
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
                   BOT_PRIOR_SPEND_USD="0.000016125")
        self.assertEqual(bot.Config.from_env(env).runtime, 300)
        self.assertNotIn("fake", repr(bot.Config.from_env(env)))
        for key, bad in [("APRON_TOKEN", ""), ("BOT_BUDGET_USD", "5"),
                         ("BOT_BUDGET_USD", "NaN"), ("BOT_PRIOR_SPEND_USD", "0"),
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
                 {"reply_to": {"message_id": "100"}}, {"deleted": True},
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
        self.assertEqual(c.prior_spend, Decimal("0.000016125"))
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
        self.assertIsNone(self.p.accept(message("101")))
        self.assertIsNone(self.p.accept(message("102", body={"text": "@self hi"})))
        self.assertIsNotNone(self.p.accept(message("103", body={"text": "Synthetic hi", "mentions": ["self"]})))
        self.assertIsNone(self.p.accept(message("104", body={"text": "hi", "mentions": ["self"]},
                                               **{"from": {"user_id": "anotherbot", "roles": ["bot"]}})))

    def test_separate_bounded_history(self):
        for _ in range(30):
            self.p.remember("human", "a"*4000, "b"*1000)
        history = self.p.messages("human", "c"*8000)
        self.assertLessEqual(sum(len(m["content"].encode()) for m in history[1:]), 12000)
        self.assertEqual(len(self.p.messages("other", "hi")), 2)
        for i in range(30):
            self.p.remember(str(i), "hi", "hello")
        self.assertLessEqual(len(self.p.history), 16)

    def test_reservation_and_call_ceiling(self):
        self.p.reserve(Decimal("0.06"), 1)
        with self.assertRaises(bot.Stop):
            self.p.reserve(Decimal("0.06"), 20)
        self.assertEqual(self.p.reserved, Decimal("0.06"))
        self.assertEqual(self.p.next_call, 16)
        self.p.config.max_calls = 1
        with self.assertRaises(bot.Stop):
            self.p.reserve(Decimal("0"), 20)

    def test_pricing_payload_and_output(self):
        api = bot.Darkbloom(config())
        with patch.object(api, "request", return_value={"prices": [
                {"model": bot.MODEL, "input_usd": "$0.0750", "output_usd": "$0.5000"}]}):
            self.assertEqual(api.reservation(), Decimal("0.0197248"))
        response = {"model": bot.MODEL, "choices": [{"message": {"content": "Synthetic reply"}}]}
        with patch.object(api, "request", return_value=response) as request:
            self.assertEqual(api.complete(self.p.messages("human", "hi")), "Synthetic reply")
            payload = request.call_args.args[1]
            self.assertEqual(set(payload), {"model", "messages", "stream", "max_tokens"})
            self.assertFalse(payload["stream"])
            self.assertNotIn("synthetic-api-secret", json.dumps(payload))
        for text in ("synthetic-api-secret", "synthetic-apron-secret", ""):
            response["choices"][0]["message"]["content"] = text
            with patch.object(api, "request", return_value=response), self.assertRaises(bot.Stop):
                api.complete([])
        response["choices"][0]["message"] = {"content": "x"*5000}
        with patch.object(api, "request", return_value=response):
            self.assertEqual(len(api.complete([])), 1000)
        response["choices"][0]["message"]["tool_calls"] = [{"name": "shell"}]
        with patch.object(api, "request", return_value=response), self.assertRaises(bot.Stop):
            api.complete([])

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
    with patch("socket.socket.connect", side_effect=AssertionError("Network prohibited in tests")):
        unittest.main()
