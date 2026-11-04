"""Production connection UI state and safe action contracts."""
from html.parser import HTMLParser
import importlib
import pytest


def render(view, **data):
    try:
        module = importlib.import_module('app.connection_pages')
    except ModuleNotFoundError:
        pytest.fail('The production connection renderer is missing')
    return module.render_connection_page(view, data)


def test_transport_or_malformed_response_does_not_claim_definite_non_delivery():
    for view in ('dashboard', 'whatsapp'):
        html = render(view, active=True, joined=True, onboarding={'attempt_status': 'verified',
                     'reply_status': 'failed', 'reply_error_code': None})
        assert 'Reply not confirmed' in html
        assert 'delivery is unknown' in html
        assert 'Reply could not be delivered' not in html


class Elements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.links, self.forms, self.inputs = [], [], []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'a': self.links.append(attrs)
        if tag == 'form': self.forms.append(attrs)
        if tag == 'input': self.inputs.append(attrs)


def test_welcome_signs_in_without_requesting_service_consent():
    html = render('welcome')
    assert 'href="/signin/google"' in html
    assert 'does not grant' in html
    assert '/connections/google?service=' not in html
    assert 'sample account' not in html


@pytest.mark.parametrize('service', ['calendar', 'gmail'])
def test_service_connect_targets_real_consent_and_explains_beta(service):
    html = render(service, email='person@example.com')
    assert f'href="/connections/google?service={service}"' in html
    assert 'person@example.com' in html
    assert 'verification' in html
    assert 'security warning' in html


def test_connected_calendar_continues_to_optional_gmail():
    html = render('calendar', calendar_status='connected')
    assert 'href="/connections/gmail"' in html
    assert '/connections/google?service=calendar' not in html


def test_gmail_is_optional_without_falsely_marking_it_done():
    html = render('gmail')
    assert 'href="/connections/whatsapp"' in html
    assert 'Skip for now' in html
    assert 'cannot send, delete, or change email' in html
    html = render('dashboard', calendar_status='connected', gmail_status='not_connected')
    assert 'data-step="gmail" class="step"' in html


@pytest.mark.parametrize('status,label', [('not_connected','Not connected'),('needs_attention','Needs attention'),('unknown','Not checked'),('checking','Checking'),('connected','Connected')])
def test_capability_statuses_are_independent(status, label):
    html = render('dashboard', calendar_status='connected', gmail_status=status)
    assert f'data-service="gmail" data-status="{status}"' in html
    assert label in html


@pytest.mark.parametrize('active,calendar,ready', [(True,'connected',True),(False,'connected',False),(True,'needs_attention',False),(True,'unknown',False)])
def test_first_scheduling_cta_requires_active_and_confirmed_calendar(active, calendar, ready):
    html = render('dashboard', active=active, joined=True, calendar_status=calendar, bot_whatsapp='6591234567')
    links = [x['href'] for x in Elements(html).links if x.get('href','').startswith('https://wa.me/')]
    assert bool(links) == ready
    if ready: assert 'calendar' in links[0]


@pytest.mark.parametrize('joined,active,pending,expected', [(False,False,False,'Not linked'),(True,False,True,'Waiting for beta approval'),(True,True,False,'Active')])
def test_whatsapp_does_not_confuse_join_with_activation(joined, active, pending, expected):
    html = render('whatsapp', joined=joined, active=active, approval_pending=pending)
    assert expected in html
    if joined: assert 'name="phone"' not in html


def test_join_requires_nonexpired_code_and_configured_safe_bot_number():
    data = dict(join_code='ABC123', bot_whatsapp='6591234567', phone='+6599990000')
    html = render('whatsapp', **data)
    assert 'https://wa.me/6591234567?text=join%20ABC123' in html
    assert 'send' in html and 'yourself' in html
    for extra in ({'join_expired':True},{'bot_whatsapp':''},{'bot_whatsapp':'x" onclick="evil()'}):
        assert 'https://wa.me/' not in render('whatsapp', **(data | extra))


@pytest.mark.parametrize('view', ['dashboard','whatsapp','booking','disconnect','calendar','gmail'])
def test_all_post_forms_have_csrf_and_local_action(view):
    html = render(view, csrf_token='safe-token', email='person@example.com', booking_url='https://calendly.com/person/intro')
    parsed = Elements(html)
    assert parsed.forms
    assert all(form.get('method') == 'post' and form['action'].startswith('/connections/') for form in parsed.forms)
    assert len([item for item in parsed.inputs if item.get('name')=='csrf_token' and item.get('value')=='safe-token']) == len(parsed.forms)


