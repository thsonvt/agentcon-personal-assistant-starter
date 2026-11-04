"""Accessible private scheduling pages for a host and two required guests."""
from datetime import datetime
from html import escape
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.connection_pages import CSS


GROUP_CSS = """
.group-app { max-width:1000px }.group-main { padding:36px; min-width:0 }.group-main>h1 { max-width:100% }
.group-grid { display:grid; grid-template-columns:minmax(0,1fr) minmax(0,1fr); gap:20px }.group-grid>* { min-width:0 }.wide { grid-column:1/-1 }
.group-main input,.group-main select,.group-main textarea { width:100%; min-width:0; font:inherit; font-size:16px; color:var(--ink); background:var(--surface); border:1px solid var(--line); border-radius:9px; padding:11px 12px }
.group-main select:focus-visible,.group-main textarea:focus-visible,summary:focus-visible { outline:3px solid var(--clay); outline-offset:3px }
fieldset { min-width:0; padding:0; border:0; margin:0 } legend { font-weight:600; padding:0; margin:0 0 8px }
.guest-box { border:1px solid var(--line); border-radius:12px; padding:18px; background:var(--surface) }.guest-box label:first-of-type { margin-top:10px }
.group-main .panel { margin:22px 0 }.group-main .panel:first-child { margin-top:0 }.facts-list { display:flex; flex-wrap:wrap; gap:8px 20px; color:var(--muted); list-style:none; margin:0 0 18px; padding:0 }
.option-list { list-style:none; padding:0; margin:16px 0; display:grid; gap:10px }.option-row { background:var(--surface); border:1px solid var(--line); border-radius:10px; padding:13px 15px; display:flex; align-items:center; gap:12px; margin:0; font-weight:400; min-height:58px; overflow-wrap:anywhere }
.option-row:has(input:checked) { border-color:var(--clay); background:var(--soft) }.option-row input { width:19px; height:19px; min-width:19px; margin:0; accent-color:var(--clay); padding:0 }.option-row span { min-width:0 }.option-row small { display:block; color:var(--muted); font-size:12px }
.group-intro { max-width:650px }.status-banner { background:var(--soft); border-left:3px solid var(--clay); padding:19px 21px; border-radius:12px; margin:25px 0 }.status-banner p { margin:7px 0 0 }.status-banner h2 { font:24px Georgia,serif }
.meeting-list { list-style:none; padding:0; margin:0 }.meeting-list li { border-top:1px solid var(--line); padding:16px 0; display:flex; align-items:center; justify-content:space-between; gap:16px }.meeting-list li:first-child { border-top:0 }.meeting-list a { font-weight:600 }.meeting-list .pill { white-space:nowrap }
.share { margin-top:20px; padding-top:18px; border-top:1px solid var(--line) }.share input { font-size:13px }.share textarea { resize:vertical; min-height:135px; font-size:13px; line-height:1.6 }.share .btn { margin-top:10px }.guest-heading { display:flex; justify-content:space-between; align-items:flex-start; gap:12px; flex-wrap:wrap }.guest-heading h3 { font-size:17px }.guest-heading p { margin:3px 0 0 }
.group-main details { margin-top:24px; border-top:1px solid var(--line); padding-top:20px }.group-main summary { cursor:pointer; font-weight:600; min-height:32px }.group-main details form { margin-top:15px }.group-main .actions form { display:inline-flex }.group-main .actions { align-items:flex-start }.group-main .details-line { margin:0 0 8px; overflow-wrap:anywhere }
.group-main h1,.group-main h2,.group-main h3,.group-main p { overflow-wrap:anywhere }.guest-main { max-width:720px; margin:auto }.small-note { color:var(--muted); font-size:12px; margin:16px 0 }.group-main .danger { color:var(--bad) }.group-main .copy-status { min-height:20px; margin:8px 0 0 }
@media(max-width:650px) { .group-main { padding:27px 22px }.group-grid { grid-template-columns:1fr; gap:16px }.wide { grid-column:auto }.group-main h1 { font-size:30px }.group-app .top .meta { max-width:150px }.meeting-list li { align-items:flex-start }.status-banner { padding:17px }.group-main .actions>.btn { width:100% } }
@media(max-width:380px) { .group-main { padding:24px 16px }.guest-box { padding:15px }.group-app .top { padding:20px 16px }.group-main .actions form,.group-main .actions form .btn { width:100% } }
"""

