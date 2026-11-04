"""Private bearer-capability pages for one conference capture and its sources."""
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from html import escape
from urllib.parse import quote, urlencode

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, Response

from app.capture_model import CaptureSummary


PRIVATE_HEADERS = {
    'Cache-Control': 'no-store',
    'Referrer-Policy': 'no-referrer',
    'X-Robots-Tag': 'noindex, nofollow, noarchive',
    'X-Content-Type-Options': 'nosniff',
    'Content-Security-Policy': "default-src 'none'; img-src 'self' data:; media-src 'self'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
}
CSS = '''
*{box-sizing:border-box}body{margin:0;background:#faf9f6;color:#262238;font:16px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
a{color:#66549e;text-underline-offset:4px}main{width:min(920px,100%);margin:auto;padding:32px 24px 70px}
.brand{font-size:20px;font-weight:750;letter-spacing:-.7px;color:#605080}.eyebrow{font-size:12px;text-transform:uppercase;letter-spacing:1.8px;color:#746884;margin:36px 0 8px}
h1{font:clamp(32px,6vw,52px)/1.14 Georgia,serif;margin:0 0 18px}h2{font:28px/1.3 Georgia,serif;margin:0 0 16px}h3{font-size:17px;margin:0 0 12px}
p{margin:8px 0 16px}header{padding-bottom:28px;border-bottom:1px solid #e4deea}section{margin-top:32px}
.muted{color:#756e81;font-size:14px}.badge{display:inline-block;border-radius:20px;background:#eee8f7;color:#655283;padding:4px 12px;font-size:12px;font-weight:650}
.notice{border:1px solid #e6dff0;background:#f3eff9;border-radius:14px;padding:18px 22px;margin:24px 0}.notice p:last-child{margin-bottom:0}
.card{background:white;border:1px solid #e7e0eb;border-radius:16px;padding:24px;margin:16px 0;scroll-margin-top:24px}
.card img{display:block;width:100%;height:auto;max-height:650px;object-fit:contain;border-radius:9px;background:#f4f1f7;margin:16px 0}
audio{display:block;width:100%;max-width:100%;margin:16px 0}.source-text{white-space:pre-wrap;overflow-wrap:anywhere}
ul{padding-left:22px}li{padding:6px 0}.citations{font-size:12px;margin-left:8px}.citations a{white-space:nowrap;margin-right:8px}
.meta{display:flex;gap:8px 18px;flex-wrap:wrap;align-items:center}.label{font-size:12px;color:#756e81;text-transform:uppercase;letter-spacing:.7px;margin-top:18px}
footer{border-top:1px solid #e4deea;margin-top:36px;padding-top:24px;font-size:13px;color:#756e81}h1,h2,h3,p,li{overflow-wrap:anywhere}
@media(max-width:600px){main{padding:24px 17px 45px}.card{padding:18px}.notice{padding:16px}.eyebrow{margin-top:28px}h2{font-size:25px}}
'''


def _text(value):
    return escape(str(value if value is not None else ''), quote=True)


def _date(value):
    dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


