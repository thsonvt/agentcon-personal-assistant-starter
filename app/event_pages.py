"""Small, escaped, server-rendered attendee pages. No third-party assets or scripts."""
from datetime import datetime
from html import escape
from urllib.parse import quote
from zoneinfo import ZoneInfo

from app.connection_pages import CSS

PURPOSES = {'coffee': 'Coffee & conversation', 'technical': 'Technical chat',
            'collaboration': 'Build together', 'work': 'Work opportunities'}
EVENT_CSS = '''
.sr-only{position:absolute;width:1px;height:1px;padding:0;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap}.event-app{max-width:1020px}.event-nav{display:flex;gap:6px;flex-wrap:wrap;padding:12px 30px;border-bottom:1px solid var(--line)}
.event-nav a{padding:8px 12px;color:var(--muted);text-decoration:none;border-radius:9px}.event-nav a:hover{background:var(--soft)}
.event-main{padding:38px;min-width:0}.hero{display:grid;grid-template-columns:minmax(0,1fr) 170px;gap:30px;align-items:center}.hero h1{font-size:43px;max-width:650px}.hero .lead{font-size:16px}
.event-main h1,.event-main h2,.event-main h3,.event-main p{overflow-wrap:anywhere}.eyebrow{letter-spacing:.1em;text-transform:uppercase;font-size:11px;font-weight:600;color:var(--clay);margin-bottom:12px}
.event-stamp{aspect-ratio:1;border-radius:50%;background:var(--soft);border:1px solid var(--line);display:grid;place-content:center;text-align:center;color:var(--clay);font:italic 27px/1.3 Georgia,serif;transform:rotate(8deg)}
.event-stamp small{font:10px system-ui;text-transform:uppercase;letter-spacing:.12em;margin-top:8px}.event-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;margin:24px 0}.person-card{padding:22px;border:1px solid var(--line);border-radius:16px;background:var(--surface);min-width:0}.person-card h2{font:25px Georgia,serif;margin:12px 0 8px}.person-card h2 a{color:var(--ink);text-decoration:none}.person-card p{margin:8px 0 18px}.avatar{display:grid;place-items:center;border-radius:50%;width:46px;height:46px;background:var(--soft);color:var(--clay);font:22px Georgia,serif}.tags{display:flex;flex-wrap:wrap;gap:6px}.tag{font-size:11px;border:1px solid var(--line);border-radius:20px;padding:4px 9px;color:var(--muted)}
.event-main textarea,.event-main select{width:100%;min-width:0;max-width:100%;font:16px/1.5 system-ui;color:var(--ink);background:var(--bg);border:1px solid var(--line);border-radius:9px;padding:11px 12px}.event-main textarea{resize:vertical}.event-main select:focus-visible,.event-main textarea:focus-visible{outline:3px solid var(--clay);outline-offset:3px}.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:0 20px}.form-grid .wide{grid-column:1/-1}.check{display:flex;gap:10px;align-items:flex-start;font-weight:400}.check input{width:18px;flex:0 0 18px;height:18px;margin:3px 0 0;padding:0;accent-color:var(--clay)}.check strong,.check small{display:block}.check small{color:var(--muted);margin-top:3px}.search{display:flex;gap:9px;margin-top:24px}.search input{flex:1}.search label{margin:0;flex:1}.empty{padding:30px;border:1px dashed var(--line);border-radius:14px;text-align:center;margin-top:24px}.empty h2{font:24px Georgia,serif;margin-bottom:8px}.empty p{margin:0;color:var(--muted)}
.profile-head{display:flex;gap:18px;align-items:center;margin:22px 0}.profile-head .avatar{width:72px;height:72px;font-size:30px;flex:0 0 72px}.profile-head h1{margin:0}.profile-head p{margin:7px 0 0;color:var(--muted)}.profile-layout{display:grid;grid-template-columns:minmax(0,1fr) 270px;gap:24px}.share-card{padding:20px;border:1px solid var(--line);border-radius:16px;background:var(--surface);align-self:start;text-align:center}.share-card img{display:block;width:180px;height:180px;max-width:100%;margin:16px auto;background:white}.share-card input{font-size:12px}.share-card p{margin-top:12px}.request-list{list-style:none;padding:0;margin:24px 0}.request-list li{border-top:1px solid var(--line);padding:18px 0;display:flex;justify-content:space-between;gap:15px}.request-list a{font-weight:600}.request-list p{margin:3px 0 0;color:var(--muted)}.request-note{white-space:pre-wrap;overflow-wrap:anywhere}.inline-actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:20px}.status-banner{padding:22px;border-left:3px solid var(--clay);background:var(--soft);border-radius:12px;margin:24px 0}.status-banner h2{font:27px Georgia,serif;margin-bottom:7px}.status-banner p{margin:0}.slot{display:flex;gap:12px;align-items:center;border:1px solid var(--line);border-radius:10px;padding:12px;margin-top:10px}.slot input{width:20px;flex:0 0 20px}.slot small{display:block;color:var(--muted)}.step-cards{counter-reset:steps}.step-cards .person-card:before{counter-increment:steps;content:'0' counter(steps);font:24px Georgia,serif;color:var(--clay)}.muted{color:var(--muted)}.event-main fieldset{border:0;margin:0;padding:0;min-width:0}.event-main legend{font-weight:600;margin:18px 0 3px}.event-main .fine{font-size:12px;color:var(--muted)}
@media(max-width:700px){.event-main{padding:28px 22px}.event-nav{padding:10px 14px}.hero{grid-template-columns:1fr;gap:10px}.hero h1{font-size:35px}.event-stamp{display:none}.event-grid,.form-grid,.profile-layout{grid-template-columns:1fr}.form-grid .wide{grid-column:auto}.share-card{max-width:100%}.profile-head{align-items:flex-start}.profile-head h1{font-size:31px}.profile-head .avatar{width:54px;height:54px;flex-basis:54px}.request-list li{flex-direction:column;gap:8px}}
@media(max-width:380px){.event-main{padding:25px 16px}.hero h1{font-size:32px}.search{flex-wrap:wrap}.search .btn{width:100%}.event-nav a{padding:7px 9px}.person-card{padding:18px}}
'''