STATES = {
    'draft': ('Review your request', 'Check the people, duration and options below. No invitations are sent. Activate when everything looks right.'),
    'waiting': ('Waiting for replies', 'Share each private link with its guest. Pippa will book one time only when both guests have accepted it.'),
    'no_common': ('No shared time yet', 'The replies do not overlap. Ask for another option or prepare a new window.'),
    'needs_help': ('A little help is needed', 'The available options have changed. Check the latest status or prepare a new window.'),
    'pending': ('Checking the booking', 'The calendar result is not yet confirmed. Please do not create another request while Pippa checks this booking.'),
    'booked': ('Meeting booked', 'The meeting has been added to the host’s calendar and invitations have been requested. Delivery and acceptance are not yet confirmed.'),
    'cancelled': ('Request cancelled', 'This scheduling request is closed. These options can no longer be selected.'),
    'expired': ('Request expired', 'These private links have expired. Ask your host to prepare a new window.'),
}
ACTIVE = {'waiting', 'no_common', 'needs_help'}
EDITABLE = {'draft', *ACTIVE, 'expired'}
ZONES = ('Asia/Singapore', 'Asia/Riyadh', 'Asia/Ho_Chi_Minh', 'Asia/Tokyo', 'Asia/Kolkata',
         'Europe/London', 'Europe/Paris', 'America/New_York', 'America/Los_Angeles', 'Australia/Sydney', 'UTC')


def _text(value):
    return escape(str(value if value is not None else ''), quote=True)


def _path(value):
    return quote(str(value or ''), safe='')


def _zone(value):
    try:
        return ZoneInfo(str(value))
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return ZoneInfo('UTC')


def _date(value, timezone):
    try:
        date = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if date.tzinfo is None:
            date = date.replace(tzinfo=_zone(timezone))
        return date.astimezone(_zone(timezone))
    except (ValueError, TypeError):
        return None


def _when(value, timezone):
    date = _date(value, timezone)
    return date.strftime('%a, %d %b %Y · ') + _clock(date) if date else str(value or '')


def _clock(date):
    offset = date.strftime('%z')
    return date.strftime('%I:%M %p').lstrip('0') + f' UTC{offset[:3]}:{offset[3:]}'


def _local(value, timezone):
    date = _date(value, timezone)
    return date.strftime('%Y-%m-%dT%H:%M') if date else ''


def _safe_calendar_url(value):
    try:
        url = urlsplit(str(value or ''))
        if url.scheme == 'https' and url.hostname in {'calendar.google.com', 'www.google.com'} and not url.username:
            return str(value)
    except ValueError:
        pass
    return ''


def _private_link(value, base_url):
    """Only make same-origin, opaque participant links clickable."""
    try:
        url = urlsplit(str(value or ''))
        base = urlsplit(str(base_url or ''))
        if url.scheme or url.netloc:
            if url.scheme not in {'http', 'https'} or url.netloc != base.netloc or url.scheme != base.scheme:
                return ''
        if not url.path.startswith('/m/') or url.query or url.fragment or url.username:
            return ''
        token = url.path.removeprefix('/m/')
        if not token or '/' in token or '\\' in token:
            return ''
        return url.path
    except ValueError:
        return ''


def _hidden(name, value):
    return f'<input type="hidden" name="{name}" value="{_text(value)}">'


def _form(action, content, data):
    return f'<form method="post" action="{_text(action)}">' + _hidden('csrf_token', data.get('csrf_token')) + content + '</form>'


