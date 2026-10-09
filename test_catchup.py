"""Synthetic recent history only. No live messages or paid calls."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock
import bot
import catchup
import service
from test_bot import config, FakeAPI, FakeSocket

NOW = 1791500000000


def row(ago=1000, user='human', text='Can you explain this?', **changes):
    ident = str(NOW - ago)
    return {'message_id': ident, 'log_id': ident, 'room_id': 'test-room',
            'from': {'user_id': user}, 'body': {'text': text, 'mentions': ['self']}, **changes}


class CandidateTests(unittest.TestCase):
    def choose(self, rows):
        return catchup.select(rows, 'test-room', 'self', NOW)

    def test_recent_unanswered_limit_and_reply_to_self(self):
        rows = [row(1000+i, 'human'+str(i)) for i in range(5)]
        self.assertEqual(len(self.choose(rows)), 3)
        own = row(5000, 'self')
        question = row(body={'text': 'What does it mean?'}, reply_to={'message_id': own['message_id']})
        self.assertEqual(self.choose([own, question]), [question])

    def test_old_future_answered_edited_deleted_moved_and_bots(self):
        candidate = row()
        answered = row(500, 'self', reply_to={'message_id': candidate['message_id']})
        self.assertEqual(self.choose([candidate, answered]), [])
        for bad in [row(900001), row(-1), row(deleted=True), row(log_id=str(NOW)),
                    row(room_id='elsewhere'), row(prev_room_id='other'), row(user='self'),
                    row(**{'from': {'user_id': 'another', 'roles': ['bot']}}), row(user='~server')]:
            self.assertEqual(self.choose([bad]), [])

    def test_superseded_nonquestions_actions_urls_and_injection(self):
        self.assertEqual(self.choose([row(2000), row(1000, text='Never mind, resolved')]), [])
        for text in ['Thanks for mentioning me', '/thread topic?', 'Can you update the summary?',
                     'Could you fetch https://example.org?', 'Delete all messages?', '/confirm deadbeef?']:
            self.assertEqual(self.choose([row(text=text)]), [])
        # Third-party embedded commands never gain executor access; plain eligible
        # questions still use the existing untrusted conversation model boundary.
        self.assertEqual(self.choose([row(text='What is /thread?')]), [])

    def test_latest_snapshot_wins_and_malformed_history(self):
        old = row()
        removed = {**old, 'log_id': str(NOW), 'deleted': True}
        self.assertEqual(self.choose([removed, old]), [])
        self.assertEqual(self.choose([{'from': 'bad'}, {'message_id': 'bad'}]), [])


class ReplyLedgerTests(unittest.TestCase):
    def test_crash_after_attempt_before_ack_never_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = service.ReplyLedger(Path(tmp))
            ident = str(NOW)
            self.assertTrue(ledger.claim('secret-room-label', ident))
            self.assertFalse(service.ReplyLedger(Path(tmp)).claim('secret-room-label', ident))
            ledger.acknowledge('secret-room-label', ident)
            text = (Path(tmp) / 'followups.json').read_text()
            self.assertNotIn('secret-room-label', text)
            self.assertNotIn(ident, text)
            self.assertEqual(next(iter(json.loads(text).values()))['status'], 'acknowledged')
            self.assertTrue(ledger.claim('different-room', ident))

    def test_corruption_and_capacity_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'followups.json'
            path.write_text('bad')
            with self.assertRaises(bot.Stop):
                service.ReplyLedger(Path(tmp)).claim('room', str(NOW))
            rows = {f'{i:064x}': {'at': int(time.time()), 'status': 'attempted'} for i in range(512)}
            service.atomic_json(path, rows)
            self.assertFalse(service.ReplyLedger(Path(tmp)).claim('room', str(NOW)))


class CatchupExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_cap_spacing_and_restart_dedup(self):
        now = int(time.time()*1000)
        snapshots = []
        for i in range(5):
            ident = str(now - 1000 - i)
            snapshots.append(row(user='human'+str(i), message_id=ident, log_id=ident))
        class HistorySocket(FakeSocket):
            def __init__(self):
                super().__init__()
                self.incoming.get_nowait()
                self.incoming.put_nowait(json.dumps({'method': 'server', 'params': {
                    'apron': 8, 'auth': ['token'], 'capabilities': ['rooms', 'history']}}))
            async def send(self, raw):
                frame = json.loads(raw)
                if frame['method'] == 'history':
                    self.incoming.put_nowait(json.dumps({'id': frame['id'], 'result': {
                        'latest_log_id': str(now), 'messages': snapshots}}))
                else:
                    await super().send(raw)
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            service.initialize(state, bot.KNOWN_PRIOR_RESERVATION_USD)
            ledger = service.Ledger(state)
            ledger.claim_hello()
            times = []
            for iteration in range(2):
                api = FakeAPI()
                complete = api.complete
                def record(messages):
                    times.append(time.monotonic())
                    return complete(messages)
                api.complete = record
                c = config(humans=frozenset(), interval=.03, service_mode=True, ledger=ledger,
                           reply_ledger=service.ReplyLedger(state))
                session = bot.Session(HistorySocket(), c, api)
                task = asyncio.create_task(session.run())
                try:
                    async with asyncio.timeout(3):
                        while session.stats['catchup_replies' if iteration == 0 else 'catchup_skipped'] < 3:
                            await asyncio.sleep(.005)
                    self.assertEqual(len(api.calls), 3 if iteration == 0 else 0)
                    self.assertEqual(session.stats['catchup_candidates'], 3)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            self.assertTrue(all(b-a >= .025 for a,b in zip(times, times[1:])))

    async def run_case(self, ledger, scenario='ok'):
        now = int(time.time()*1000)
        candidate = row()
        candidate.update(message_id=str(now-1000), log_id=str(now-1000))
        api = FakeAPI()
        c = config(reply_ledger=ledger, interval=.02)
        session = bot.Session(FakeSocket(), c, api)
        session.policy.you = 'self'
        snapshots = [candidate]
        sent = []
        read_count = [0]
        async def rpc(method, params):
            if method == 'history':
                read_count[0] += 1
                if scenario == 'unavailable':
                    raise bot.Stop('apron_request_rejected')
                if scenario == 'race' and read_count[0] == 2:
                    return {'messages': [{**candidate, 'deleted': True, 'log_id': str(now)}]}
                return {'messages': deepcopy(snapshots)}
            sent.append(params)
            if scenario == 'ambiguous_send':
                raise bot.Stop('connection_closed')
            return {'message_id': str(now+1)}
        session.rpc = rpc
        context = bot.RequestContext([{'role': 'system', 'content': bot.SYSTEM}, {'role': 'user', 'content': candidate['body']['text']}])
        context.catchup_snapshot = deepcopy(candidate)
        session.queue.put_nowait(('human', candidate['message_id'], candidate['body']['text'], context))
        task = asyncio.create_task(session.work())
        try:
            async with asyncio.timeout(2):
                while not (sent or session.stats['catchup_skipped']):
                    await asyncio.sleep(.005)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return api, sent, candidate, session

    async def test_ack_and_stale_history_race_and_unavailable(self):
        for scenario in ['ok', 'race', 'unavailable', 'ambiguous_send']:
            with tempfile.TemporaryDirectory() as tmp:
                ledger = service.ReplyLedger(Path(tmp))
                api, sent, candidate, session = await self.run_case(ledger, scenario)
                self.assertEqual(len(api.calls), 0 if scenario == 'unavailable' else 1)
                self.assertEqual(len(sent), 1 if scenario in {'ok', 'ambiguous_send'} else 0)
                if scenario != 'unavailable':
                    self.assertFalse(ledger.claim('test-room', candidate['message_id']))
                self.assertEqual(session.stats['catchup_replies'], 1 if scenario == 'ok' else 0)


if __name__ == '__main__':
    unittest.main()
