"""Small, accessible personal booking pages; visitors never need an account."""
from html import escape
import json
from urllib.parse import quote, urlsplit

from app.connection_pages import CSS


BOOKING_CSS = """
:root { color-scheme:light; --bg:#faf7f1; --surface:#fffdf9; --ink:#392f29; --muted:#71675e; --line:#e4dccf; --soft:#f0e9de; --clay:#a14f32; --on-clay:#fffaf4; --good:#4a6346; --bad:#91432f }
body { min-height:100vh } .booking-app { max-width:1050px; margin:40px auto; box-shadow:0 14px 60px #49332108 }
.booking-shell { display:grid; grid-template-columns:330px minmax(0,1fr); min-height:540px }
.host { padding:40px 32px; border-right:1px solid var(--line); background:var(--surface) }.host h1 { font-size:36px; margin:18px 0 24px }
.host-name { font-weight:600; font-size:15px; margin:18px 0 0; overflow-wrap:anywhere }.avatar { display:grid; place-items:center; height:54px; width:54px; border-radius:50%; background:var(--soft); color:var(--clay); font:26px Georgia,serif }
.facts { display:grid; gap:16px; margin:0; padding:0; list-style:none; color:var(--muted); font-size:13px }.facts li { display:flex; gap:10px; align-items:flex-start; overflow-wrap:anywhere }.fact-icon { color:var(--clay); font-size:17px; line-height:1.2; width:18px; flex:0 0 18px }.host-note { margin:32px 0 0; font-size:12px; color:var(--muted) }
.picker { padding:36px 32px; min-width:0 }.picker h2 { font:24px Georgia,serif; margin-bottom:7px }.calendar-heading { display:flex; align-items:center; justify-content:space-between; margin:26px 0 16px }.calendar-heading h3 { margin:0; font-size:15px }.month-controls { display:flex; gap:6px }.arrow { border:1px solid var(--line); background:transparent; color:var(--ink); border-radius:50%; width:36px; height:36px; cursor:pointer }.arrow:disabled { opacity:.35; cursor:default }
.calendar { display:grid; grid-template-columns:repeat(7,minmax(0,1fr)); gap:5px }.weekday { text-align:center; color:var(--muted); font-size:11px; padding:5px 0 10px }.day { border:0; border-radius:9px; background:var(--soft); color:var(--ink); min-height:40px; cursor:pointer; font-size:14px }.day:hover { background:#e9d8c7 }.day:disabled { background:transparent; color:var(--muted); opacity:.45; cursor:default }.day[aria-pressed=true] { background:var(--clay); color:var(--on-clay); font-weight:600 }.day.today:not([aria-pressed=true]) { box-shadow:inset 0 0 0 1px var(--clay) }
select { font:inherit; color:var(--ink); background:var(--surface); border:1px solid var(--line); border-radius:9px; padding:11px; width:100%; min-width:0 } select:focus-visible { outline:3px solid var(--clay); outline-offset:3px }
.slots { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:9px; margin:14px 0 }.slot { padding:10px 5px; border:1px solid var(--clay); border-radius:8px; background:var(--surface); color:var(--clay); min-height:44px; cursor:pointer }.slot[aria-pressed=true] { background:var(--clay); color:var(--on-clay) }
.slot-heading { font-size:14px; margin:25px 0 8px }.status-text { color:var(--muted); font-size:13px; margin:12px 0; min-height:20px }.details { border-top:1px solid var(--line); margin-top:24px; padding-top:23px }.details h3 { font:22px Georgia,serif }.selected-summary { background:var(--soft); border-radius:9px; padding:12px; margin:15px 0; font-size:13px }.details .primary { width:100%; margin-top:20px; min-height:48px }.details button:disabled { opacity:.55; cursor:wait }.details[hidden], [hidden] { display:none!important }.trap { position:absolute; left:-10000px; width:1px; height:1px; overflow:hidden }
.confirmation { padding:38px 0 }.confirmed-icon { display:grid; place-items:center; width:55px; height:55px; border-radius:50%; background:#e9efe4; color:var(--good); font-size:25px; margin-bottom:22px }.confirmation h2 { font:32px Georgia,serif }.confirmation dl { display:grid; grid-template-columns:80px minmax(0,1fr); gap:12px; margin:26px 0 }.confirmation dt { color:var(--muted); font-size:12px }.confirmation dd { margin:0; font-size:14px; overflow-wrap:anywhere }
.settings { max-width:750px }.settings-main { padding:36px }.settings-grid { display:grid; grid-template-columns:1fr 1fr; gap:0 20px }.settings-grid .wide { grid-column:1/-1 }.check-label { display:flex; gap:10px; align-items:center; margin:0; font-size:13px }.check-label input { width:18px; min-height:18px; accent-color:var(--clay) }.days-options { display:flex; gap:17px; flex-wrap:wrap; margin:13px 0 20px }.days-options label { margin:0 }.days-fieldset { border:0; margin:20px 0 0; padding:0 }.days-fieldset legend { font-size:13px; font-weight:500 }.share-link { background:var(--soft); border-radius:10px; padding:15px; margin:20px 0; overflow-wrap:anywhere }.settings .publish { border-top:1px solid var(--line); padding-top:22px; margin-top:25px }.notice { background:#edf1e9; color:var(--good); border-radius:10px; padding:13px 16px }
@media(max-width:760px) { .booking-app { margin:0; border:0; border-radius:0; box-shadow:none }.booking-shell { grid-template-columns:1fr }.host { border-right:0; border-bottom:1px solid var(--line); padding:24px }.avatar { width:42px; height:42px; font-size:22px }.host-name { margin:12px 0 0 }.host h1 { font-size:30px; margin:12px 0 20px }.facts { gap:10px }.host-note { margin-top:18px }.picker { padding:27px 24px }.settings-main { padding:26px 22px } }
@media(max-width:400px) { .picker,.host { padding:24px 18px }.slots { grid-template-columns:repeat(2,minmax(0,1fr)) }.settings-grid { grid-template-columns:1fr }.settings-grid .wide { grid-column:auto }.day { min-height:37px }.calendar { gap:4px } }
"""