def text(value):
    return escape(str(value if value is not None else ''), quote=True)


def path(value):
    return quote(str(value), safe='')


def hidden(name, value):
    return f'<input type="hidden" name="{text(name)}" value="{text(value)}">'


def form(action, content, data):
    return f'<form method="post" action="{text(action)}">' + hidden('csrf_token', data.get('csrf_token')) + content + '</form>'


def link(label, href, primary=False):
    return f'<a class="btn{" primary" if primary else ""}" href="{text(href)}">{text(label)}</a>'


def button(label, primary=True):
    return f'<button class="btn{" primary" if primary else ""}" type="submit">{text(label)}</button>'


def when(value, zone='Asia/Singapore'):
    try:
        date = datetime.fromisoformat(str(value).replace('Z', '+00:00')).astimezone(ZoneInfo(zone))
        return text(date.strftime('%a, %d %b · %I:%M %p').replace('· 0', '· ')) + ' ' + text(zone)
    except (ValueError, TypeError, KeyError):
        return text(value)


def tags(intents):
    return '<div class="tags">' + ''.join(f'<span class="tag">{text(PURPOSES.get(i, i))}</span>' for i in intents) + '</div>'


def avatar(name):
    initials = ''.join(word[0] for word in str(name).split()[:2]) or 'P'
    return f'<span class="avatar" aria-hidden="true">{text(initials)}</span>'


def render_event_page(view, data):
    event = data.get('event') or {}
    slug = path(event.get('slug', ''))
    nav = '<a href="/network/signin">Events</a>'
    if data.get('signed_in'):
        nav += f'<a href="/network/inbox">My inbox</a>'
    if slug and data.get('member'):
        nav += f'<a href="/events/{slug}/directory">People</a><a href="/events/{slug}/profile">My profile</a>'
    if slug:
        nav += f'<a href="/events/{slug}">Event</a>'
    content = globals()['_' + view](data)
    error = f'<div class="message error" role="alert">{text(data["error"])}</div>' if data.get('error') else ''
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta name="robots" content="noindex,nofollow"><title>' + text(data.get('title') or event.get('title') or 'Meet with Pippa') + ' · Pippa</title>'
            f'<style>{CSS}{EVENT_CSS}</style></head><body><a class="skip" href="#main">Skip to content</a>'
            '<div class="app event-app"><header class="top"><a class="brand" href="/"><span class="brand-dot"></span>pippa</a><span class="meta">Good conversations.<br>A little more follow-through.</span></header>'
            f'<nav class="event-nav" aria-label="Event navigation">{nav}</nav><main class="event-main" id="main">{error}{content}</main>'
            '<footer class="footer"><span>Meet someone. Make something happen.</span><a href="/network/privacy">Event privacy &amp; data</a></footer></div></body></html>')


