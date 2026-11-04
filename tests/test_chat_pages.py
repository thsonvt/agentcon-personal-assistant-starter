"""The chat is a plain-text readback with one explicit invitation gate."""
import re
import shutil
import subprocess

import pytest

from app import chat_pages
from app.chat_pages import render_chat_page


def page_data(status='collecting'):
    return {'csrf_token': 'safe-token', 'chat': {
        'id': 'chat1', 'revision': 3, 'status': status,
        'messages': [{'role': 'user', 'content': 'Arrange a meeting with Ana and Ben.'},
                     {'role': 'assistant', 'content': 'Which timezone should I use?'}],
        'proposal': {'title': 'Project catch-up', 'duration_min': 60,
                     'timezone': 'America/New_York',
                     'start': '2026-11-01T05:30:00Z', 'end': '2026-11-01T06:30:00Z',
                     'location': 'Library',
                     'guests': [{'name': 'Ana', 'email': 'ana@example.com'},
                                {'name': 'Ben', 'email': 'ben@example.com'}],
                     'sources': ['Ana: November 1, 1:30 AM.', 'Ben: November 1, 1–3 AM.']},
        'meeting': None}}


def forms(html):
    return re.findall(r'<form\b.*?</form>', html, re.S)


def test_landing_has_one_accessible_composer_and_no_structured_fields():
    html = render_chat_page({'csrf_token': 'safe-token', 'chat': None})
    assert html.count('<textarea') == 1
    assert '<label for="message">Message</label>' in html
    assert 'name="message"' in html and 'required' in html
    assert 'action="/connections/chat"' in html
    assert '>Submit</button>' in html
    assert 'name="revision" value="0"' in html
    assert 'two guests' in html
    assert 'Example' in html
    for name in ('title', 'duration_min', 'timezone', 'guest1_name', 'guest1_email'):
        assert f'name="{name}"' not in html
    for path in ('/connections/chat', '/connections', '/connections/meetings'):
        assert f'href="{path}"' in html


def test_conversation_and_clarification_are_preserved_in_followup_form():
    html = render_chat_page(page_data())
    assert 'Arrange a meeting with Ana and Ben.' in html
    assert 'Which timezone should I use?' in html
    assert 'action="/connections/chat/chat1/message"' in html
    assert 'name="revision" value="3"' in html
    assert '/confirm' not in html


def test_proposal_reads_back_exact_guests_source_and_both_offsets():
    html = render_chat_page(page_data('ready'))
    for value in ('Project catch-up', '60 minutes', 'America/New_York', '01 Nov 2026',
                  'UTC-04:00', 'UTC-05:00', 'Library', 'Ana', 'Ben',
                  'ana@example.com', 'ben@example.com', 'Ana: November 1, 1:30 AM.'):
        assert value in html
    assert '<details>' in html and 'Availability you provided' in html
    assert 'Confirm and send invites' in html
    confirm = next(form for form in forms(html) if '/confirm"' in form)
    assert re.findall(r'name="([^"]+)"', confirm) == ['csrf_token', 'revision']
    assert 'value="3"' in confirm
    assert 'message' not in confirm
    assert 'new message' in html.lower()
    assert 'guests accepted' not in html.lower()


def test_pending_has_status_check_and_no_confirmation_or_composer():
    html = render_chat_page(page_data('pending'))
    assert 'Check booking status' in html
    assert 'action="/connections/chat/chat1/check"' in html
    assert 'not yet confirmed' in html
    assert '/confirm' not in html
    assert '<textarea' not in html


def test_booked_distinguishes_created_requested_delivery_and_acceptance():
    html = render_chat_page(page_data('booked'))
    assert 'Event created' in html
    assert 'invitations requested' in html
    assert 'Delivery and acceptance are not yet confirmed' in html
    assert '/confirm' not in html and '/check"' not in html
    assert '<textarea' not in html


@pytest.mark.parametrize('status', ['collecting', 'ready', 'pending', 'booked'])
def test_forms_have_csrf_and_revision(status):
    html = render_chat_page(page_data(status))
    assert len(forms(html)) == {'collecting': 1, 'ready': 2, 'pending': 1, 'booked': 0}[status]
    for form in forms(html):
        assert 'method="post"' in form
        assert 'name="csrf_token" value="safe-token"' in form
        assert 'name="revision" value="3"' in form


def test_all_conversation_and_proposal_content_is_escaped_not_executed():
    data = page_data('ready')
    attack = '</textarea><script>alert(1)</script><img src=x onerror=alert(1)>'
    data['chat']['messages'][0]['content'] = attack
    data['chat']['messages'][1]['content'] = '**bold**\n' + attack
    data['chat']['proposal'].update(title=attack, location=attack, sources=[attack])
    data['chat']['proposal']['guests'][0].update(name=attack, email=attack)
    data.update(error=attack, notice=attack, csrf_token='" onfocus="bad')
    data['chat']['id'] = '"><script>bad</script>'
    html = render_chat_page(data)
    assert attack not in html
    assert '<script>alert(1)</script>' not in html
    assert '&lt;script&gt;' in html
    assert '**bold**' in html and '<strong>bold' not in html
    assert 'value="&quot; onfocus=&quot;bad"' in html
    assert '/connections/chat/%22%3E%3Cscript%3Ebad%3C%2Fscript%3E/confirm' in html
    assert 'role="alert"' in html