def _text(value):
    return escape(str(value if value is not None else ''), quote=True)


def _json(value):
    return json.dumps(value, ensure_ascii=True).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')


def _page(title, content, *, settings=False):
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta name="robots" content="noindex,nofollow">'
            f'<title>{_text(title)} · Pippa</title><style>{CSS}{BOOKING_CSS}</style></head><body>'
            '<a class="skip" href="#main">Skip to content</a>'
            f'<div class="app booking-app{" settings" if settings else ""}">'
            '<header class="top"><a href="/" class="brand" aria-label="Pippa home"><span class="brand-dot" aria-hidden="true"></span>Pippa</a>'
            + ('<a href="/connections">← Connections</a>' if settings else '<span class="meta">A little less back-and-forth.</span>')
            + f'</header>{content}<footer class="footer"><span>Made simple with Pippa</span><a href="/privacy">Privacy</a></footer></div></body></html>')


def render_public_booking(data):
    profile = data.get('profile') or {}
    name = profile.get('display_name') or 'Your host'
    title = profile.get('title') or '30-minute meeting'
    location = profile.get('location') or 'Location arranged with your host'
    host = (f'<aside class="host"><div class="avatar" aria-hidden="true">{_text(str(name)[:1].upper())}</div>'
            f'<p class="host-name">{_text(name)}</p><h1>{_text(title)}</h1>'
            '<ul class="facts"><li><span class="fact-icon" aria-hidden="true">◷</span><span>30 minutes</span></li>'
            f'<li><span class="fact-icon" aria-hidden="true">⌖</span><span>{_text(location)}</span></li>'
            '<li><span class="fact-icon" aria-hidden="true">✓</span><span>No account needed</span></li></ul>'
            '<p class="host-note">Choose a time that works for you.<br>We’ll add it to your host’s calendar and request an email invitation for you.</p></aside>')
    if data.get('error'):
        return _page(title, '<main id="main" class="booking-shell">' + host
                     + '<section class="picker"><p class="kicker">Booking is unavailable</p><h2>Let’s find another time.</h2>'
                     + f'<p class="message error" role="alert">{_text(data["error"])}</p>'
                     + '<p class="lead">Please contact your host or try this page again later.</p></section></main>')
    picker = ('<section class="picker"><div id="choose"><p class="kicker">Make a little time</p>'
              '<h2 id="date-title">Select a date</h2><p class="caption">Then choose a time for your conversation.</p>'
              '<div class="calendar-heading"><h3 id="month-label" aria-live="polite"></h3><div class="month-controls">'
              '<button class="arrow" id="previous-month" type="button" aria-label="Previous month">←</button>'
              '<button class="arrow" id="next-month" type="button" aria-label="Next month">→</button></div></div>'
              '<div class="calendar" id="calendar" role="group" aria-labelledby="date-title"></div>'
              '<label for="display-timezone">Your timezone</label><select id="display-timezone" aria-describedby="timezone-note"></select>'
              '<p class="caption" id="timezone-note">All times below are in your selected timezone.</p>'
              '<h3 class="slot-heading" id="slot-heading">Available times</h3><p id="slots-status" class="status-text" role="status" aria-live="polite"></p>'
              '<div id="slots" class="slots" role="group" aria-labelledby="slot-heading"></div>'
              '<form id="booking-form" class="details" hidden><h3>Your details</h3><p class="selected-summary" id="selected-summary"></p>'
              f'<input type="hidden" name="booking_token" value="{_text(data.get("booking_token"))}">'
              '<input type="hidden" name="start" id="selected-start"><input type="hidden" name="timezone" id="selected-timezone">'
              '<label for="guest-name">Your name</label><input id="guest-name" name="name" autocomplete="name" maxlength="100" required>'
              '<label for="guest-email">Email address</label><input id="guest-email" name="email" type="email" autocomplete="email" maxlength="254" required>'
              '<p class="caption spaced">We’ll use this address for your calendar invitation.</p>'
              '<div class="trap" aria-hidden="true"><label for="website">Leave this blank</label><input id="website" name="website" tabindex="-1" autocomplete="off"></div>'
              '<button type="submit" class="btn primary" id="confirm-booking">Confirm booking →</button>'
              '<p class="caption spaced">Your name and email will be shared with your host for this meeting. <a href="/privacy">Privacy</a></p></form></div>'
              '<p id="booking-status" class="message" role="status" aria-live="polite" hidden></p>'
              '<button id="check-booking" class="btn primary" type="button" hidden>Check booking status</button>'
              '<section id="confirmation" class="confirmation" tabindex="-1" hidden><div class="confirmed-icon" aria-hidden="true">✓</div>'
              '<h2>You’re booked.</h2><p>Your meeting has been added to your host’s calendar.</p><dl id="confirmation-details"></dl>'
              '<p class="caption">A calendar invitation has been requested for <span id="invitation-recipient"></span> '
              'from your host through Google Calendar. Delivery and acceptance are not yet confirmed. '
              'For changes or cancellation, please contact your host.</p></section>'
              '<noscript><p class="message">Please enable JavaScript to load available times and book your meeting.</p></noscript></section>')
    config = {key: data.get(key) for key in ('profile', 'today', 'last_day', 'now', 'horizon_end')}
    return _page(title, '<main id="main" class="booking-shell">' + host + picker + '</main>'
                 + '<script id="booking-data" type="application/json">' + _json(config) + '</script><script>' + PUBLIC_JS + '</script>')