class CapturePages:
    def __init__(self, captures, users, now, base_url: str, secret: str):
        if not secret:
            raise ValueError('Capture access links require a session secret.')
        self.captures, self.users, self.now = captures, users, now
        self.base_url, self.secret = base_url.rstrip('/'), secret.encode('utf-8')
        self.router = APIRouter()
        self.router.add_api_route('/captures/{session_id}', self.page, methods=['GET'], response_class=HTMLResponse)
        self.router.add_api_route('/captures/{session_id}/items/{item_id}', self.media, methods=['GET'])

    def _signature(self, session, expires):
        payload = json.dumps([session['id'], session['user_id'], expires, session['link_version']], separators=(',', ':'))
        return hmac.new(self.secret, payload.encode('utf-8'), hashlib.sha256).hexdigest()

    def link(self, session):
        expires = int(min(_date(session['expires_at']), self.now() + timedelta(days=7)).timestamp())
        token = f'{expires}.{self._signature(session, expires)}'
        return f'{self.base_url}/captures/{quote(session["id"], safe="")}?{urlencode({"token": token})}'

    def _authorized(self, session_id, token):
        if not token or len(token) > 100 or len(session_id) > 200:
            return None
        # This lookup resolves ownership only; all content is fetched with that owner scope.
        row = self.captures.conn.execute('SELECT user_id FROM capture_sessions WHERE id = ?', (session_id,)).fetchone()
        if row is None:
            return None
        session = self.captures.get(row['user_id'], session_id)
        if session is None:
            return None
        user = self.users.get(session['user_id'])
        if user is None or user.status == 'disabled' or not (user.status == 'active' or user.is_admin):
            return None
        try:
            expires_text, signature = token.split('.', 1)
            expires = int(expires_text)
            now = self.now()
            if (expires <= now.timestamp() or expires > _date(session['expires_at']).timestamp()
                    or _date(session['expires_at']) <= now
                    or expires > (now + timedelta(days=7)).timestamp()
                    or not hmac.compare_digest(signature, self._signature(session, expires))):
                return None
        except (ValueError, TypeError, OverflowError):
            return None
        return session

    @staticmethod
    def _missing():
        return HTMLResponse('<!doctype html><html lang="en"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Capture unavailable</title><body><h1>Capture unavailable</h1><p>This link is invalid, expired or no longer available. Ask Pippa for capture status in WhatsApp.</p></body></html>', status_code=404, headers=PRIVATE_HEADERS)

    async def page(self, session_id: str, token: str = ''):
        session = self._authorized(session_id, token)
        if session is None:
            return self._missing()
        items = self.captures.items(session['user_id'], session_id, include_media=False)
        return HTMLResponse(self._render(session, items, token), headers=PRIVATE_HEADERS)

    async def media(self, session_id: str, item_id: str, token: str = ''):
        session = self._authorized(session_id, token)
        if session is None or not item_id.isascii() or not item_id.isdigit() or len(item_id) > 20:
            return self._missing()
        item = self.captures.item(session['user_id'], int(item_id))
        if item is None or item['session_id'] != session_id or not item.get('media'):
            return self._missing()
        mime = item.get('mime_type', '').split(';', 1)[0].strip().lower()
        allowed = {'image/jpeg', 'image/png'} if item['kind'] == 'image' else {'audio/ogg', 'audio/mpeg', 'audio/mp4', 'audio/aac', 'audio/amr', 'audio/wav', 'audio/webm', 'audio/x-wav'} if item['kind'] == 'audio' else set()
        if mime not in allowed:
            return self._missing()
        return Response(content=item['media'], media_type=mime, headers=PRIVATE_HEADERS)

    def _render(self, session, items, token):
        link_expiry = datetime.fromtimestamp(int(token.split('.', 1)[0]), tz=timezone.utc)
        expiry_label = link_expiry.strftime('%d %b %Y, %H:%M UTC')
        state = session['state']
        labels = {'collecting': 'Collecting notes', 'closing': 'Preparing your notes', 'ready': 'Notes ready', 'failed': 'Summary needs a retry'}
        usable = [item for item in items if item.get('included') and item['status'] == 'ready']
        other = [item for item in items if item not in usable]
        allowed = {item['id'] for item in usable}
        sections = ''
        if state == 'ready' and session.get('summary_json'):
            try:
                summary = CaptureSummary.model_validate(json.loads(session['summary_json']))
                points = [point for group in (summary.takeaways, summary.observations, summary.questions) for point in group]
                if (not points and usable) or any(not point.text.strip() or not set(point.source_ids) <= allowed for point in points):
                    raise ValueError('Invalid summary provenance')
                if not points:
                    sections = '<div class="notice">No included, readable sources to summarize. Original items, if any, remain below.</div>'
                for key, title in [('takeaways', 'Key takeaways'), ('observations', 'Your observations'), ('questions', 'Questions to explore · unresearched')]:
                    group = getattr(summary, key)
                    entries = ''.join('<li>' + _text(point.text) + '<span class="citations">' + ''.join(
                        f'<a href="#item-{source_id}">Source {source_id}</a>' for source_id in dict.fromkeys(point.source_ids)) + '</span></li>' for point in group)
                    sections += f'<section><h2>{title}</h2>' + (f'<ul>{entries}</ul>' if entries else '<p class="muted">None recorded in the submitted material.</p>') + '</section>'
            except (ValueError, TypeError):
                sections = '<div class="notice">The summary is unavailable. Send “retry capture” in WhatsApp.</div>'
        elif state == 'failed':
            sections = '<div class="notice">The summary could not be completed. Your original material is below. Send “retry capture” in WhatsApp.</div>'
        else:
            sections = '<div class="notice">' + ('Send “finish this session” in WhatsApp when you are ready to prepare these notes.' if state == 'collecting' else 'Accepted items are being processed. Refresh this page to check the summary.') + '</div>'
        if any(item['status'] == 'failed' for item in items):
            sections += '<p class="notice">Incomplete input: some items could not be processed and are excluded from the summary. Review them below or send “retry capture”.</p>'
        sources = '<section><h2>Original sources</h2>' + (''.join(self._item(session, item, token) for item in usable) or '<p class="muted">No completed, included sources yet.</p>') + '</section>'
        if other:
            sources += '<section><h2>Pending, review &amp; excluded items</h2><p class="muted">These items are preserved below and are not in the summary.</p>' + ''.join(self._item(session, item, token) for item in other) + '</section>'
        return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{_text(session['title'])} · Pippa notes</title><style>{CSS}</style></head><body><main>
