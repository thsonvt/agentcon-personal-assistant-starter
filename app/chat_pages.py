"""A minimal, server-rendered chat for host-provided scheduling."""

from app.connection_pages import CSS
from app.group_pages import _form, _hidden, _path, _text, _when


CHAT_CSS = """
.chat-app { max-width:800px }.chat-main { padding:32px; min-width:0 }
.chat-nav { display:flex; flex-wrap:wrap; gap:8px 18px; align-items:center; font-size:13px }.chat-nav a { display:inline-flex; align-items:center; min-height:44px }
.chat-main h1 { font-size:34px }.chat-main .lead { margin-bottom:24px }
.conversation { display:flex; flex-direction:column; gap:16px; padding:0; margin:28px 0; list-style:none }
.bubble { max-width:92%; border:1px solid var(--line); border-radius:15px; padding:14px 17px; min-width:0; background:var(--surface) }
.bubble.user { align-self:flex-end; background:var(--soft) }.bubble.assistant { align-self:flex-start }
.speaker { display:block; color:var(--muted); font-size:12px; margin-bottom:5px }.bubble p,.source-quote { white-space:pre-wrap; overflow-wrap:anywhere; margin:0 }
.chat-main textarea { display:block; width:100%; min-width:0; min-height:120px; resize:vertical; border:1px solid var(--line); border-radius:12px; background:var(--surface); color:var(--ink); padding:14px; font:16px/1.5 system-ui,sans-serif }
.chat-main textarea:focus-visible,.chat-main summary:focus-visible { outline:3px solid var(--clay); outline-offset:4px }
.compose-actions { display:flex; justify-content:flex-end; margin-top:12px }.chat-main .caption { margin-top:9px }.example { margin-top:12px; color:var(--muted); font-size:13px }
.proposal { margin:24px 0; border-color:var(--clay) }.proposal h2 { font:24px/1.25 Georgia,serif; margin-bottom:12px; overflow-wrap:anywhere }
.proposal p { overflow-wrap:anywhere; margin:8px 0 }.proposal time { display:block }.proposal .when { font-weight:600 }.proposal .zone { color:var(--muted); font-size:13px }
.chat-guests { list-style:none; padding:0; margin:16px 0 }.chat-guests li { margin:8px 0; overflow-wrap:anywhere }.chat-guests span { display:block; color:var(--muted); font-size:13px }
.chat-main details { margin:17px 0 }.chat-main summary { cursor:pointer; min-height:44px; display:list-item; padding:10px 0; color:var(--muted); font-size:13px }.source-quote { margin:8px 0 12px; padding-left:12px; border-left:2px solid var(--line); font-size:13px }
.booking-status { border-left:3px solid var(--clay); padding:17px; margin:24px 0; background:var(--soft); border-radius:10px }.booking-status p { margin:8px 0 0 }.booking-status form { margin-top:14px }
.bubble,.proposal,.booking-status { scroll-margin-top:20px }.submit-status { min-height:20px }.chat-main .btn:disabled { cursor:wait; opacity:.7 }
@media(max-width:600px) { .chat-main { padding:24px 20px }.chat-app .top { align-items:flex-start; gap:14px; flex-wrap:wrap }.chat-nav { gap:6px 14px }.chat-main h1 { font-size:30px }.bubble { max-width:96% }.proposal { padding:18px }.proposal .btn { width:100% } }
@media(max-width:380px) { .chat-main { padding:22px 16px }.chat-app .top { padding:18px 16px }.bubble { padding:12px 14px }.chat-nav { font-size:12px } }
"""


CHAT_JS = """
(() => {
  const replies = document.querySelectorAll('.bubble.assistant');
  const latest = document.querySelector('[role="alert"]')
    || document.querySelector('.booking-status')
    || document.querySelector('.proposal') || replies[replies.length - 1];
  if (latest) window.requestAnimationFrame(() => {
    const bounds = latest.getBoundingClientRect();
    if (bounds.top < 0 || bounds.bottom > window.innerHeight) {
      latest.scrollIntoView({block:'start', behavior:'auto'});
    }
  });
  const composer = document.querySelector('#message');
  if (!composer) return;
  const form = composer.form;
  const button = form.querySelector('button[type="submit"]');
  const confirm = document.querySelector('.proposal button[type="submit"]');
  const feedback = document.querySelector('#submit-status');
  let submitting = false;
  form.addEventListener('submit', event => {
    if (submitting) { event.preventDefault(); return; }
    submitting = true;
    button.disabled = true;
    if (confirm) confirm.disabled = true;
    form.setAttribute('aria-busy', 'true');
    feedback.textContent = 'Pippa is reading…';
  });
  window.addEventListener('pageshow', () => {
    submitting = false;
    button.disabled = false;
    if (confirm) confirm.disabled = false;
    form.removeAttribute('aria-busy');
    feedback.textContent = '';
  });
})();
"""


def _chat_form(action, content, data, chat):
    return _form(action, _hidden('revision', chat.get('revision', 0)) + content, data)