PUBLIC_JS = r"""
function bookingOutcome(httpStatus, body) {
  if (httpStatus >= 200 && httpStatus < 300 && httpStatus !== 202 && body.status === 'confirmed') return 'confirmed';
  if (httpStatus === 409) return 'unavailable';
  if (httpStatus === 429) return 'limited';
  if (httpStatus === 403) return 'expired';
  return 'unknown';
}
function visitorDateBounds(config, timeZone) {
  const localDay = (instant, fallback) => {
    if (!instant || !Number.isFinite(Date.parse(instant))) return fallback;
    const parts = new Intl.DateTimeFormat('en', {timeZone,year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date(instant));
    const part = name => parts.find(item=>item.type===name).value;
    return part('year')+'-'+part('month')+'-'+part('day');
  };
  return {first:localDay(config.now,config.today),last:localDay(config.horizon_end,config.last_day)};
}
function snapshotBookingPayload(entries) {
  return new URLSearchParams(entries).toString();
}
(() => {
  const config = JSON.parse(document.getElementById('booking-data').textContent);
  const byId = id => document.getElementById(id);
  const timezone = byId('display-timezone');
  const form = byId('booking-form');
  const endpoint = '/booking/' + encodeURIComponent(config.profile.username);
  const parseDay = text => new Date(text + 'T12:00:00Z');
  const dayKey = date => date.toISOString().slice(0, 10);
  let first, last, month, selectedDate, dateBounds, dateWasChosen=false;
  let selectedSlot = null, requestVersion = 0, busy = false, stopped = false;
  let submittedPayload = null, pendingCheck = false;
  const monthFormat = new Intl.DateTimeFormat('en', {month:'long',year:'numeric',timeZone:'UTC'});
  const dateFormat = new Intl.DateTimeFormat('en', {weekday:'long',month:'long',day:'numeric',timeZone:'UTC'});
  let zones = ['Asia/Singapore', 'UTC', 'Europe/London', 'America/New_York', 'America/Los_Angeles', 'Asia/Ho_Chi_Minh', 'Asia/Tokyo', 'Australia/Sydney'];
  try { if (Intl.supportedValuesOf) zones = Intl.supportedValuesOf('timeZone'); } catch (_) {}
  let localZone = config.profile.timezone;
  try { localZone = Intl.DateTimeFormat().resolvedOptions().timeZone || localZone; } catch (_) {}
  zones = [...new Set([localZone, config.profile.timezone, ...zones])];
  zones.forEach(zone => { const option = document.createElement('option'); option.value=zone; option.textContent=zone.replaceAll('_',' '); timezone.append(option); });
  timezone.value = localZone;
  function updateDateBounds() {
    dateBounds=visitorDateBounds(config,timezone.value);
    first=parseDay(dateBounds.first);last=parseDay(dateBounds.last);
    if(!dateWasChosen || !selectedDate || selectedDate<dateBounds.first || selectedDate>dateBounds.last) selectedDate=dateBounds.first;
    const selected=parseDay(selectedDate);
    month=new Date(Date.UTC(selected.getUTCFullYear(),selected.getUTCMonth(),1,12));
  }
  updateDateBounds();
  function calendar() {
    const container = byId('calendar'); container.replaceChildren();
    byId('month-label').textContent = monthFormat.format(month);
    ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'].forEach(day => { const el=document.createElement('span'); el.className='weekday'; el.textContent=day; container.append(el); });
    const offset=(month.getUTCDay()+6)%7;
    for(let i=0;i<offset;i++) container.append(document.createElement('span'));
    const days=new Date(Date.UTC(month.getUTCFullYear(),month.getUTCMonth()+1,0)).getUTCDate();
    for(let day=1;day<=days;day++) {
      const date=new Date(Date.UTC(month.getUTCFullYear(),month.getUTCMonth(),day,12)); const key=dayKey(date);
      const button=document.createElement('button'); button.type='button'; button.className='day'+(key===dateBounds.first?' today':'');
      button.textContent=day; button.setAttribute('aria-label',dateFormat.format(date)); button.setAttribute('aria-pressed',key===selectedDate?'true':'false');
      button.disabled=key<dateBounds.first || key>dateBounds.last || busy || stopped;
      button.addEventListener('click',()=>{dateWasChosen=true;selectedDate=key;calendar();loadSlots();}); container.append(button);
    }
    byId('previous-month').disabled=month.getUTCFullYear()*12+month.getUTCMonth()<=first.getUTCFullYear()*12+first.getUTCMonth() || busy || stopped;
    byId('next-month').disabled=month.getUTCFullYear()*12+month.getUTCMonth()>=last.getUTCFullYear()*12+last.getUTCMonth() || busy || stopped;
  }
  function slotSummary(slot) {
    return new Intl.DateTimeFormat('en',{weekday:'short',month:'short',day:'numeric',hour:'numeric',minute:'2-digit',timeZone:timezone.value}).format(new Date(slot.start))+' · 30 minutes · '+timezone.value;
  }
  function choose(slot, button) {
    selectedSlot=slot;
    byId('slots').querySelectorAll('button').forEach(el=>el.setAttribute('aria-pressed',el===button?'true':'false'));
    byId('selected-start').value=slot.start; byId('selected-timezone').value=timezone.value;
    byId('selected-summary').textContent=slotSummary(slot); form.hidden=false;
    byId('guest-name').focus({preventScroll:true});
  }
  async function loadSlots() {
    const version=++requestVersion, date=selectedDate, zone=timezone.value;
    selectedSlot=null; form.hidden=true; byId('slots').replaceChildren();
    byId('slot-heading').textContent=dateFormat.format(parseDay(date)); byId('slots-status').textContent='Checking available times…';
    try {
      const response=await fetch(endpoint+'/slots?'+new URLSearchParams({date:date,tz:zone}),{headers:{Accept:'application/json'},cache:'no-store'});
      const body=await response.json();
      if(version!==requestVersion || busy || stopped) return;
      if(!response.ok || !Array.isArray(body.slots)) {byId('slots-status').textContent=body.error || 'Availability could not be loaded. Please choose a date to try again.';return;}
      if(body.date!==date || body.timezone!==zone) throw new Error('Mismatched availability');
      byId('slots-status').textContent=body.slots.length?'Choose a time to continue.':'No available times on this day. Try another date.';
      body.slots.forEach(slot=>{
        if(typeof slot.start!=='string' || !Number.isFinite(Date.parse(slot.start)) || typeof slot.label!=='string') return;
        const button=document.createElement('button');button.type='button';button.className='slot';button.textContent=slot.label;
        button.setAttribute('aria-pressed','false');button.addEventListener('click',()=>choose(slot,button));byId('slots').append(button);
      });
    } catch (_) {if(version===requestVersion) byId('slots-status').textContent='Availability could not be loaded. Please choose a date to try again.';}
  }
  function setBusy(value) {
    busy=value; timezone.disabled=value || stopped;
    byId('check-booking').disabled=value;
    byId('check-booking').textContent=value?'Checking…':'Check booking status';
    form.querySelectorAll('input').forEach(input=>input.disabled=value || stopped);
    byId('confirm-booking').disabled=value || stopped;
    byId('confirm-booking').textContent=value?'Confirming…':'Confirm booking →';
    byId('slots').querySelectorAll('button').forEach(button=>button.disabled=value || stopped);
    calendar();
  }
  function message(text) { const el=byId('booking-status');el.hidden=false;el.textContent=text; }
  function markPending() {
    stopped=true;pendingCheck=true;byId('check-booking').hidden=false;
    message('We cannot yet confirm this booking. Please do not submit another booking. Use Check booking status to safely check this same request, or contact your host.');
  }
  async function reserve() {
    setBusy(true);message(pendingCheck?'Checking this booking…':'Confirming your meeting…');
    try {
      const response=await fetch(endpoint+'/reserve',{method: 'POST',body:new URLSearchParams(submittedPayload),headers:{Accept:'application/json'}});
      const body=await response.json();const outcome=bookingOutcome(response.status,body);
      if(outcome==='confirmed') {
        stopped=true;pendingCheck=false;byId('check-booking').hidden=true;byId('choose').hidden=true;byId('booking-status').hidden=true;
        const details=byId('confirmation-details');
        byId('invitation-recipient').textContent=new URLSearchParams(submittedPayload).get('email');
        const date=new Date(body.start);
        const when=Number.isFinite(date.getTime())?new Intl.DateTimeFormat('en',{dateStyle:'full',timeStyle:'short',timeZone:body.timezone || timezone.value}).format(date):slotSummary(selectedSlot);
        [['Meeting',body.title || config.profile.title],['When',when],['Timezone',body.timezone || timezone.value],['Location',body.location || 'Arrange with your host']].forEach(([label,value])=>{const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=label;dd.textContent=value;details.append(dt,dd);});
        byId('confirmation').hidden=false;byId('confirmation').focus();
      } else if(outcome==='unavailable' && !pendingCheck) {message(body.error || 'That time is no longer available. Please choose another time.');setBusy(false);await loadSlots();}
      else if(outcome==='limited') message(body.error || 'Too many requests. Please wait a little before trying again.');
      else if(outcome==='expired') {stopped=true;if(pendingCheck){pendingCheck=false;byId('check-booking').hidden=true;message('This session has expired. Please contact your host to check this booking before making another request.');}else message(body.error || 'This booking page has expired. Refresh the page before choosing another time.');}
      else if(response.status===400 && !pendingCheck) message(body.error || 'Please check your name and email, then try again.');
      else markPending();
    } catch (_) {markPending();}
    finally {setBusy(false);}
  }
  form.addEventListener('submit',async event=>{
    event.preventDefault();if(busy || stopped || !selectedSlot || !form.reportValidity())return;
    submittedPayload=snapshotBookingPayload(new FormData(form).entries());
    await reserve();
  });
  byId('check-booking').addEventListener('click',async()=>{
    if(busy || !pendingCheck || submittedPayload===null)return;
    await reserve();
  });
  timezone.addEventListener('change',()=>{updateDateBounds();calendar();loadSlots();});
  byId('previous-month').addEventListener('click',()=>{month.setUTCMonth(month.getUTCMonth()-1);calendar();});
  byId('next-month').addEventListener('click',()=>{month.setUTCMonth(month.getUTCMonth()+1);calendar();});
  calendar();loadSlots();
})();
"""