def test_private_accessible_page_uses_local_assets_and_wraps_long_text():
    html = render_chat_page(page_data())
    assert 'name="viewport" content="width=device-width,initial-scale=1"' in html
    assert 'name="robots" content="noindex,nofollow"' in html
    assert 'name="referrer" content="same-origin"' in html
    assert 'Skip to content' in html
    assert 'aria-label="Conversation"' in html
    assert 'white-space:pre-wrap' in html and 'overflow-wrap:anywhere' in html
    assert 'textarea:focus-visible' in html
    assert '<script src=' not in html and '<link ' not in html


def test_invalid_status_does_not_expose_booking_or_message_actions():
    html = render_chat_page(page_data('unknown'))
    assert 'Status unavailable' in html
    assert not forms(html)


def test_proposal_crossing_midnight_reads_back_both_calendar_dates():
    data = page_data('ready')
    data['chat']['proposal'].update(timezone='Asia/Singapore',
                                   start='2026-12-31T15:30:00Z',
                                   end='2026-12-31T16:30:00Z')
    html = render_chat_page(data)
    assert '31 Dec 2026' in html and '01 Jan 2027' in html
    assert html.count('UTC+08:00') == 2


def test_internal_messages_and_unrelated_payload_fields_are_not_exposed():
    data = page_data()
    data['chat']['messages'].append({'role': 'system', 'content': 'private system instructions'})
    data['chat']['api_key'] = 'private-api-key'
    html = render_chat_page(data)
    assert 'private system instructions' not in html
    assert 'private-api-key' not in html


def run_chat_js(assertions, setup=''):
    if not shutil.which('node'):
        pytest.skip('Node is unavailable')
    environment = r'''
const assert = require('node:assert/strict');
const listeners = {};
const submit = {disabled:false, textContent:'Submit'};
const feedback = {textContent:''};
const form = {attributes:{}, addEventListener(name, callback){listeners[name] = callback},
  querySelector(){return submit}, setAttribute(name,value){this.attributes[name] = value},
  removeAttribute(name){delete this.attributes[name]}};
const composer = {form};
const makeTarget = name => ({name, calls:[],
  getBoundingClientRect(){return {top:1400,bottom:1600}},
  scrollIntoView(options){this.calls.push(options)}});
const first = makeTarget('first'), latest = makeTarget('latest');
const nodes = {'#message':composer, '#submit-status':feedback};
globalThis.window = {innerHeight:800,
  requestAnimationFrame(callback){callback()},
  addEventListener(name, callback){listeners[name] = callback}};
globalThis.document = {querySelector(selector){return nodes[selector] || null},
  querySelectorAll(selector){return selector === '.bubble.assistant' ? [first,latest] : []}};
'''
    subprocess.run(['node', '-e', environment + setup + getattr(chat_pages, 'CHAT_JS', '') + assertions],
                   check=True, capture_output=True, text=True)


def test_latest_assistant_is_scrolled_into_view_without_moving_composer_focus():
    run_chat_js('assert.equal(latest.calls.length,1); assert.equal(first.calls.length,0);')


@pytest.mark.parametrize('selector', ['.proposal', '.booking-status', '[role="alert"]'])
def test_readback_status_and_errors_take_precedence_over_old_bubbles(selector):
    setup = f'const priority = makeTarget("priority"); nodes[{selector!r}] = priority;'
    run_chat_js('assert.equal(priority.calls.length,1); assert.equal(latest.calls.length,0);', setup)


def test_message_submit_feedback_blocks_duplicates_without_changing_payload():
    run_chat_js(r'''
let prevented = 0;
listeners.submit({preventDefault(){prevented++}});
assert.equal(prevented,0);
assert.equal(submit.disabled,true);
assert.equal(feedback.textContent,'Pippa is reading…');
assert.equal(form.attributes['aria-busy'],'true');
assert.equal(composer.disabled,undefined);
listeners.submit({preventDefault(){prevented++}});
assert.equal(prevented,1);
listeners.pageshow();
assert.equal(submit.disabled,false);
assert.equal(feedback.textContent,'');
listeners.submit({preventDefault(){prevented++}});
assert.equal(prevented,1);
''')


def test_message_feedback_is_live_and_native_post_remains_available():
    html = render_chat_page(page_data('ready'))
    assert 'id="submit-status" role="status" aria-live="polite"' in html
    assert 'method="post" action="/connections/chat/chat1/message"' in html
    assert 'Confirm and send invites' in html
    assert 'autofocus' not in html


def test_correction_submission_disables_old_proposal_confirmation_until_back():
    setup = r'''
const confirm = {disabled:false};
nodes['.proposal button[type="submit"]'] = confirm;
const hiddenFields = [{name:'csrf_token',disabled:false}, {name:'revision',disabled:false}];
form.elements = [composer, ...hiddenFields, submit];
'''
    run_chat_js(r'''
let prevented = 0;
assert.equal(confirm.disabled,false);
listeners.submit({preventDefault(){prevented++}});
assert.equal(confirm.disabled,true);
assert.equal(submit.disabled,true);
assert.equal(prevented,0);
assert.equal(composer.disabled,undefined);
assert.ok(hiddenFields.every(field => !field.disabled));
listeners.pageshow();
assert.equal(confirm.disabled,false);
assert.equal(submit.disabled,false);
''', setup)