def test_booking_can_save_and_remove_without_provider_consent():
    html = render('booking', booking_url='https://calendly.com/person/intro')
    assert 'action="/connections/booking"' in html
    assert 'name="booking_url"' in html
    assert 'value=""' in html
    assert 'Remove link' in html
    assert 'No Calendly account access or booking sync' in html
    assert '/connections/google' not in html


def test_untrusted_values_are_escaped_and_unsafe_booking_not_linked():
    attack = '<script>alert("x")</script>'
    html = render('dashboard', email=attack, notice=attack, error=attack, calendar_detail=attack, booking_url='javascript:alert(1)', checked_at=attack)
    assert attack not in html
    assert '&lt;script&gt;' in html
    assert 'href="javascript:' not in html
    assert 'role="alert"' in html
    assert '<script' not in html


def test_disconnect_explains_both_services_and_separate_revocation():
    html = render('disconnect')
    assert 'Calendar and Gmail' in html
    assert 'locally' in html
    assert 'revocation' in html
    assert 'action="/connections/disconnect"' in html
    html = render('dashboard', revocation_status='failed')
    assert 'not confirmed' in html
    assert 'https://myaccount.google.com/connections' in html


def test_responsive_accessible_document_and_no_simulated_controls():
    for view in ('welcome','calendar','gmail','whatsapp','dashboard','booking','disconnect','error'):
        html = render(view)
        assert '<!doctype html>' in html
        assert 'name="viewport"' in html
        assert 'prefers-color-scheme: dark' in html
        assert ':focus-visible' in html
        assert '@media' in html
        assert 'data-scenario' not in html
        assert 'Simulate' not in html
    assert 'for="phone"' in render('whatsapp')
    assert 'for="booking_url"' in render('booking')


def test_data_deletion_uses_existing_privacy_instructions():
    html = render('disconnect')
    assert 'href="/data-deletion"' not in html
    assert 'Privacy &amp; data deletion' in html


def test_whatsapp_missing_join_never_claims_active_even_if_activation_flag_is_set():
    html = render('dashboard', active=True, joined=False, calendar_status='connected', bot_whatsapp='6591234567')
    assert 'Pippa is active' not in html
    assert 'https://wa.me/' not in html


@pytest.mark.parametrize('url', ['https://evil.com/person', 'https://calendly.com@evil.com/person', 'https://user:password@calendly.com/person', 'https://calendly.com:444/person', 'https://calendly.com/', 'https://calendly.com/\nwrong', 'https://calendly.com/\\wrong'])
def test_unapproved_booking_links_are_not_clickable(url):
    html = render('dashboard', booking_url=url)
    links = Elements(html).links
    assert not any(link.get('href') == url for link in links)
    assert 'saved link needs attention' in html


@pytest.mark.parametrize('view', ['dashboard', 'whatsapp'])
@pytest.mark.parametrize('joined,calendar', [(False, 'unknown'), (False, 'connected'), (True, 'unknown')])
def test_approved_incomplete_setup_never_shows_organizer_wait(view, joined, calendar):
    html = render(view, beta_approved=True, joined=joined, calendar_status=calendar, calendar_ready=calendar=='connected')
    assert 'Beta access approved' in html
    assert 'Waiting for beta approval' not in html and 'Beta approval pending' not in html
    assert ('Calendar' if calendar=='unknown' else 'country code') in html
    if not joined: assert 'Your number is linked' not in html
    if view == 'whatsapp' and calendar == 'unknown':
        assert 'href="/connections/calendar"' in html


def test_automatic_mode_never_labels_legacy_pending_as_organizer_wait():
    html = render('dashboard', joined=True, approval_pending=True, auto_activate=True)
    assert 'Waiting for beta approval' not in html and 'Beta approval pending' not in html


def test_phone_form_has_visible_country_code_and_preserves_legacy_national_input():
    html = render('whatsapp', phone='90000002', join_code='ABCDEF', bot_whatsapp='6591234567')
    parsed = Elements(html)
    assert any(i.get('name') == 'country_code' and i.get('value') == '+65' for i in parsed.inputs)
    assert 'Country code' in html and 'Singapore' in html
    assert 'Confirm the country code' in html
    assert 'https://wa.me/' not in html