def _conversation(messages):
    bubbles = []
    for message in messages:
        if message.get('role') not in {'user', 'assistant'}:
            continue
        role = message['role']
        speaker = 'You' if role == 'user' else 'Pippa'
        bubbles.append(f'<li class="bubble {role}"><span class="speaker">{speaker}</span>'
                       f'<p>{_text(message.get("content"))}</p></li>')
    return '<ol class="conversation" aria-label="Conversation">' + ''.join(bubbles) + '</ol>' if bubbles else ''


def _proposal(proposal, ready):
    zone = proposal.get('timezone') or 'UTC'
    content = ('<section class="panel proposal" aria-label="Meeting details">'
               f'<h2>{_text(proposal.get("title"))}</h2>'
               f'<p>{_text(proposal.get("duration_min"))} minutes</p><p class="when">'
               f'<time datetime="{_text(proposal.get("start"))}">From {_text(_when(proposal.get("start"), zone))}</time>'
               f'<time datetime="{_text(proposal.get("end"))}">To {_text(_when(proposal.get("end"), zone))}</time></p>'
               f'<p class="zone">{_text(zone)}</p>')
    if proposal.get('location'):
        content += f'<p>{_text(proposal["location"])}</p>'
    content += '<ul class="chat-guests" aria-label="Guests">'
    for guest in proposal.get('guests') or []:
        content += f'<li>{_text(guest.get("name"))}<span>{_text(guest.get("email"))}</span></li>'
    content += '</ul>'
    if proposal.get('sources'):
        content += '<details><summary>Availability you provided</summary>'
        for source in proposal['sources']:
            content += f'<blockquote class="source-quote">{_text(source)}</blockquote>'
        content += '</details>'
    if ready:
        content += ('<p class="caption">Check the time, names and emails. You are confirming the availability '
                    'you provided on your guests’ behalf.</p>')
    return content


def render_chat_page(data):
    """Render escaped conversation content; only server state enables booking."""
    chat = data.get('chat') or {}
    status = chat.get('status', 'collecting')
    base = '/connections/chat/' + _path(chat.get('id')) if chat else '/connections/chat'
    content = '<h1>Let’s find a time.</h1>'
    if not chat.get('messages'):
        content += ('<p class="lead">Tell Pippa who you’re meeting and paste the availability for two guests. '
                    'Pippa will check your calendar and ask for anything missing.</p>')
    if data.get('error'):
        content += f'<p class="message error" role="alert">{_text(data["error"])}</p>'
    if data.get('notice'):
        content += f'<p class="message" role="status">{_text(data["notice"])}</p>'
    content += _conversation(chat.get('messages') or [])
    if status == 'pending':
        content += ('<section class="booking-status" role="status"><h2>Checking your booking</h2>'
                    '<p>The calendar result is not yet confirmed. Check this booking before starting another request.</p>'
                    + _chat_form(base + '/check', '<button class="btn" type="submit">Check booking status</button>', data, chat)
                    + '</section>')
    elif status == 'booked':
        content += ('<section class="booking-status" role="status"><h2>Event created · invitations requested</h2>'
                    '<p>Delivery and acceptance are not yet confirmed.</p></section>')
    elif status not in {'collecting', 'ready'}:
        content += '<p class="message" role="status">Status unavailable. Refresh this page to check your request.</p>'
    proposal = chat.get('proposal')
    if proposal and status in {'ready', 'pending', 'booked'}:
        content += _proposal(proposal, status == 'ready')
        if status == 'ready':
            content += ('<div class="actions">' + _chat_form(base + '/confirm',
                        '<button class="btn primary" type="submit">Confirm and send invites</button>', data, chat) + '</div>')
        content += '</section>'
    if status in {'collecting', 'ready'}:
        composer = ('<label for="message">Message</label><textarea id="message" name="message" rows="4" '
                    'required aria-describedby="message-help"></textarea><div class="compose-actions">'
                    '<button class="btn primary" type="submit">Submit</button></div>')
        content += _chat_form(base + '/message' if chat else base, composer, data, chat)
        content += '<p class="caption submit-status" id="submit-status" role="status" aria-live="polite"></p>'
        content += ('<p class="caption" id="message-help">'
                    + ('A new message replaces this proposal. ' if status == 'ready' else '')
                    + 'Invitations are requested only when you select “Confirm and send invites”.</p>')
        if not chat.get('messages'):
            content += ('<p class="example">Example: “A 30-minute catch-up with Ana (ana@example.com) and '
                        'Ben (ben@example.com).” Then paste their dates, times and timezones.</p>')
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta name="robots" content="noindex,nofollow"><meta name="referrer" content="same-origin">'
            f'<title>Chat scheduling · Pippa</title><style>{CSS}{CHAT_CSS}</style></head><body>'
            '<a class="skip" href="#main">Skip to content</a><div class="app chat-app"><header class="top">'
            '<a class="brand" href="/" aria-label="Pippa home"><span class="brand-dot" aria-hidden="true"></span>Pippa</a>'
            '<nav class="chat-nav" aria-label="Scheduling"><a href="/connections/chat">New chat</a>'
            '<a href="/connections">Connections</a><a href="/connections/meetings">Detailed group form</a></nav></header>'
            '<main class="chat-main" id="main">' + content + '</main><footer class="footer">'
            '<span>A little less back-and-forth.</span><a href="/privacy">Privacy</a></footer></div>'
            '<script>' + CHAT_JS + '</script></body></html>')