def _timezone(value, name='timezone', identity='timezone'):
    zones = list(dict.fromkeys([str(value or 'Asia/Singapore'), *ZONES]))
    options = ''.join(f'<option value="{_text(zone)}"{" selected" if zone == zones[0] else ""}>{_text(zone.replace("_", " "))}</option>' for zone in zones)
    return f'<label for="{identity}">Timezone</label><select id="{identity}" name="{name}" data-timezone>{options}</select>'


def _input(name, label, value='', kind='text', required=True, maximum=None):
    attrs = (' required' if required else '') + (f' maxlength="{maximum}"' if maximum else '')
    return f'<label for="{name}">{label}</label><input id="{name}" name="{name}" type="{kind}" value="{_text(value)}"{attrs}>'


def _window_fields(values):
    timezone = values.get('timezone') or 'Asia/Singapore'
    return ('<div class="wide">' + _timezone(timezone) + '<p class="caption">Enter both dates and times in this timezone.</p></div>'
            + '<div>' + _input('window_start', 'Window starts', _local(values.get('window_start'), timezone), 'datetime-local') + '</div>'
            + '<div>' + _input('window_end', 'Window ends', _local(values.get('window_end'), timezone), 'datetime-local') + '</div>')


def _list(data):
    meetings = data.get('meetings') or []
    items = ''
    for meeting in meetings:
        status = STATES.get(meeting.get('status'), ('Check status', ''))[0]
        items += (f'<li><div><a href="/connections/meetings/{_path(meeting.get("id"))}">{_text(meeting.get("title"))}</a>'
                  f'<p class="caption">{_text(meeting.get("duration_min"))} minutes · {_text(meeting.get("timezone"))}</p></div><span class="pill">{_text(status)}</span></li>')
    existing = '<section class="panel"><h2>Your requests</h2><ul class="meeting-list">' + items + '</ul></section>' if items else ''
    values = data.get('values') or data.get('form') or {}
    duration = str(values.get('duration_min') or '60')
    fields = '<div class="group-grid"><div>' + _input('title', 'Meeting title', values.get('title', ''), maximum=120) + '</div>'
    fields += ('<div><label for="duration_min">Duration</label><select id="duration_min" name="duration_min">'
               + ''.join(f'<option value="{minutes}"{" selected" if duration == str(minutes) else ""}>{minutes} minutes</option>' for minutes in (30, 60)) + '</select></div>')
    fields += _window_fields(values)
    fields += '<p class="caption wide">Choose one window within the next 30 days, at least 2 hours from now. Pippa will suggest up to 6 times from your calendar.</p>'
    for index in (1, 2):
        fields += f'<fieldset class="guest-box"><legend>Guest {index}</legend>'
        fields += _input(f'guest{index}_name', 'Name', values.get(f'guest{index}_name', ''), maximum=80)
        fields += _input(f'guest{index}_email', 'Email for the invitation', values.get(f'guest{index}_email', ''), 'email', maximum=254) + '</fieldset>'
    fields += '<div class="wide">' + _input('location', 'Location or meeting link (optional)', values.get('location', ''), required=False, maximum=500) + '</div></div>'
    fields += '<div class="actions"><button class="btn primary" type="submit">Review available times →</button></div><p class="small-note">No invitations are sent. You’ll review the options before activating your request.</p>'
    return ('<p class="kicker">Make time together</p><h1>One meeting. Three people.</h1><p class="lead group-intro">Find a time for you and two guests. Start with a window, review the options, then share a private link with each person.</p>'
            + existing + '<section class="panel"><h2>Prepare a request</h2>' + _form('/connections/meetings', fields, data) + '</section>')


def _details(meeting, timezone):
    return (f'<ul class="facts-list"><li>{_text(meeting.get("duration_min"))} minutes</li><li data-zone-label>{_text(timezone)}</li>'
            f'<li>{_text(meeting.get("location") or "Location arranged with your host")}</li></ul>')


