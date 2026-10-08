"""Opt-in actual aprond tests: APRON_GO_BINARY=/absolute/path/to/aprond.

Local loopback only, memory store, empty credential environment, synthetic data.
No paid model calls. Server stdout/stderr are discarded, never payload logs.
"""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
import uuid
from websockets.asyncio.client import connect
import organizer as o
from test_organizer import fixture_planner


class RPCError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__('local_rpc_rejected')


class Client:
    async def start(self, url, name):
        # Explicit loopback server, not a way around public egress policy.
        self.ws = await connect(url, proxy=None)
        self.pending, self.events = {}, []
        self.server = {}
        self.task = asyncio.create_task(self.receive())
        auth = await self.rpc('auth', {'scheme': 'guest', 'name': name})
        self.you = auth['you']['user_id']
        return self

    async def receive(self):
        async for raw in self.ws:
            frame = json.loads(raw)
            if frame.get('id') in self.pending:
                future = self.pending[frame['id']]
                if 'error' in frame:
                    future.set_exception(RPCError(frame['error']['code']))
                else:
                    future.set_result(frame['result'])
            elif frame.get('method') == 'server':
                self.server = frame['params']
            else:
                self.events.append(frame)

    async def rpc(self, method, params):
        ident = uuid.uuid4().hex
        self.pending[ident] = asyncio.get_running_loop().create_future()
        try:
            await self.ws.send(json.dumps({'id': ident, 'method': method, 'params': params}))
            return await asyncio.wait_for(self.pending[ident], 5)
        finally:
            self.pending.pop(ident, None)

    async def close(self):
        await self.ws.close()
        await self.task