<div class="brand">pippa<span aria-hidden="true"> ✧</span></div><header><p class="eyebrow">Your conference notebook</p><h1>{_text(session['title'])}</h1><div class="meta"><span class="badge">{_text(labels.get(state, state))}</span><span class="muted">{len(items)} submitted items · {_text(session['created_at'][:10])}</span></div><p class="muted">Notes from the material you submitted. This is not a record of the entire talk.</p></header>
{sections}{sources}<footer><p><strong>Private access link.</strong> Anyone with this link can view this session until <time datetime="{_text(link_expiry.isoformat())}">{_text(expiry_label)}</time>, or until it is deleted. This link is not an account login.</p><p>Manage these notes in WhatsApp: “capture status”, “exclude item N”, “include item N”, “retry capture” or “delete capture”.</p></footer></main></body></html>'''

    @staticmethod
    def _item(session, item, token):
        item_id = item['id']
        label = 'Failed' if item['status'] == 'failed' else 'Pending' if item['status'] != 'ready' else 'Needs review' if not item.get('included') and item.get('category') == 'review' else 'Excluded' if not item.get('included') else 'Included'
        kind = {'image': 'Slide photograph', 'audio': 'Voice note', 'text': 'Text note'}.get(item['kind'], 'Source')
        body = ''
        if (item.get('has_media') or item.get('media')) and item['kind'] in {'image', 'audio'}:
            url = f'/captures/{quote(session["id"], safe="")}/items/{item_id}?{urlencode({"token": token})}'
            body += f'<img src="{_text(url)}" alt="Original slide photograph, source {item_id}" loading="lazy">' if item['kind'] == 'image' else f'<audio controls preload="none" src="{_text(url)}">Your browser cannot play this voice note.</audio>'
        if item.get('original_text'):
            body += '<div class="label">Original note / caption</div><p class="source-text">' + _text(item['original_text']) + '</p>'
        if item.get('extracted_text') and (item['kind'] in {'image', 'audio'} or item['extracted_text'] != item.get('original_text')):
            body += '<div class="label">' + ('Voice transcript' if item['kind'] == 'audio' else 'Slide extraction' if item['kind'] == 'image' else 'Captured text') + '</div><p class="source-text">' + _text(item['extracted_text']) + '</p>'
        if item['status'] == 'failed':
            body += '<p class="muted">Processing failed. Send “retry capture” in WhatsApp to try again.</p>'
        return f'<article class="card" id="item-{item_id}"><div class="meta"><h3>Source {item_id} · {kind}</h3><span class="badge">{label}</span></div>{body}</article>'