def _banner(meeting, host):
    status = meeting.get('status')
    heading, message = STATES.get(status, ('Status unavailable', 'Please refresh this page or contact your host.'))
    if not host and status in ACTIVE:
        heading, message = ('Find a time together', 'Choose every option you can attend. No account needed. Pippa will compare your reply with the other guest’s availability.')
    if not host and status == 'draft':
        heading, message = ('Not ready yet', 'Your host is still preparing this request. Please wait for your private invitation link.')
    return f'<section class="status-banner" role="status"><h2>{heading}</h2><p>{message}</p></section>'


def _options(meeting, timezone, selected=(), selectable=False):
    rows = ''
    for option in meeting.get('options') or []:
        label = _when(option.get('start'), timezone)
        end = _date(option.get('end'), timezone)
        end_label = _clock(end) if end else str(option.get('end') or '')
        content = f'<span data-slot-start="{_text(option.get("start"))}" data-slot-end="{_text(option.get("end"))}">{_text(label)} – {_text(end_label)}</span>'
        if selectable:
            content = (f'<label class="option-row"><input type="checkbox" name="selected" value="{_text(option.get("id"))}"'
                       + (' checked' if str(option.get('id')) in {str(item) for item in selected} else '') + '>' + content + '</label>')
        else:
            content = '<div class="option-row">' + content + '</div>'
        rows += '<li>' + content + '</li>'
    return '<ul class="option-list">' + rows + '</ul>' if rows else '<p class="message">No available options in this window. Try a new window.</p>'


def _host(data):
    meeting = data.get('meeting') or {}
    timezone = meeting.get('timezone') or 'Asia/Singapore'
    status = meeting.get('status')
    action = '/connections/meetings/' + _path(meeting.get('id'))
    content = ('<a href="/connections/meetings">← All requests</a><p class="kicker spaced">Your scheduling request</p>'
               + f'<h1>{_text(meeting.get("title"))}</h1>' + _details(meeting, timezone) + _banner(meeting, True))
    if status == 'booked':
        content += _booked(meeting, timezone)
    elif status == 'pending':
        content += _form(action + '/check', '<button class="btn primary" type="submit">Check booking status</button>', data)
    else:
        content += '<h2>Proposed times</h2>' + _options(meeting, timezone)
    content += '<section class="panel"><h2>The people</h2><p class="caption">You are the host. Both guests must accept a shared time.</p><div class="group-grid spaced">'
    for index, guest in enumerate(meeting.get('guests') or []):
        reply_status = {'awaiting': 'Waiting for reply', 'replied': 'Answered', 'none': 'No times work'}.get(guest.get('status'), 'Waiting for reply')
        content += (f'<article class="guest-box"><div class="guest-heading"><h3>{_text(guest.get("name"))}</h3><span class="pill">{reply_status}</span></div>'
                    f'<p class="caption">{_text(guest.get("email"))}</p>')
        if status in ACTIVE:
            safe_link = _private_link(guest.get('link'), data.get('base_url'))
            if safe_link:
                content += (f'<div class="share"><h3>Share privately</h3><p class="caption">This link is only for {_text(guest.get("name"))}. It has not been sent.</p>'
                            f'<label for="share-link-{index}">Private link</label><input id="share-link-{index}" readonly value="{_text(guest.get("link"))}">'
                            f'<button class="btn" type="button" data-copy="share-link-{index}">Copy link</button>'
                            f'<label for="share-message-{index}">Prepared message · send it yourself</label><textarea id="share-message-{index}" readonly>{_text(guest.get("message"))}</textarea>'
                            f'<button class="btn" type="button" data-copy="share-message-{index}">Copy message</button></div>')
        content += '</article>'
    content += '</div><p class="caption copy-status" id="copy-status" role="status" aria-live="polite"></p></section>'
    if status == 'draft' and meeting.get('options'):
        consent = ('<p>By activating, you allow Pippa to book one time accepted by both guests, after checking your calendar again. '
                   'You’ll receive private links to send. Pippa will not email or message anyone for you.</p>'
                   '<button class="btn primary" type="submit">Activate request →</button>')
        content += '<section class="panel"><h2>Ready to share?</h2>' + _form(action + '/activate', consent, data) + '</section>'
    if status in ACTIVE:
        content += '<p class="small-note">Next action: share any unsent links, or follow up with guests still waiting to reply. You can copy the prepared message as a reminder. No reminders are sent automatically.</p>'
        if meeting.get('needs_reminder'):
            content += '<p class="message">This request has been waiting for more than 48 hours. Follow up with anyone who has not replied.</p>'
        content += _form(action + '/check', '<button class="btn" type="submit">Check latest status</button>', data)
    if status in EDITABLE:
        revise = ('<p class="caption">A new window clears earlier replies and replaces both private links. Review the new options before sharing again.</p><div class="group-grid">'
                  + _window_fields({**meeting, **(data.get('values') or {})}) + '</div><div class="actions"><button class="btn" type="submit">Prepare new window</button></div>')
        content += '<details' + (' open' if data.get('error') and data.get('values', {}).get('window_start') else '') + '><summary>Change the time window</summary>' + _form(action + '/revise', revise, data) + '</details>'
        content += '<div class="actions">' + _form(action + '/cancel', '<button class="btn danger" type="submit">Cancel this request</button>', data) + '</div>'
    if meeting.get('expires_at') and status not in {'booked', 'cancelled', 'expired'}:
        content += f'<p class="small-note">Links expire {_text(_when(meeting.get("expires_at"), timezone))} · {_text(timezone)}.</p>'
    return content


