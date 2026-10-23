"""Telegram Channel Briefings: authenticated HA ingress + MTProto collector.

Only channels explicitly set in Home Assistant app configuration are queried.
No Telegram sending, no network ports published to the host. The private
briefing API requires a separately generated 256-bit bearer token.
"""
import asyncio
import hmac
import html
import json
import logging
import os
import re
import secrets
import tempfile
from pathlib import Path
from uuid import uuid4

from aiohttp import web
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

DATA = Path('/data')
OPTIONS = DATA / 'options.json'
STATE = DATA / 'state.json'
PENDING = DATA / 'pending.json'
TOKEN = DATA / 'api_token'
CSRF = DATA / 'csrf_token'
SESSION = str(DATA / 'telegram_session')
TOTAL_LIMIT = 19000  # hard cap on AI-bound payload (app security invariant)
CHANNEL_RE = re.compile(r"[A-Za-z0-9_]{5,32}\Z")
MAX_CHANNELS = 20


def normalized_channels(raw):
    """Allow only Telegram usernames / t.me channel URLs, never arbitrary URLs."""
    if not isinstance(raw, list) or len(raw) > MAX_CHANNELS:
        raise ValueError('channels must be a list of at most 20 public usernames')
    channels = []
    for entry in raw:
        if not isinstance(entry, str):
            raise ValueError('Each channel must be text')
        name = entry.strip()
        if name.startswith('https://t.me/s/'):
            name = name[len('https://t.me/s/'):]
        elif name.startswith('https://t.me/'):
            name = name[len('https://t.me/'):]
        elif name.startswith('@'):
            name = name[1:]
        name = name.rstrip('/')
        if not CHANNEL_RE.fullmatch(name):
            raise ValueError('Use public channel usernames, @names, or https://t.me/s/name only')
        if name.lower() not in (v.lower() for v in channels):
            channels.append(name)
    return tuple(channels)
LOG = logging.getLogger('nabu_news')


def atomic_json(path, obj):
    """Write data atomically, so power failures cannot corrupt checkpoints."""
    fd, temp = tempfile.mkstemp(dir=str(DATA), prefix='.tmp_')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(obj, out, ensure_ascii=False)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def load_json(path, default=None):
    if not path.exists():
        return default
    with path.open(encoding='utf-8') as f:
        return json.load(f)


def private_token(path):
    if not path.exists():
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write(secrets.token_hex(32))
    return path.read_text().strip()