def _error(data):
    return '<h1>This page is unavailable</h1><p class="lead">The link may be private, or event access may have ended.</p>'


def _landing(data):
    event = data['event']; slug = path(event['slug'])
    if data.get('member'):
        action = link('Meet the people here', f'/events/{slug}/directory', True) + link('Edit my profile', f'/events/{slug}/profile')
    else:
        action = link('Claim my attendee profile', f'/events/{slug}/claim', True) if data.get('signed_in') else link('Continue with Google', f'/signin/google?event={slug}', True)
    return f'''<section class="hero"><div><p class="eyebrow">Your event, beyond the room</p><h1>{text(event['title'])}</h1>
    <p class="lead">Find your next good conversation. Meet the people building, exploring and looking for collaborators around you.</p>
    <p class="fine">{when(event.get('starts_at'))}</p><div class="actions">{action}</div></div><div class="event-stamp" aria-hidden="true">Hello,<br>possibility.<small>Meet with Pippa</small></div></section>
    <div class="event-grid step-cards"><div class="person-card"><h2>Make yourself known</h2><p>Claim your checked-in attendee profile. Choose what to share and who can see it.</p></div><div class="person-card"><h2>Keep the conversation going</h2><p>Ask for a coffee, a technical chat or a chance to build together. Agree a time when you are both ready.</p></div></div>
    <p class="fine">Google verifies your identity. Calendar permission is not needed. Profiles start hidden; joining the attendee directory is your choice.</p>'''


def _claim(data):
    slug = path(data['event']['slug'])
    if not data.get('signed_in'):
        action = link('Continue with Google', f'/signin/google?event={slug}', True)
    else:
        action = form(f'/events/{slug}/claim', button('Claim my profile'), data)
    return f'''<p class="eyebrow">{text(data['event']['title'])}</p><h1>A familiar face, a new connection.</h1><p class="lead">Use the Google account on your registration. You can claim your profile once the organizer has checked you in.</p>
    <div class="panel"><h2>Start with just your identity</h2><p class="spaced">No Calendar permission, Gmail access or WhatsApp connection is needed. Claiming does not publish your profile or add you to the directory.</p>{action}</div>
    <p class="fine">Use the Google account on your registration, or ask the organizer for help. Accounts are not automatically reassigned.</p>'''


def _edit(data):
    p = data['profile']; slug = path(data['event']['slug'])
    intents = ''.join(f'<label class="check"><input type="checkbox" name="intents" value="{key}"{" checked" if key in p.get("intents", []) else ""}><span>{text(label)}</span></label>' for key, label in PURPOSES.items())
    choices = [('hidden','Hidden — only me'),('event','Event attendees'),('public','Anyone with my link')]
    visibility = ''.join(f'<option value="{key}"{" selected" if p.get("visibility", "hidden") == key else ""}>{text(label)}</option>' for key,label in choices)
    content = f'''<div class="form-grid"><div><label for="display_name">Your name</label><input id="display_name" name="display_name" maxlength="80" required value="{text(p.get('display_name'))}"></div>
    <div><label for="username">Your Pippa handle</label><input id="username" name="username" pattern="[a-z][a-z0-9-]{{2,31}}" minlength="3" maxlength="32" required value="{text(p.get('username'))}" autocapitalize="none" spellcheck="false"><p class="caption">3–32 lowercase letters, numbers or hyphens.</p></div>
    <div class="wide"><label for="building">What are you building or exploring?</label><textarea id="building" name="building" rows="3" maxlength="280" placeholder="A small idea, a big problem, or something you would love to learn…">{text(p.get('building'))}</textarea></div></div>
    <fieldset><legend>Open to</legend>{intents}</fieldset><label for="visibility">Who can open your profile?</label><select id="visibility" name="visibility">{visibility}</select>
    <label class="check"><input name="directory_opt_in" type="checkbox"{" checked" if p.get('directory_opt_in') else ""}><span><strong>Include me in this event’s attendee directory</strong><small>Other checked-in members can find your name, building statement and interests. Hidden profiles stay out of the directory.</small></span></label>
    <p class="fine spaced">Publishing shares these profile fields with the audience you choose. Your email, phone and Google account details are never shown. Directory consent is separate from visibility; you can change either at any time.</p>
    <div class="actions">{button('Save profile')} {link('View my profile', '/people/' + path(p['user_id'])) if p.get('stable_url') else ''}</div>'''
    return f'<p class="eyebrow">{text(data["event"]["title"])}</p><h1>Your introduction starts here.</h1><p class="lead">A little context makes the first hello easier. Share as much or as little as you like.</p><div class="panel">' + form(f'/events/{slug}/profile', content, data) + '</div><p class="fine">Want calendar booking later? <a href="/connections/calendar">Connect Calendar</a> when you are ready. Standard beta eligibility and capacity checks still apply.</p>' + link('Remove my event data', '/network/privacy#remove')