def test_valid_code_shows_exact_number_and_expiration():
    html = render('whatsapp', phone='6590000002', join_code='ABCDEF',
                  join_code_expires_at='2026-10-01T10:30:00+08:00', bot_whatsapp='6591234567')
    assert '+6590000002' in html and 'join ABCDEF' in html
    assert '2026-10-01T10:30:00+08:00' in html
    assert 'same WhatsApp number' in html


@pytest.mark.parametrize('view', ['whatsapp', 'dashboard'])
def test_meta_blocked_reply_is_visible_without_false_delivery_claim(view):
    html = render(view, active=True, joined=True, onboarding={
        'attempt_status': 'verified', 'attempted_at': '2026-10-01T10:05:00+08:00',
        'reply_status': 'failed', 'reply_error_code': '131030', 'provider_text': '<script>unsafe</script>'})
    assert 'Pippa received your message, but Meta has not enabled replies to this number.' in html
    assert 'Son needs to complete Meta test-recipient setup.' in html
    assert 'WhatsApp number verified' in html
    assert 'Reply delivered' not in html and '<script>unsafe</script>' not in html


@pytest.mark.parametrize('status,copy', [('not_sent', 'Reply has not been sent'),
    ('accepted', 'Meta accepted the reply; delivery is not confirmed'),
    ('delivered', 'Reply delivered'), ('read', 'Reply read'), ('failed', 'delivery is unknown')])
def test_reply_status_distinguishes_acceptance_from_delivery(status, copy):
    html = render('whatsapp', onboarding={'attempt_status':'verified', 'reply_status':status})
    assert copy in html


def test_mismatch_offers_correction_and_preserves_reply_failure():
    html = render('whatsapp', onboarding={'attempt_status':'number_mismatch', 'reply_status':'failed'})
    assert 'country code' in html and 'different WhatsApp number' in html
    assert 'name="country_code"' in html and 'delivery is unknown' in html


def test_pending_poll_has_bounded_visibility_and_edit_guards():
    html = render('whatsapp', onboarding_revision='safe-revision', onboarding={'attempt_status':'waiting'})
    assert '/connections/whatsapp/status' in html and '120000' in html
    assert 'visibilityState' in html and 'navigator.onLine' in html
    assert 'addEventListener("input"' in html and 'Refresh status' in html
    assert 'safe-revision' in html


@pytest.mark.parametrize('view', ['whatsapp', 'dashboard'])
@pytest.mark.parametrize('code,label', [('131030', 'Replies blocked'), ('999', 'Reply failed')])
def test_active_failed_reply_has_prominent_failure_and_blocked_meta_has_no_chat_cta(view, code, label):
    html = render(view, active=True, joined=True, calendar_status='connected', bot_whatsapp='6591234567',
                  onboarding={'attempt_status':'verified', 'reply_status':'failed', 'reply_error_code':code})
    assert label in html
    assert 'Pippa is active' not in html
    assert 'Your number is verified and beta access is approved' in html
    assert 'Reply delivered' not in html and 'Reply read' not in html
    if code == '131030':
        assert 'Talk to Pippa →' not in html
        assert [link.get('href') for link in Elements(html).links if link.get('href', '').startswith('https://wa.me/')] == ['https://wa.me/6591234567?text=status']
        assert 'Retry WhatsApp check' in html
        assert 'After Son completes Meta setup, send status to Pippa to retry the reply check.' in html