def render_page(body):
    return web.Response(text=(
        '<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta charset="utf-8"><title>Telegram Channel Briefings</title><style>'
        'body{font:16px system-ui,sans-serif;max-width:690px;padding:22px;margin:auto;line-height:1.5;}'
        'input,button{font:inherit;padding:9px;margin:5px 0;max-width:100%;}'
        'label{display:block;margin-top:12px}button{cursor:pointer}code{overflow-wrap:anywhere}'
        '.notice{padding:12px;border:1px solid #888;border-radius:8px}'
        '</style></head><body><h1>Telegram Channel Briefings</h1>' + body + '</body></html>'),
        content_type='text/html', headers={'Cache-Control': 'no-store',
                                          'X-Content-Type-Options':'nosniff',
                                          'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'self'"})


def ingress_only(request):
    # Home Assistant Supervisor proxies all ingress requests from this address.
    if request.remote != '172.30.32.2':
        raise web.HTTPForbidden(text='Use Open Web UI through Home Assistant.')


def require_form_csrf(data):
    if not hmac.compare_digest(str(data.get('csrf', '')), private_token(CSRF)):
        raise web.HTTPForbidden(text='Invalid form token.')


def api_authorize(request):
    supplied = request.headers.get('Authorization', '')
    wanted = 'Bearer ' + private_token(TOKEN)
    if not hmac.compare_digest(supplied, wanted):
        raise web.HTTPUnauthorized(text='Unauthorized')


class Collector:
    def __init__(self):
        options = load_json(OPTIONS, {})
        self.api_id = int(options.get('api_id', 0))
        self.api_hash = str(options.get('api_hash', ''))
        self.channels = normalized_channels(options.get('channels', []))
        self.per_channel_limit = max(1, min(100, int(options.get('max_posts_per_channel', 25))))
        self.text_limit = max(100, min(2000, int(options.get('max_chars_per_post', 800))))
        self.summary_word_limit = max(30, min(200, int(options.get('briefing_word_limit', 75))))
        self.deduplicate = bool(options.get('deduplicate_stories', True))
        self.client = None
        self.lock = asyncio.Lock()
        self.phone = None
        self.awaiting_2fa = False

    async def connect(self):
        if not self.api_id or not self.api_hash:
            raise RuntimeError('Set Telegram API ID and API Hash in the app Configuration tab.')
        if self.client is None:
            self.client = TelegramClient(SESSION, self.api_id, self.api_hash,
                                         auto_reconnect=True, request_retries=2,
                                         connection_retries=2)
        if not self.client.is_connected():
            await asyncio.wait_for(self.client.connect(), 20)
        return self.client

    async def authorized(self):
        client = await self.connect()
        return await asyncio.wait_for(client.is_user_authorized(), 15)

    async def latest_ids(self):
        result = {}
        for channel in self.channels:
            entity = await self.client.get_entity(channel)
            recent = await self.client.get_messages(entity, limit=1)
            result[channel] = int(recent[0].id) if recent else 0
        return result

    async def prepare(self):
        async with self.lock:
            if not await self.authorized():
                return {'status': 'login_required', 'message': 'Sign into Telegram using the app Open Web UI.'}
            if not self.channels:
                return {'status': 'configuration_required', 'message': 'Configure at least one public Telegram channel in the app Configuration tab.'}
            state = load_json(STATE)
            if state is None:
                ids = await self.latest_ids()  # all channels must succeed
                atomic_json(STATE, {'ids': ids, 'last_briefing': ''})
                return {'status': 'initialized', 'message': f'Tracking started at the latest posts in {len(ids)} channel(s). Older posts were skipped.'}
            base = state['ids']
            existing = set(base)
            configured = set(self.channels)
            if existing != configured:
                # A newly added source starts at its latest message; existing ones
                # retain their checkpoints. Removed sources are no longer queried.
                additions = [name for name in self.channels if name not in base]
                latest = {}
                for channel in additions:
                    entity = await self.client.get_entity(channel)
                    recent = await self.client.get_messages(entity, limit=1)
                    latest[channel] = int(recent[0].id) if recent else 0
                revised = {channel: (base[channel] if channel in base else latest[channel])
                           for channel in self.channels}
                atomic_json(STATE, {'ids': revised, 'last_briefing': state.get('last_briefing', '')})
                # A pending (uncommitted) batch may contain removed sources and
                # MUST NOT be committed under a new source configuration.
                PENDING.unlink(missing_ok=True)
                return {'status': 'channels_updated', 'message':
                        f'Channel list updated. {len(additions)} new channel(s) start from their latest post; existing reading positions are retained. Ask again for news.'}
            pending = load_json(PENDING)
            if pending is not None:
                return {'status': 'ready', **pending}
            next_ids = dict(base)
            messages = []
            truncated = False
            for channel in self.channels:
                entity = await self.client.get_entity(channel)
                # Oldest-first, so any backlog beyond the limit is left unread.
                iterator = self.client.iter_messages(
                    entity, min_id=int(base[channel]), reverse=True,
                    limit=self.per_channel_limit + 1)
                fetched = []
                async for message in iterator:
                    fetched.append(message)
                if len(fetched) > self.per_channel_limit:
                    truncated = True
                    fetched = fetched[:self.per_channel_limit]
                for msg in fetched:
                    next_ids[channel] = max(next_ids[channel], int(msg.id))
                    # Every post advances the checkpoint, including media-only posts.
                    body = (msg.message or '').strip()
                    if not body:
                        body = '[Media-only post, contents not analyzed]'
                    body = ' '.join(body.split())[:self.text_limit]
                    messages.append({'channel': channel, 'id': int(msg.id),
                                     'date': msg.date.isoformat() if msg.date else '',
                                     'text': body, 'url': f'https://t.me/{channel}/{msg.id}'})
            if not messages:
                return {'status': 'no_new', 'message': 'There are no new posts in the configured Telegram channels.'}
            messages.sort(key=lambda m: (m['date'], m['channel'], m['id']))
            lines = []
            used = 0
            # Do not skip later checkpoints because of prompt-size bounds:
            # return an error, rather than silently discarding unread posts.
            for msg in messages:
                line = f"[{msg['channel']} #{msg['id']} {msg['date']}] {msg['text']} ({msg['url']})"
                used += len(line) + 1
                if used > TOTAL_LIMIT:
                    raise RuntimeError('This batch exceeds the safe AI input limit; lower max_posts_per_channel in app settings.')
                lines.append(line)
            pending = {'batch_id': str(uuid4()), 'posts_text': '\n'.join(lines),
                       'post_count': len(messages), 'more_may_remain': truncated,
                       'next_ids': next_ids,
                       'briefing_word_limit': self.summary_word_limit,
                       'deduplicate_stories': self.deduplicate}
            atomic_json(PENDING, pending)
            return {'status': 'ready', **pending}

    async def commit(self, batch_id, summary):
        async with self.lock:
            pending = load_json(PENDING)
            if pending is None or not hmac.compare_digest(str(batch_id), str(pending.get('batch_id', ''))):
                raise web.HTTPConflict(text='Pending batch does not match.')
            if not isinstance(summary, str) or not 1 <= len(summary.strip()) <= 1500:
                raise web.HTTPBadRequest(text='Invalid summary length.')
            atomic_json(STATE, {'ids': pending['next_ids'], 'last_briefing': summary.strip()})
            PENDING.unlink(missing_ok=True)
            return {'status': 'committed', 'briefing': summary.strip(),
                    'post_count': pending['post_count'],
                    'more_may_remain': pending['more_may_remain']}


async def page(request):
    ingress_only(request)
    col = request.app['collector']
    notice = ''
    if not col.api_id or not col.api_hash:
        notice = '<p class="notice">Enter API ID and API Hash in the app Configuration tab, save, and restart.</p>'
        return render_page(notice)
    try:
        signed = await col.authorized()
    except Exception:
        LOG.warning('Telegram connection unavailable (details omitted).')
        return render_page('<p class="notice">Cannot connect to Telegram. See app logs or confirm API credentials.</p>')
    if signed:
        state = load_json(STATE)
        ids = state['ids'] if state else {}
        checkpoints = ', '.join(f'{html.escape(k)}: {int(v)}' for k, v in ids.items()) or 'Not initialized yet'
        configured = ', '.join(html.escape(channel) for channel in col.channels) or '(none configured)'
        return render_page(
            '<p class="notice">Telegram is connected. Only configured channels will be read.</p>'
            f'<p>Configured channels: {configured}</p>'
            f'<p>Last committed message IDs: {checkpoints}</p>'
            '<p>On the first request, each channel starts at the latest post and skips older history. Adding channels later also skips their history.</p>'
            '<p>Paste the following secret into <code>secrets.yaml</code>. Anyone with this token can call the private news API; do not share it.</p>'
            f'<p><code>telegram_news_bearer: "Bearer {private_token(TOKEN)}"</code></p>'
            '<p>Keep this app web UI available only to Home Assistant administrators.</p>')
    csrf = html.escape(private_token(CSRF), quote=True)
    if col.awaiting_2fa:
        form = (f'<form method="post" action="login/password"><input type="hidden" name="csrf" value="{csrf}">'
                '<label>Telegram two-step verification password</label>'
                '<input name="password" type="password" required autocomplete="off"><button>Finish login</button></form>')
    elif col.phone:
        form = (f'<form method="post" action="login/code"><input type="hidden" name="csrf" value="{csrf}">'
                '<label>Telegram login code (sent to your Telegram app)</label>'
                '<input name="code" required autocomplete="off"><button>Confirm code</button></form>')
    else:
        form = (f'<form method="post" action="login/send"><input type="hidden" name="csrf" value="{csrf}">'
                '<label>Telegram phone number (international format, for login only)</label>'
                '<input name="phone" type="tel" placeholder="+..." required autocomplete="off">'
                '<button>Send login code</button></form>')
    return render_page('<p>Sign in to Telegram once. Your session stays in this app\'s private storage.</p>' + form)


async def login_send(request):
    ingress_only(request)
    data = await request.post()
    require_form_csrf(data)
    phone = str(data.get('phone', '')).strip()
    if not (phone.startswith('+') and 7 <= len(phone) <= 20 and phone[1:].isdigit()):
        return render_page('<p>Invalid international phone number. Use the Back button and try again.</p>')
    col = request.app['collector']
    async with col.lock:
        try:
            client = await col.connect()
            await asyncio.wait_for(client.send_code_request(phone), 30)
            col.phone = phone
            col.awaiting_2fa = False
        except Exception:
            LOG.warning('Could not send Telegram login code (details omitted).')
            return render_page('<p>Telegram could not send a login code. Check credentials and try later.</p>')
    return await page(request)


async def login_code(request):
    ingress_only(request)
    data = await request.post()
    require_form_csrf(data)
    code = str(data.get('code', '')).strip().replace(' ', '')
    if not (code.isdigit() and 4 <= len(code) <= 8):
        return render_page('<p>Invalid code format. Go back and retry.</p>')
    col = request.app['collector']
    if not col.phone:
        raise web.HTTPConflict(text='Request a new login code.')
    async with col.lock:
        try:
            await asyncio.wait_for(col.client.sign_in(phone=col.phone, code=code), 30)
            col.phone = None
        except SessionPasswordNeededError:
            col.awaiting_2fa = True
        except Exception:
            LOG.warning('Telegram login code not accepted (details omitted).')
            return render_page('<p>Code not accepted or expired. Return to the app page to retry.</p>')
    return await page(request)


async def login_password(request):
    ingress_only(request)
    data = await request.post()
    require_form_csrf(data)
    col = request.app['collector']
    if not col.awaiting_2fa:
        raise web.HTTPConflict(text='No two-step verification is pending.')
    async with col.lock:
        try:
            await asyncio.wait_for(col.client.sign_in(password=str(data.get('password', ''))), 30)
            col.awaiting_2fa = False
            col.phone = None
        except Exception:
            LOG.warning('Telegram two-step verification failed (details omitted).')
            return render_page('<p>Two-step verification password not accepted. Return and retry.</p>')
    return await page(request)


async def prepare_api(request):
    api_authorize(request)
    try:
        result = await asyncio.wait_for(request.app['collector'].prepare(), 55)
        return web.json_response(result)
    except Exception:
        LOG.exception('Unable to retrieve Telegram posts')
        return web.json_response({'status': 'error', 'message': 'Telegram retrieval failed. Check app logs; checkpoints were not advanced.'}, status=503)


async def commit_api(request):
    api_authorize(request)
    data = await request.json()
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(text='Invalid JSON payload')
    return web.json_response(await request.app['collector'].commit(
        data.get('batch_id', ''), data.get('summary', '')))


async def replay_api(request):
    api_authorize(request)
    state = load_json(STATE, {})
    return web.json_response({'status': 'ok',
                              'briefing': state.get('last_briefing') or 'No previous news briefing is available yet.'})


async def start_app():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    private_token(TOKEN)
    private_token(CSRF)
    col = Collector()
    ingress = web.Application(client_max_size=4096)
    ingress['collector'] = col
    ingress.router.add_get('/', page)
    ingress.router.add_post('/login/send', login_send)
    ingress.router.add_post('/login/code', login_code)
    ingress.router.add_post('/login/password', login_password)
    # No API endpoints in ingress. Conversely, no login endpoints in the HA API.
    api = web.Application(client_max_size=4096)
    api['collector'] = col
    api.router.add_post('/prepare', prepare_api)
    api.router.add_post('/commit', commit_api)
    api.router.add_get('/replay', replay_api)
    ui_runner = web.AppRunner(ingress, access_log=None)
    api_runner = web.AppRunner(api, access_log=None)
    await ui_runner.setup()
    await api_runner.setup()
    await web.TCPSite(ui_runner, '0.0.0.0', 8099).start()
    await web.TCPSite(api_runner, '0.0.0.0', 8765).start()
    LOG.info('Running: ingress UI 8099, private API 8765, no host ports published. Configured sources: %s', len(col.channels))
    try:
        await asyncio.Event().wait()
    finally:
        if col.client and col.client.is_connected():
            await col.client.disconnect()
        await api_runner.cleanup()
        await ui_runner.cleanup()


if __name__ == '__main__':
    asyncio.run(start_app())