@unittest.skipUnless(os.environ.get('APRON_GO_BINARY'), 'set APRON_GO_BINARY for actual local aprond tests')
class GoIntegration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='apron-local-verification-')
        self.addCleanup(self.temp.cleanup)
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            port = s.getsockname()[1]
        binary = str(Path(os.environ['APRON_GO_BINARY']).resolve(strict=True))
        self.process = subprocess.Popen([binary, '--addr', f'127.0.0.1:{port}', '--store', 'memory'],
            env={'PATH': '/usr/bin:/bin'}, cwd=self.temp.name,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop_server)
        url = f'ws://127.0.0.1:{port}/ws'
        async with asyncio.timeout(10):
            while True:
                try:
                    reader, writer = await asyncio.open_connection('127.0.0.1', port)
                    writer.close()
                    await writer.wait_closed()
                    break
                except OSError:
                    await asyncio.sleep(.05)
        self.bot = await Client().start(url, 'Synthetic bot')
        self.owner = await Client().start(url, 'Synthetic owner')
        self.addAsyncCleanup(self.owner.close)
        self.addAsyncCleanup(self.bot.close)
        self.org = o.Organizer(self.bot.rpc, fixture_planner, {self.owner.you}, {'general'})
        first = await self.bot.rpc('message', {'room_id': 'general',
            'body': {'text': 'deploy topic: migrate Friday', 'format': 'markdown', 'mentions': [self.owner.you]},
            'ext': {'synthetic': {'preserve': 'field'}}})
        self.first = first['message_id']
        second = await self.bot.rpc('message', {'room_id': 'general', 'reply_to': {'message_id': self.first},
            'body': {'text': 'deploy topic: keep a rollback', 'format': 'plain'}})
        self.second = second['message_id']
        self.unrelated = (await self.owner.rpc('message', {'room_id': 'general', 'body': {'text': 'Unrelated lunch discussion'}}))['message_id']

    def stop_server(self):
        self.process.terminate()
        try:
            self.process.wait(3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()

    async def dispatch(self, text):
        # The requester is obtained from a real server-authored snapshot, never
        # a username string supplied by the command text.
        response = await self.owner.rpc('message', {'room_id': 'general', 'body': {'text': text, 'mentions': [self.bot.you]}})
        async with asyncio.timeout(5):
            while True:
                event = next((f['params'] for f in self.bot.events if f.get('method') == 'message'
                              and f['params'].get('message_id') == response['message_id']), None)
                if event:
                    break
                await asyncio.sleep(.01)
        return await self.org.handle(event['from']['user_id'], event['room_id'], event['body']['text'],
            event['message_id'], self.bot.you, [], self.bot.server['capabilities'])

    async def test_actual_thread_moves_preserve_fields_and_other_author_denied(self):
        response = await self.dispatch('thread the discussion about deployment')
        self.assertIn('Preview:', response)
        self.assertEqual(self.org.pending.plan['message_ids'], [self.first, self.second])
        originals = deepcopy(self.org.pending.messages)
        response = await self.dispatch('/confirm ' + self.org.pending.nonce)
        self.assertIn('moved 2 messages', response)
        destination = response.split()[2]
        record, messages = await self.org.snapshot(destination)
        self.assertEqual(record['parent_room_id'], 'general')
        self.assertEqual(record['title'], 'Deploy discussion')
        for ident, original in originals.items():
            for key in o.MESSAGE_FIELDS:
                self.assertEqual(messages[ident].get(key), original.get(key))
            self.assertEqual(messages[ident]['from'], original['from'])
        _, source = await self.org.snapshot('general')
        self.assertIn(self.unrelated, source)
        self.assertNotIn(self.first, source)
        # Unmodified upstream enforces creating identity, unlike CF's mod move.
        original = source[self.unrelated]
        with self.assertRaises(RPCError) as caught:
            await self.bot.rpc('message', {'message_id': self.unrelated, 'room_id': destination, 'body': original['body']})
        self.assertEqual(caught.exception.code, -32001)

    async def test_actual_general_and_thread_summary(self):
        before, _ = await self.org.snapshot('general')
        await self.dispatch('update the summary of this room')
        self.assertIn('Updated', await self.dispatch('/confirm ' + self.org.pending.nonce))
        after, _ = await self.org.snapshot('general')
        self.assertEqual(after.get('title'), before.get('title'))
        self.assertEqual(after['description'], 'Synthetic deployment decisions.')
        child = (await self.bot.rpc('room_set', {'parent_room_id': 'general', 'title': 'Original child title', 'description': 'Old description'}))['room_id']
        await self.bot.rpc('message', {'room_id': child, 'body': {'text': 'Synthetic child context'}})
        self.org.rooms.add(child)
        self.org.pending = None
        # Use a real trigger in child; server-authenticated owner identity retained.
        trigger = await self.owner.rpc('message', {'room_id': child, 'body': {'text': '/summary'}})
        preview = await self.org.handle(self.owner.you, child, '/summary', trigger['message_id'], self.bot.you, [], self.bot.server['capabilities'])
        self.assertIn('Preview:', preview)
        result = await self.org.handle(self.owner.you, child, '/confirm ' + self.org.pending.nonce, trigger['message_id'], self.bot.you, [], self.bot.server['capabilities'])
        self.assertIn('Updated', result)
        record, _ = await self.org.snapshot(child)
        self.assertEqual(record['title'], 'Original child title')
        self.assertEqual(record['description'], 'Synthetic deployment decisions.')

    async def test_actual_conflict_unauthorized_and_partial_stop(self):
        preview = await self.dispatch('/thread deploy')
        self.assertIn('Preview:', preview)
        original = self.org.pending.messages[self.first]
        await self.bot.rpc('message', {'message_id': self.first, 'room_id': 'general', 'body': {'text': 'deploy topic: edited'}})
        self.assertIn('no changes', await self.dispatch('/confirm ' + self.org.pending.nonce))
        await self.dispatch('/thread deploy')
        nonce = self.org.pending.nonce
        self.assertIn('not authorized', await self.org.handle(self.bot.you, 'general', '/confirm ' + nonce, '99', self.bot.you, [], self.bot.server['capabilities']))
        # Interleave a genuine conflicting edit after the first move.
        async def racing_rpc(method, params):
            result = await self.bot.rpc(method, params)
            if method == 'message' and params.get('message_id') == self.first:
                await self.bot.rpc('message', {'message_id': self.second, 'room_id': 'general', 'body': {'text': 'Concurrent synthetic edit'}})
            return result
        self.org.rpc = racing_rpc
        result = await self.dispatch('/confirm ' + nonce)
        self.assertIn('1 moves confirmed', result)
        self.assertIn('move_check', result)


if __name__ == '__main__':
    unittest.main()