def _booked(meeting, timezone):
    content = f'<p class="message"><strong>{_text(_when(meeting.get("selected_start"), timezone))}</strong> · {_text(timezone)}</p>' if meeting.get('selected_start') else ''
    link = _safe_calendar_url(meeting.get('event_url'))
    if link:
        content += f'<a class="btn" href="{_text(link)}" rel="noreferrer">Open Google Calendar</a>'
    return content + '<p class="small-note">For changes or cancellation, contact the host. The host can manage this event in Google Calendar.</p>'


def _guest(data):
    meeting = data.get('meeting') or {}
    timezone = data.get('timezone') or meeting.get('timezone') or 'UTC'
    status = meeting.get('status')
    content = (f'<p class="kicker">A little less back-and-forth</p><h1>{_text(meeting.get("title"))}</h1>'
               + f'<p class="lead">Hi {_text(data.get("guest_name") or "there")}. {_text(meeting.get("host_name") or "Your host")} is arranging this meeting.</p>'
               + _details(meeting, timezone) + _banner(meeting, False))
    if status == 'booked':
        return content + _booked(meeting, timezone)
    if status not in ACTIVE:
        return content + '<p class="small-note">Contact your host if you need help with this request.</p>'
    if data.get('submitted') or data.get('replied'):
        content += '<p class="message" role="status">Your availability is saved. This is not yet a confirmed booking.</p>'
    fields = (_hidden('revision', meeting.get('revision')) + _hidden('response', 'submit')
              + _timezone(timezone, identity='guest-timezone') + '<p class="caption">All options are shown in this timezone. Changing it keeps the same meeting times.</p>'
              + '<fieldset class="spaced"><legend>Select every time that works</legend>'
              + _options(meeting, timezone, data.get('selected') or [], True) + '</fieldset>')
    fields += ('<p class="small-note" id="consent-note">Submitting your choices gives Pippa permission to book one of these times when both guests agree and the host is still free. '
               'You’ll be included in the calendar invitation with the other guest.</p><div class="actions">'
               '<button class="btn primary" type="submit" aria-describedby="consent-note">Save availability →</button>'
               '<button class="btn" type="submit" name="none" value="1">None of these times work</button></div>'
               '<p class="caption spaced">You can update your reply here until the meeting is booked or the request closes.</p>')
    content += _form('/m/' + _path(data.get('token')), fields, data)
    if meeting.get('expires_at'):
        content += (f'<p class="small-note">Please reply before <span data-instant="{_text(meeting.get("expires_at"))}">{_text(_when(meeting.get("expires_at"), timezone))}</span>'
                    f' · <span data-zone-label>{_text(timezone)}</span>. Keep your private link to yourself.</p>')
    return content