def render_booking_settings(data):
    profile = data.get('profile') or {}
    defaults = dict(username='', display_name='', title='30-minute meeting', timezone='Asia/Singapore',
                    work_start='09:00', work_end='18:00', location='', weekdays=[0, 1, 2, 3, 4], enabled=False)
    profile = {**defaults, **profile}
    fields = [('username', 'Your username', 'text'), ('display_name', 'Your name', 'text'),
              ('title', 'Meeting title', 'text'), ('timezone', 'Your timezone', 'text'),
              ('work_start', 'Start of your day', 'time'), ('work_end', 'End of your day', 'time'),
              ('location', 'Location or meeting link (optional)', 'text')]
    inputs = ''
    for field, label, kind in fields:
        attrs = (' required' if field != 'location' else '')
        if field == 'username':
            attrs += ' pattern="[a-z][a-z0-9-]{2,31}" minlength="3" maxlength="32" autocomplete="off" autocapitalize="none" aria-describedby="username-note"'
        elif field == 'timezone':
            attrs += ' list="timezone-options" aria-describedby="timezone-help"'
        elif kind == 'text':
            attrs += f' maxlength="{dict(display_name=80, title=120, location=500)[field]}"'
        elif kind == 'time':
            attrs += ' step="1800"'
        inputs += (f'<div class="{"wide" if field in ("title", "location") else ""}"><label for="setting-{field}">{label}</label>'
                   f'<input id="setting-{field}" type="{kind}" name="{field}" value="{_text(profile[field])}"{attrs}>')
        if field == 'username':
            inputs += '<p class="caption spaced" id="username-note">3–32 lowercase letters, numbers or hyphens. Start with a letter; don’t end with a hyphen. Changing it changes your shared link.</p>'
        elif field == 'timezone':
            inputs += '<p class="caption spaced" id="timezone-help">Use a city timezone, such as Asia/Singapore.</p>'
        inputs += '</div>'
    weekdays = profile.get('weekdays') or []
    day_options = ''.join(f'<label class="check-label"><input type="checkbox" class="weekday-choice" value="{index}"'
                          + (' checked' if index in weekdays else '') + f'> {day}</label>'
                          for index, day in enumerate(('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')))
    ready = data.get('calendar_ready', False)
    notice = f'<p class="notice" role="status">{_text(data["notice"])}</p>' if data.get('notice') else ''
    error = f'<p class="message error" role="alert">{_text(data["error"])}</p>' if data.get('error') else ''
    if data.get('notification_problem_count'):
        notice += f'<p class="message" role="status">Booking confirmation emails need attention: {_text(data["notification_problem_count"])}. Your meetings remain saved. Contact Pippa support for help.</p>'
    calendar_note = '' if ready else '<p class="message">Connect Google Calendar and check its connection before publishing. <a href="/connections/calendar">Connect Google Calendar →</a></p>'
    share = ''
    base = str(data.get('base_url') or '').rstrip('/')
    parsed = urlsplit(base)
    if profile['username'] and parsed.scheme in ('https', 'http') and parsed.netloc and not parsed.username and not parsed.password:
        url = base + '/' + quote(str(profile['username']), safe='') + '/30min'
        share = f'<div class="share-link"><p class="caption">Your personal link{" · published" if profile["enabled"] and ready else " · not accepting bookings"}</p><a href="{_text(url)}">{_text(url)}</a></div>'
    content = ('<main id="main" class="settings-main"><p class="kicker">Your personal booking page</p>'
               '<h1>A link. A time. You’re booked.</h1><p class="lead">Share one simple link for a 30-minute meeting. '
               'Pippa checks your primary calendar and the calendars selected in Google Calendar before booking.</p>' + notice + error + calendar_note + share
               + '<form method="post" action="/connections/scheduling" id="settings-form">'
               + f'<input type="hidden" name="csrf_token" value="{_text(data.get("csrf_token"))}">'
               + f'<input type="hidden" name="weekdays" value="{_text(",".join(str(day) for day in weekdays))}" id="weekdays-value">'
               + '<div class="settings-grid">' + inputs + '</div><datalist id="timezone-options">'
               + ''.join(f'<option value="{zone}"></option>' for zone in ('Asia/Singapore', 'Asia/Ho_Chi_Minh', 'Asia/Tokyo', 'Europe/London', 'America/New_York', 'America/Los_Angeles', 'Australia/Sydney', 'UTC'))
               + '</datalist><fieldset class="days-fieldset"><legend>Days you accept meetings</legend><div class="days-options">'
               + day_options + '</div></fieldset><p class="caption">Times follow your selected timezone, including daylight saving where applicable. '
               'Bookings need at least 2 hours’ notice and can be made up to 30 days ahead. Google Calendar conflicts are excluded.</p>'
               + '<div class="publish"><label class="check-label"><input type="checkbox" name="enabled" value="1"'
               + (' checked' if profile['enabled'] else '') + '> Publish my booking page</label>'
               + '<p class="caption spaced">Anyone with your link can book a meeting. No account needed. '
               'Your calendar’s event details stay private.</p></div><div class="actions"><button type="submit" class="btn primary">Save booking page →</button>'
               + '<a href="/connections" class="btn">Back to Connections</a></div></form>'
               + '<noscript><p class="message">Enable JavaScript to change your working days. Other settings can still be saved.</p></noscript></main>'
               + '<script>document.getElementById("settings-form").addEventListener("submit",function(){document.getElementById("weekdays-value").value=Array.from(document.querySelectorAll(".weekday-choice:checked"),el=>el.value).join(",");});</script>')
    return _page('Your booking page', content, settings=True)