def test_onboarding_poll_resumes_when_returning_from_whatsapp():
    """Exercise the actual browser script through visibility, network and edit events."""
    import json
    import re
    import shutil
    import subprocess

    node = shutil.which('node')
    if not node:
        pytest.skip('Node is needed to execute the browser polling regression')
    html = render('whatsapp', onboarding_revision='waiting', onboarding={'attempt_status':'waiting'})
    script = re.search(r'<script>(.*?)</script>', html, re.S).group(1)
    harness = r'''
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = SOURCE;
function browser() {
  let time=0, nextTimer=0, fetches=0, reloads=0, revision='waiting', release=null;
  let defer=false;
  const timers=new Map(), events={}, inputEvents={};
  const document={visibilityState:'visible', getElementById:()=>({dataset:{revision:'waiting'}}),
    addEventListener:(name, cb)=>events[name]=cb,
    querySelectorAll:()=>[{addEventListener:(name, cb)=>inputEvents[name]=cb}]};
  const navigator={onLine:true};
  const window={addEventListener:(name, cb)=>events[name]=cb,
    setTimeout:(cb, ms)=>{const id=++nextTimer;timers.set(id,{cb,at:time+ms});return id;},
    clearTimeout:id=>timers.delete(id), location:{reload:()=>reloads++}};
  const context={document,navigator,window,Date:{now:()=>time},AbortController,
    AbortSignal:{timeout:()=>new AbortController().signal},
    fetch:async()=>{fetches++; if(defer) await new Promise(resolve=>{release=resolve;});
      return {ok:true,json:async()=>({revision})};}};
  vm.runInNewContext(source,context);
  const flush=async()=>{for(let i=0;i<8;i++) await Promise.resolve();};
  return {
    get fetches(){return fetches;},get reloads(){return reloads;},get timerCount(){return timers.size;},
    set revision(value){revision=value;}, set defer(value){defer=value;},
    async advance(ms){time+=ms;for(const [id,t] of [...timers]) if(t.at<=time){timers.delete(id);t.cb();}await flush();},
    async visible(value){document.visibilityState=value?'visible':'hidden';events.visibilitychange?.();await flush();},
    async online(value){navigator.onLine=value;events[value?'online':'offline']?.();await flush();},
    async edit(){inputEvents.input();await flush();},
    async release(){defer=false;release();await flush();}
  };
}
(async()=>{
  // User opens WhatsApp, sends join while this page is hidden, then returns.
  let page=browser();
  await page.visible(false); await page.advance(180000);
  assert.equal(page.fetches,0);
  page.revision='verified'; await page.visible(true);
  assert.equal(page.fetches,1,'Returning must immediately check the completed join');
  assert.equal(page.reloads,1);
  // A fresh two-minute window starts on return; network recovery also resumes once.
  page=browser(); await page.visible(false); await page.advance(180000); await page.visible(true);
  assert.equal(page.fetches,1); assert.equal(page.timerCount,1);
  await page.online(false); await page.advance(8000); assert.equal(page.fetches,1);
  await page.online(true); assert.equal(page.fetches,2); assert.equal(page.timerCount,1);
  await page.online(true); await page.visible(true); assert.equal(page.fetches,2);
  await page.advance(120000); assert.equal(page.fetches,2); assert.equal(page.timerCount,0);
  // Returning cannot overwrite an edited country or number.
  page=browser(); await page.edit(); await page.visible(false); await page.visible(true);
  await page.online(false); await page.online(true); await page.advance(8000);
  assert.equal(page.fetches,0); assert.equal(page.reloads,0);
  // Events during a request never start concurrent requests or duplicate timers.
  page=browser(); page.defer=true; await page.advance(8000);
  await page.visible(false); await page.visible(true); await page.online(false); await page.online(true);
  assert.equal(page.fetches,1);
  await page.release(); await page.advance(0);
  assert.equal(page.fetches,2); assert.equal(page.timerCount,1);
  // Editing during a request also prevents its response from reloading.
  page=browser(); page.defer=true; await page.advance(8000); page.revision='verified';
  await page.edit(); await page.release();
  assert.equal(page.reloads,0); assert.equal(page.timerCount,0);
  console.log('Onboarding visibility, network, time limit, edit, and in-flight regressions passed');
})().catch(error=>{console.error(error);process.exitCode=1});
'''.replace('SOURCE', json.dumps(script))
    result = subprocess.run([node, '-e', harness], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('joined,attempt,bot,expected', [
    (True, 'verified', '6591234567', True),
    (False, 'verified', '6591234567', False),
    (False, 'number_mismatch', '6591234567', False),
    (True, 'number_mismatch', '6591234567', False),
    (True, 'verified', '', False),
    (True, 'verified', 'bad-number', False),
])
def test_failed_reply_retry_requires_verified_join_and_safe_bot(joined, attempt, bot, expected):
    html = render('whatsapp', joined=joined, bot_whatsapp=bot,
                  onboarding={'attempt_status':attempt, 'reply_status':'failed', 'reply_error_code':'131030'})
    assert ('Retry WhatsApp check' in html) == expected
    assert 'Talk to Pippa →' not in html
