"""Synthetic service lifecycle tests; no external sockets or real credentials."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from types import SimpleNamespace
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout

import bot
import service
from test_bot import FakeSocket, FakeAPI, config, message


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        service.initialize(self.state, bot.KNOWN_PRIOR_RESERVATION_USD)
        self.ledger = service.Ledger(self.state)

    def tearDown(self):
        self.tmp.cleanup()

    def test_reservation_survives_restart_and_cannot_reset(self):
        self.ledger.reserve(Decimal('.5'))
        other = service.Ledger(self.state)
        self.assertEqual(other.total, bot.KNOWN_PRIOR_RESERVATION_USD + Decimal('.5'))
        with self.assertRaisesRegex(bot.Stop, 'already_initialized'):
            service.initialize(self.state, bot.KNOWN_PRIOR_RESERVATION_USD)
        with self.assertRaisesRegex(bot.Stop, 'budget_limit'):
            other.reserve(Decimal('5'))

    def test_missing_corrupt_and_invalid_ledger_fail_closed(self):
        for content in ('{}', 'invalid', '{"cumulative_reserved_usd":"NaN"}'):
            self.ledger.path.write_text(content)
            with self.assertRaisesRegex(bot.Stop, 'ledger_invalid'):
                self.ledger.reserve(Decimal('.1'))
        self.ledger.path.unlink()
        with self.assertRaisesRegex(bot.Stop, 'ledger_invalid'):
            self.ledger.claim_hello()
        with self.assertRaisesRegex(bot.Stop, 'already_initialized'):
            service.initialize(self.state, bot.KNOWN_PRIOR_RESERVATION_USD)

    def test_concurrent_reservations_never_exceed_five(self):
        def attempt(_):
            try:
                service.Ledger(self.state).reserve(Decimal('.3'))
                return 1
            except bot.Stop:
                return 0
        with ThreadPoolExecutor(max_workers=8) as pool:
            succeeded = sum(pool.map(attempt, range(30)))
        self.assertEqual(succeeded, 15)
        self.assertEqual(self.ledger.total, bot.KNOWN_PRIOR_RESERVATION_USD + Decimal('4.5'))
        self.assertLessEqual(self.ledger.total, 5)

    def test_atomic_write_failure_prevents_paid_call_and_keeps_previous_ledger(self):
        before = self.ledger.total
        with patch('service.os.replace', side_effect=OSError('synthetic')):
            with self.assertRaisesRegex(bot.Stop, 'ledger_invalid'):
                self.ledger.reserve(Decimal('.1'))
        self.assertEqual(self.ledger.total, before)
        self.assertFalse(list(self.state.glob('.atomic-*')))

    def test_hello_claim_persists_before_any_send(self):
        self.assertTrue(self.ledger.claim_hello())
        self.assertFalse(service.Ledger(self.state).claim_hello())
        self.assertEqual(self.ledger.total, bot.KNOWN_PRIOR_RESERVATION_USD)

    def test_singleton_and_inherited_descriptor(self):
        with service.singleton(self.state) as lock:
            service.validate_lock(self.state, lock.fileno())
            with self.assertRaisesRegex(bot.Stop, 'already_running'):
                service.singleton(self.state)
            code = ('from pathlib import Path; import service; '
                    'service.validate_lock(Path(__import__("sys").argv[1]),int(__import__("sys").argv[2])); '
                    'print("inherited_lock_valid")')
            output = subprocess.check_output([sys.executable, '-B', '-c', code, str(self.state), str(lock.fileno())],
                                             pass_fds=(lock.fileno(),))
            self.assertEqual(output.strip(), b'inherited_lock_valid')
        with service.singleton(self.state):
            pass

    def test_log_filter_drops_payloads_and_unknown_fields(self):
        raw = {'status':'api_failure', 'category':'timeout', 'phase':'completion', 'http_status':0,
               'recoverable':True, 'body':'synthetic-secret', 'headers':{'Authorization':'synthetic-secret'},
               'counts':{'calls':1,'payload':'synthetic-secret'}}
        filtered = service.safe_event(json.dumps(raw))
        self.assertNotIn('synthetic', json.dumps(filtered))
        self.assertEqual(filtered['counts'], {'calls':1})
        self.assertIsNone(service.safe_event('synthetic wire data'))
        self.assertIsNone(service.safe_event(json.dumps({'status':'synthetic-secret'})))
        self.assertIsNone(service.safe_event('x'*17000))

    def test_supervisor_restarts_only_transient_worker_and_writes_safe_state(self):
        real_popen = subprocess.Popen
        launches = []
        def fake_worker(*args, **kwargs):
            code = ('import json; print(json.dumps({"status":"connection_closed"}),flush=True); raise SystemExit(75)'
                    if not launches else
                    'import json; print(json.dumps({"status":"ready","counts":{"ready":1},"payload":"synthetic-secret"}),flush=True)')
            launches.append(1)
            return real_popen([sys.executable, '-u', '-c', code], **kwargs)
        with service.singleton(self.state) as lock, \
                patch('service.deployment', return_value={'root':str(Path.cwd()),'commit':'a'*40}), \
                patch('service.subprocess.Popen', side_effect=fake_worker), \
                patch('service.RECONNECT_BASE_SECONDS', 0):
            service.supervise(self.state, lock.fileno())
        self.assertEqual(len(launches), 2)
        state = service.status(self.state)
        self.assertEqual(state['state'], 'halted')
        self.assertFalse(state['worker_alive'])
        self.assertEqual(state['restarts'], 1)
        self.assertNotIn('synthetic-secret', (self.state/'metadata.log').read_text())


class SessionServiceTests(unittest.IsolatedAsyncioTestCase):
    async def until(self, check):
        async with asyncio.timeout(3):
            while not check():
                await asyncio.sleep(.005)

    async def test_reconnect_skips_hello_and_history_replay_with_persistent_budget(self):
        class HistorySocket(FakeSocket):
            def __init__(self):
                super().__init__()
                self.incoming.get_nowait()
                self.incoming.put_nowait(json.dumps({'method':'server','params':{
                    'apron':8,'auth':['token'],'capabilities':['rooms','history']}}))
            async def send(self, raw):
                frame = json.loads(raw)
                if frame['method'] == 'history':
                    self.sent.append(frame)
                    self.incoming.put_nowait(json.dumps({'id':frame['id'],'result':{
                        'latest_log_id':'100','messages':[message('99')['params']]}}))
                else:
                    await super().send(raw)
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            service.initialize(state, bot.KNOWN_PRIOR_RESERVATION_USD)
            ledger = service.Ledger(state)
            for iteration in range(2):
                socket, api = HistorySocket(), FakeAPI()
                conf = config(ledger=ledger, service_mode=True, prior_spend=ledger.total,
                              budget=Decimal('5')-ledger.total)
                session = bot.Session(socket, conf, api)
                task = asyncio.create_task(session.run())
                try:
                    await self.until(lambda:session.stats['ready'])
                    self.assertEqual(session.stats['hello_acknowledged'], 1 if iteration == 0 else 0)
                    self.assertFalse(api.calls)
                    if iteration:
                        socket.incoming.put_nowait(json.dumps(message('99')))
                        socket.incoming.put_nowait(json.dumps(message('101')))
                        await self.until(lambda:session.stats['replies'] == 1)
                        self.assertEqual(len(api.calls),1)
                        self.assertEqual(ledger.total,bot.KNOWN_PRIOR_RESERVATION_USD+Decimal('.02'))
                        self.assertEqual(ledger.read()['attempts'],1)
                finally:
                    task.cancel()
                    await asyncio.gather(task,return_exceptions=True)

    async def test_service_requires_a_ledger(self):
        with self.assertRaisesRegex(bot.Stop, "ledger_invalid"):
            await bot.live(config(service_mode=True))

    async def test_service_has_no_runtime_alarm_but_keeps_operator_shutdown(self):
        class Connect:
            def __init__(self,*args,**kwargs): pass
            async def __aenter__(self): return FakeSocket()
            async def __aexit__(self,*args): pass
        loop = asyncio.get_running_loop()
        def register(sig, callback):
            if sig == service.signal.SIGTERM:
                loop.call_later(.05,callback)
        with patch('websockets.asyncio.client.connect',Connect), \
                patch('bot.Darkbloom',return_value=FakeAPI()), \
                patch('signal.signal'), patch('signal.alarm') as alarm, \
                patch.object(loop,'add_signal_handler',register), redirect_stdout(io.StringIO()):
            reason = await bot.live(config(service_mode=True,runtime=1,ledger=SimpleNamespace(claim_hello=lambda:True)))
        self.assertEqual(reason,'operator_stop')
        alarm.assert_called_once_with(5)


if __name__ == '__main__':
    with patch('socket.socket.connect',side_effect=AssertionError('Network prohibited in tests')), redirect_stdout(io.StringIO()):
        unittest.main()