PAGE_JS = r"""
(() => {
  document.querySelectorAll('[data-timezone]').forEach(select => {
    const current = select.value;
    let zones = [];
    try { if (Intl.supportedValuesOf) zones = Intl.supportedValuesOf('timeZone'); } catch (_) {}
    const existing = new Set([...select.options].map(option => option.value));
    zones.forEach(zone => {
      if (existing.has(zone)) return;
      const option = document.createElement('option'); option.value = zone; option.textContent = zone.replaceAll('_', ' '); select.append(option);
    });
    select.value = current;
    if (select.id !== 'guest-timezone') return;
    select.addEventListener('change', () => {
      try {
        const full = new Intl.DateTimeFormat('en', {weekday:'short', day:'numeric', month:'short', year:'numeric', hour:'numeric', minute:'2-digit', timeZone:select.value, timeZoneName:'shortOffset'});
        const time = new Intl.DateTimeFormat('en', {hour:'numeric', minute:'2-digit', timeZone:select.value, timeZoneName:'shortOffset'});
        document.querySelectorAll('[data-slot-start]').forEach(slot => {
          slot.textContent = full.format(new Date(slot.dataset.slotStart)) + ' – ' + time.format(new Date(slot.dataset.slotEnd));
        });
        document.querySelectorAll('[data-zone-label]').forEach(label => { label.textContent = select.value; });
        document.querySelectorAll('[data-instant]').forEach(label => { label.textContent = full.format(new Date(label.dataset.instant)); });
      } catch (_) {}
    });
  });
  document.querySelectorAll('[data-copy]').forEach(button => button.addEventListener('click', async () => {
    const source = document.getElementById(button.dataset.copy);
    const status = document.getElementById('copy-status');
    try {
      await navigator.clipboard.writeText(source.value);
      status.textContent = 'Copied. Paste it into your own message to this guest.';
    } catch (_) {
      source.focus(); source.select(); status.textContent = 'Text selected. Copy it, then paste it into your message.';
    }
  }));
})();
"""


def render_group_page(view, data):
    """Render only fields appropriate to this audience; never serialize payloads."""
    if view not in {'list', 'host', 'guest', 'error'}:
        raise ValueError('Unknown group page view')
    title = {'list': 'Meet together', 'host': 'Scheduling request', 'guest': 'Choose your availability', 'error': 'Request unavailable'}[view]
    content = ''
    if data.get('error'):
        content += f'<p class="message error" role="alert">{_text(data["error"])}</p>'
    if data.get('notice'):
        content += f'<p class="message" role="status">{_text(data["notice"])}</p>'
    if view == 'error':
        content += '<p class="kicker">Let’s reconnect</p><h1>This request is unavailable.</h1><p class="lead">Your link may have expired or been replaced. Please ask your host for the latest private link.</p>'
    else:
        content += {'list': _list, 'host': _host, 'guest': _guest}[view](data)
    nav = '<a href="/connections">← Connections</a>' if view in {'list', 'host'} else '<span class="meta">Made simple with Pippa</span>'
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta name="robots" content="noindex,nofollow"><meta name="referrer" content="same-origin">'
            f'<title>{title} · Pippa</title><style>{CSS}{GROUP_CSS}</style></head><body><a class="skip" href="#main">Skip to content</a>'
            '<div class="app group-app"><header class="top"><a href="/" class="brand" aria-label="Pippa home"><span class="brand-dot" aria-hidden="true"></span>Pippa</a>'
            + nav + '</header><main id="main" class="group-main' + (' guest-main' if view in {'guest', 'error'} else '') + '">'
            + content + '</main><footer class="footer"><span>A little less back-and-forth.</span><a href="/privacy">Privacy</a></footer></div>'
            '<script>' + PAGE_JS + '</script></body></html>')