def _directory(data):
    event = data['event']; cards = ''
    for p in data['people']:
        cards += f'<article class="person-card">{avatar(p["display_name"])}<h2><a href="/people/{path(p["user_id"])}">{text(p["display_name"])}</a></h2><p>{text(p.get("building"))}</p>{tags(p.get("intents", []))}<div class="actions">{link("Say hello", "/people/" + path(p["user_id"]))}</div></article>'
    empty = '<div class="empty"><h2>No introductions yet</h2><p>Try another search, or check back as people choose to join the directory.</p></div>'
    return f'''<p class="eyebrow">{text(event['title'])}</p><h1>Good people. Shared possibilities.</h1><p class="lead">Explore what people are building and find a reason to say hello.</p>
    <form class="search" method="get"><label><span class="sr-only">Search people</span><input name="q" maxlength="100" value="{text(data.get('search'))}" placeholder="Search a name or idea…" aria-label="Search people"></label>{button('Search', False)}</form>
    <p class="caption spaced">{len(data['people'])} profiles · Only attendees who chose to be listed appear here.</p>{'<div class="event-grid">' + cards + '</div>' if cards else empty}'''


def _profile(data):
    p = data['profile']; uid = path(p['user_id'])
    request = ''
    if data.get('is_owner'):
        request = '<p class="message">This is your profile. Your current audience: <strong>' + text({'hidden':'only you','event':'event attendees','public':'anyone with your link'}.get(p['visibility'])) + '</strong>.</p>'
    elif data.get('shared_events'):
        events = ''.join(f'<option value="{text(e["slug"])}">{text(e["title"])}</option>' for e in data['shared_events'])
        purposes = ''.join(f'<option value="{key}">{text(label)}</option>' for key,label in PURPOSES.items())
        content = f'''<h2>Give your next conversation a reason</h2><label for="event_slug">Where you met</label><select id="event_slug" name="event_slug">{events}</select>
        <label for="purpose">I’d like to connect about</label><select id="purpose" name="purpose">{purposes}</select><label for="note">A personal note</label><textarea id="note" name="note" rows="3" maxlength="500" required placeholder="What caught your interest?">{text(data.get('note'))}</textarea>
        {hidden('idempotency_key', data['idempotency_key'])}<p class="fine spaced">This sends a request to their Pippa inbox. They choose whether to accept.</p>{button('Send a request')}'''
        request = '<div class="panel">' + form(f'/people/{uid}/requests', content, data) + '</div>'
    elif not data.get('signed_in'):
        request = '<p class="message">Event attendees can send a request after signing in through their event page.</p>'
    booking = link('Book 30 minutes', p['booking_url']) if p.get('booking_url') else ''
    return f'''<p class="eyebrow">An introduction, with Pippa</p><div class="profile-head">{avatar(p['display_name'])}<div><h1>{text(p['display_name'])}</h1><p>@{text(p.get('username'))}</p></div></div>
    <div class="profile-layout"><div><h2>Building & exploring</h2><p class="spaced">{text(p.get('building')) or 'An introduction is on its way.'}</p>{tags(p.get('intents', []))}{request}{booking}</div>
    <aside class="share-card"><h2>A hello worth keeping</h2><img src="/people/{uid}/qr.svg" width="180" height="180" alt="QR code for this profile"><label for="profile-link">Profile link</label><input id="profile-link" readonly value="{text(data['profile_url'])}"><p class="fine">This link stays the same when your handle changes. Your visibility choice still applies.</p></aside></div>'''


