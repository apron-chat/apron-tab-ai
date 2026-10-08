"""Explicit opt-in paid planner verification on a local synthetic Apron server.

Run only with APRON_SYNTHETIC_PLANNER=1 and APRON_GO_BINARY set. At most two
model calls in this scenario, three across this process, no automatic retries.
Never reads live Apron traffic; only the model key and durable spend ledger are
shared with production. Raw model/API payloads are not printed or persisted.
"""
import asyncio
from copy import deepcopy
from decimal import Decimal
import json
import os
import unittest
import bot
import organizer
import service
import test_go_integration as local_go

ATTEMPTS = 0


@unittest.skipUnless(os.environ.get('APRON_SYNTHETIC_PLANNER') == '1' and os.environ.get('APRON_GO_BINARY'),
                     'explicit paid synthetic planner opt-in required')
class SyntheticPlanner(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = local_go.GoIntegration.asyncSetUp
    stop_server = local_go.GoIntegration.stop_server
    dispatch = local_go.GoIntegration.dispatch

    async def test_actual_model_selects_and_executes_then_updates_summary(self):
        global ATTEMPTS
        self.assertEqual(os.environ.get('DARKBLOOM_BASE_URL'), bot.API_URL)
        ledger = service.Ledger(service.DEFAULT_STATE)
        config = bot.Config(token='synthetic-local-unused-apron-token',
            api_key=os.environ['DARKBLOOM_API_KEY'], room='general', humans=frozenset(),
            budget=Decimal('5') - ledger.total, prior_spend=ledger.total)
        api = bot.Darkbloom(config)
        reserve = await asyncio.to_thread(api.reservation)
        self.assertLessEqual(reserve, Decimal('0.04'))
        plans = []

        async def real_planner(messages):
            global ATTEMPTS
            if ATTEMPTS >= 3:
                raise RuntimeError('synthetic_attempt_limit')
            ledger.reserve(reserve)  # atomic, before dispatch, never refunded
            ATTEMPTS += 1
            text = await asyncio.to_thread(api.complete, messages)
            try:
                parsed = json.loads(text)
            except Exception:
                raise RuntimeError('synthetic_plan_invalid_json') from None
            plans.append(parsed)
            return text

        self.org.planner = real_planner
        # Fictional topic is interleaved with two irrelevant lunch messages.
        await self.bot.rpc('message', {'message_id': self.first, 'room_id': 'general',
            'body': {'text': 'Project Aster deployment decision: launch Friday after the staging checks pass.',
                     'format': 'markdown', 'mentions': [self.owner.you]},
            'ext': {'synthetic': {'preserve': 'aster-field'}}})
        await self.bot.rpc('message', {'room_id': 'general',
            'body': {'text': 'Lunch decision: order sandwiches on Tuesday. This is unrelated to Aster.'}})
        await self.bot.rpc('message', {'message_id': self.second, 'room_id': 'general',
            'reply_to': {'message_id': self.first},
            'body': {'text': 'Project Aster deployment rollback: restore the previous release if staging checks fail.', 'format': 'plain'}})
        preview = await self.dispatch('/thread Project Aster deployment decisions and rollback, excluding lunch')
        self.assertTrue(preview.startswith('Preview:'), 'actual planner did not yield a valid preview')
        pending = self.org.pending
        self.assertTrue(set(pending.plan['message_ids']) == {self.first, self.second}, 'topic selection mismatch')
        title = pending.plan['title'].lower()
        summary = pending.plan['summary'].lower()
        self.assertTrue('aster' in title, 'title did not identify requested project')
        self.assertTrue('friday' in summary and any(x in summary for x in ('rollback', 'previous release', 'restore')),
                        'summary missed launch timing or rollback')
        self.assertTrue('lunch' not in summary and 'sandwich' not in summary, 'unrelated topic leaked into summary')
        originals = deepcopy(pending.messages)
        nonce = pending.nonce
        before, source = await self.org.snapshot('general')
        wrong = await self.org.handle(self.bot.you, 'general', '/confirm ' + nonce,
            str(int(self.second) + 1), self.bot.you, [], self.bot.server['capabilities'])
        self.assertTrue('not authorized' in wrong, 'wrong identity confirmation accepted')
        self.assertIs(self.org.pending, pending)
        self.assertTrue(all(source[i]['room_id'] == 'general' for i in originals))
        result = await self.dispatch('/confirm ' + nonce)
        self.assertTrue('moved 2 messages' in result, 'local execution did not complete')
        destination = result.split()[2]
        child, moved = await self.org.snapshot(destination)
        self.assertTrue(set(moved) == set(originals), 'unexpected messages moved')
        for ident, original in originals.items():
            self.assertTrue(all(moved[ident].get(k) == original.get(k) for k in organizer.MESSAGE_FIELDS),
                            'client field preservation failed')
            self.assertTrue(moved[ident]['from'] == original['from'], 'author changed')
        _, remaining = await self.org.snapshot('general')
        self.assertTrue(self.unrelated in remaining, 'unrelated message moved')
        self.assertTrue('No matching' in await self.dispatch('/confirm ' + nonce), 'confirmation replay accepted')
        print(json.dumps({'synthetic_thread_planner_correct': True, 'local_moves_verified': 2,
                          'confirmation_identity_and_replay_verified': True}), flush=True)

        # Second and final call summarizes only the two-message synthetic child.
        self.org.rooms.add(destination)
        trigger = await self.owner.rpc('message', {'room_id': destination, 'body': {'text': '/summary'}})
        preview = await self.org.handle(self.owner.you, destination, '/summary', trigger['message_id'],
            self.bot.you, [], self.bot.server['capabilities'])
        self.assertTrue(preview.startswith('Preview:'), 'summary planner did not yield preview')
        summary = self.org.pending.plan['summary'].lower()
        self.assertTrue('friday' in summary and any(x in summary for x in ('rollback', 'restore', 'previous release')),
                        'thread summary missed key facts')
        confirm = '/confirm ' + self.org.pending.nonce
        result = await self.org.handle(self.owner.you, destination, confirm, trigger['message_id'],
            self.bot.you, [], self.bot.server['capabilities'])
        self.assertTrue(result.startswith('Updated'), 'summary update failed')
        updated, _ = await self.org.snapshot(destination)
        self.assertTrue(updated.get('title') == child.get('title'), 'summary update changed title')
        self.assertTrue(updated.get('description') == plans[-1]['summary'], 'summary did not match plan')
        print(json.dumps({'synthetic_summary_planner_correct': True, 'local_summary_update_verified': True,
                          'synthetic_attempts': ATTEMPTS, 'synthetic_reserved_usd': str(reserve * ATTEMPTS),
                          'cumulative_reserved_usd': str(ledger.total)}), flush=True)


if __name__ == '__main__':
    unittest.main()
