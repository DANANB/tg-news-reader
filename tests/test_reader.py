"""Offline unit tests: no Telegram network or Home Assistant required."""
import asyncio
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

# Do not require the actual Telethon package for offline tests.
telethon = types.ModuleType('telethon')
telethon.TelegramClient = object
telethon_errors = types.ModuleType('telethon.errors')
telethon_errors.SessionPasswordNeededError = type('SessionPasswordNeededError', (Exception,), {})
sys.modules.setdefault('telethon', telethon)
sys.modules.setdefault('telethon.errors', telethon_errors)
# The app does require aiohttp; in automated offline lint environments that
# don't have it, use a minimal module stub to test the isolated collector.
try:
    from aiohttp import web  # noqa
except ImportError:
    aiohttp = types.ModuleType('aiohttp')
    aiohttp.web = types.SimpleNamespace(
        HTTPConflict=type('HTTPConflict', (Exception,), {}),
        HTTPBadRequest=type('HTTPBadRequest', (Exception,), {}),
    )
    sys.modules['aiohttp'] = aiohttp
spec = importlib.util.spec_from_file_location('telegram_reader', Path(__file__).parents[1] / 'telegram_news' / 'app.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class FakeMessage:
    def __init__(self, msg_id, body):
        self.id = msg_id
        self.message = body
        self.date = datetime(2026, 10, 23, tzinfo=timezone.utc)


class FakeClient:
    def __init__(self, messages):
        self.messages = messages

    async def get_entity(self, name):
        if name not in self.messages:
            raise ValueError('Channel inaccessible')
        return name

    async def get_messages(self, entity, limit=1):
        entries = self.messages[entity]
        return entries[-limit:][::-1]

    async def iter_messages(self, entity, min_id=0, reverse=False, limit=None):
        items = [msg for msg in self.messages[entity] if msg.id > min_id]
        if not reverse:
            items.reverse()
        for msg in items[:limit]:
            yield msg


class TestReader(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = Path(self.temp.name)
        for attr, filename in [('DATA', None), ('OPTIONS', 'options.json'), ('STATE', 'state.json'), ('PENDING', 'pending.json'), ('TOKEN', 'api_token'), ('CSRF', 'csrf_token')]:
            setattr(module, attr, path if filename is None else path / filename)
        module.OPTIONS.write_text(json.dumps({'api_id': 123, 'api_hash': 'example',
             'channels': ['FotrosResistancee', 'Middle_East_Spectator'],
             'max_posts_per_channel': 2, 'max_chars_per_post': 800,
             'briefing_word_limit': 65, 'deduplicate_stories': True}))
        self.collector = module.Collector()
        self.fake = FakeClient({'FotrosResistancee': [FakeMessage(10, 'initial')],
                                'Middle_East_Spectator': [FakeMessage(90, 'initial')]})
        self.collector.client = self.fake
        self.collector.authorized = AsyncMock(return_value=True)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_initial_skips_history_then_batches_and_commits(self):
        self.assertEqual((await self.collector.prepare())['status'], 'initialized')
        self.assertEqual(module.load_json(module.STATE)['ids'], {'FotrosResistancee': 10, 'Middle_East_Spectator': 90})
        self.fake.messages['FotrosResistancee'] += [FakeMessage(11, 'a'), FakeMessage(12, 'b'), FakeMessage(13, 'c')]
        self.fake.messages['Middle_East_Spectator'] += [FakeMessage(91, 'd')]
        pending = await self.collector.prepare()
        self.assertEqual(pending['status'], 'ready')
        self.assertEqual(pending['post_count'], 3)
        self.assertTrue(pending['more_may_remain'])
        self.assertEqual(pending['next_ids']['FotrosResistancee'], 12)
        self.assertEqual(pending['briefing_word_limit'], 65)
        self.assertTrue(pending['deduplicate_stories'])
        # Until commit, offsets remain unchanged; preparing twice does not drop items.
        self.assertEqual((await self.collector.prepare())['batch_id'], pending['batch_id'])
        self.assertEqual(module.load_json(module.STATE)['ids']['FotrosResistancee'], 10)
        await self.collector.commit(pending['batch_id'], 'Briefing one')
        self.assertEqual(module.load_json(module.STATE)['ids']['FotrosResistancee'], 12)
        more = await self.collector.prepare()
        self.assertEqual(more['post_count'], 1)
        self.assertEqual(more['next_ids']['FotrosResistancee'], 13)

    async def test_added_channel_skips_history_without_resetting_other_sources(self):
        await self.collector.prepare()
        self.fake.messages['AddedChannel'] = [FakeMessage(900, 'old')]
        self.collector.channels = ('FotrosResistancee', 'Middle_East_Spectator', 'AddedChannel')
        changed = await self.collector.prepare()
        self.assertEqual(changed['status'], 'channels_updated')
        self.assertEqual(module.load_json(module.STATE)['ids'], {
            'FotrosResistancee': 10, 'Middle_East_Spectator': 90, 'AddedChannel': 900})
        self.fake.messages['AddedChannel'].append(FakeMessage(901, 'new'))
        nxt = await self.collector.prepare()
        self.assertEqual(nxt['status'], 'ready')
        self.assertIn('new', nxt['posts_text'])
        self.assertNotIn('old', nxt['posts_text'])

    async def test_invalid_urls_rejected(self):
        for value in (['https://evil.example/a'], ['https://t.me/s/a/9'], ['http://t.me/mynews'], [123], ['..'], [''], ['valid_name', '  https://other.site/a']):
            with self.subTest(value=value), self.assertRaises(ValueError):
                module.normalized_channels(value)
        self.assertEqual(module.normalized_channels(['@FotrosResistancee', 'https://t.me/s/Middle_East_Spectator/', 'fotrosresistancee']),
                         ('FotrosResistancee', 'Middle_East_Spectator'))


if __name__ == '__main__':
    unittest.main()