def _inbox(data):
    rows = ''
    for item in data['requests']:
        other = item['recipient_name'] if item['sender_id'] == data['user_id'] else item['sender_name']
        rows += f'<li><div><a href="/network/inbox/{path(item["id"])}">{text(other)}</a><p>{text(PURPOSES.get(item["purpose"], item["purpose"]))}</p></div><span class="pill">{text(item["state"].replace("_", " ").title())}</span></li>'
    return '<p class="eyebrow">Your next conversations</p><h1>A little follow-through.</h1><p class="lead">Manage requests and agree a time together. Your inbox is private.</p>' + ('<ul class="request-list">' + rows + '</ul>' if rows else '<div class="empty"><h2>Your inbox is quiet</h2><p>Visit the attendee directory and start a conversation.</p></div>')


def _detail(data):
    r = data['request']; rid = path(r['id']); action = '/network/inbox/' + rid; state = r['state']; owner = data['user_id']
    other = r['recipient_name'] if r['sender_id'] == owner else r['sender_name']
    v = hidden('version', r['version']); actions = ''; status = ''
    if state == 'pending':
        status = '<h2>A conversation is waiting</h2><p>Accept to start agreeing a time. No calendar event has been created.</p>'
        if r['recipient_id'] == owner:
            actions = form(action+'/accept', v + button('Accept request'), data) + form(action+'/decline', v + button('Decline', False), data)
        else:
            actions = '<p class="muted">Waiting for the other person to reply.</p>' + form(action+'/withdraw', v + button('Withdraw request', False), data)
    elif state in ('accepted', 'time_proposed'):
        status = '<h2>Let’s find a time.</h2><p>You have agreed to connect. Choose one to three options for the other person to confirm.</p>'
        if r.get('slots'):
            if r.get('proposer_id') == owner:
                actions += '<p class="muted">Waiting for the other person to confirm a time.</p>'
                actions += ''.join('<p>'+when(s['start'],r['timezone'])+'</p>' for s in r['slots'])
            else:
                options = ''.join(f'<label class="slot"><input type="radio" name="slot_index" value="{i}" required><span>{when(slot["start"], r["timezone"])}<small>To {when(slot["end"], r["timezone"])}</small></span></label>' for i,slot in enumerate(r['slots']))
                actions += form(action+'/confirm', v + options + '<p class="fine spaced">Location: '+text(r.get('location'))+'</p>' + button('Confirm this time'), data)
        proposal = ''.join(f'<label for="slot_{i}">Option {i}{" (optional)" if i>1 else ""}</label><input id="slot_{i}" name="slot_{i}" type="datetime-local"{" required" if i==1 else ""}>' for i in range(1,4))
        proposal += '<label for="timezone">Timezone for all options</label><input id="timezone" name="timezone" value="Asia/Singapore" maxlength="64" required><label for="duration">Duration</label><select id="duration" name="duration"><option value="30">30 minutes</option><option value="60">60 minutes</option></select><label for="location">Where would you like to meet?</label><input id="location" name="location" maxlength="200" required placeholder="A cafe, event venue or video meeting location"><p class="fine spaced">Choose future times. These are proposals; availability has not been checked against calendars.</p>' + button('Propose times')
        actions += '<details class="panel"><summary>Propose new time options</summary>' + form(action+'/propose', v + proposal, data) + '</details>'
    elif state == 'time_agreed':
        status = '<h2>Time agreed</h2><p>' + when(r.get('selected_time'),r.get('timezone','UTC')) + '</p><p>'+text(r.get('location'))+'</p><p class="spaced">No calendar event has been created. Add this time to your calendars yourselves.</p>'
    else:
        status = '<h2>' + text(state.replace('_',' ').title()) + '</h2><p>This request is closed.</p>'
    if data.get('booking_url'):
        actions += '<div class="panel"><h2>Prefer their available calendar slots?</h2><p class="spaced">' + link('Book through their existing link', data['booking_url']) + '</p><p class="fine">This opens a separate booking flow. Your time agreement here does not create a calendar event.</p></div>'
    if state == 'time_agreed':
        actions += '<div class="panel"><h2>Make the next one easier</h2><p class="spaced">Calendar booking and the WhatsApp assistant are optional. Standard beta eligibility and capacity checks still apply.</p><div class="actions">' + link('Connect Calendar', '/connections/calendar') + link('Set up WhatsApp', '/connections/whatsapp') + '</div></div>'
    block = form(action+'/block', button('Block this person', False), data)
    return f'<p class="eyebrow">{text(PURPOSES.get(r["purpose"], r["purpose"]))}</p><h1>A conversation with {text(other)}</h1><p class="request-note">{text(r["note"])}</p><div class="status-banner">{status}</div><div class="inline-actions">{actions}</div><details class="panel"><summary>Manage this connection</summary><p class="fine spaced">Blocking prevents further requests and hides profiles between you.</p>{block}</details>'


def _signin(data):
    events = ''.join('<li><div><h2>' + text(event['title']) + '</h2><p class="fine">' + when(event['starts_at']) + '</p></div>' + link('Continue with Google', '/signin/google?event=' + path(event['slug'])) + '</li>' for event in data['events'])
    return '<p class="eyebrow">Welcome back</p><h1>Find your event. Keep in touch.</h1><p class="lead">Choose the event where you checked in, then sign in with the Google account on your registration. Calendar permission is not required.</p>' + ('<ul class="request-list">' + events + '</ul>' if events else '<div class="empty"><h2>No events are available</h2><p>Ask your organizer for the event link.</p></div>') + '<p class="fine">Registration uses another account? Ask the organizer for help. Accounts are not automatically reassigned.</p>'


def _private(data):
    return '<h1>Sign in to check this profile.</h1><p class="lead">Profiles may be private or unavailable. Sign in through your event to see the attendee directory and your inbox.</p>' + link('Find my event', '/network/signin', True)


def _privacy(data):
    if data.get('deleted'):
        detail = 'Your event-only identity and sign-in sessions were also removed.' if data.get('account_deleted') else 'Your existing Pippa account, Calendar connection, booking setup and assistant data were kept.'
        return '<h1>Event data removed</h1><p class="lead">Your event profiles, attendance records, requests and networking blocks have been removed from this app.</p><p>' + detail + '</p><p class="fine">This does not cancel calendar events or remove the organizer’s source registration records or separate backups.</p>'
    remove = link('Sign in through my event', '/network/signin')
    if data.get('signed_in'):
        scope = ('If this is only an event account with no connected services or assistant access, its identity and sessions will also be deleted. Otherwise your Pippa account and connected services are kept.' if data.get('event_only') else 'Your Pippa account, Calendar connection, booking setup and assistant data are kept.')
        remove = '<p>' + scope + '</p><p>This removes all your event profiles, memberships, requests and networking blocks in this app. Existing calendar events and the organizer’s source registration records are unchanged.</p><p class="fine">Removal requires a Google sign-in within the last 15 minutes.</p>' + link('Sign in again', '/network/signin') + form('/network/privacy/delete', '<label for="confirmation">Type DELETE to confirm</label><input id="confirmation" name="confirmation" autocomplete="off" required pattern="DELETE" maxlength="6">' + '<div class="actions">' + button('Remove my event data', False) + '</div>', data)
    return '<p class="eyebrow">Your choice, at every step</p><h1>Event privacy &amp; your data</h1><div class="panel"><h2>What you choose to share</h2><p class="spaced">Claiming a checked-in registration keeps your profile hidden. Publishing chooses an audience: only you, event attendees, or anyone with your link. Joining an event directory is a separate choice. Your name, building statement and interests are shared with your chosen audience; account email, phone and Google credentials are never shown in the directory.</p><h2>Private requests</h2><p class="spaced">Request notes and proposed times are visible to the two participants. The organizer uses registration and check-in records to verify membership and sees aggregate activity counts. Acceptance and time agreement do not create calendar invitations.</p><h2>How long information remains</h2><p class="spaced">Unclaimed registration records become eligible for removal 14 days after the event; the organizer must run retention cleanup. Event discovery closes after 30 days, and cleanup expires pending requests. Claimed profiles and request history remain until you remove them or the organizer removes the data. Public profiles remain visible until their audience is changed or they are removed.</p><p>Backups and source registration records are handled separately by the organizer. Ask the organizer about those records or a registration mismatch; accounts are not automatically reassigned.</p><p><a href="/privacy">Read the general Pippa privacy policy</a></p></div><section class="panel" id="remove"><h2>Remove my event data</h2><div class="spaced">' + remove + '</div></section>'
