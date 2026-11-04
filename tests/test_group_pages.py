"""Private group pages preserve consent, privacy and truthful booking states."""
import re
import shutil
import subprocess

import pytest

from app.group_pages import PAGE_JS, render_group_page


def page_data(status='waiting', **extra):
    meeting = dict(id='meeting1', title='Call with Tonya and Joel', duration_min=60,
                   timezone='Asia/Singapore', window_start='2026-10-14T14:30:00+08:00',
                   window_end='2026-10-14T17:00:00+08:00', location='', status=status,
                   revision=2, expires_at='2026-10-09T00:00:00Z', host_name='Son',
                   options=[dict(id='option1', start='2026-10-14T06:30:00Z', end='2026-10-14T07:30:00Z')],
                   guests=[dict(name='Tonya', email='tonya@example.com', status='replied', selected=['option1'],
                                link='https://example.com/m/tonya-secret', message='Please select: https://example.com/m/tonya-secret'),
                           dict(name='Joel', email='joel@example.com', status='awaiting', selected=[],
                                link='https://example.com/m/joel-secret', message='Please select: https://example.com/m/joel-secret')])
    return dict(meeting=meeting, csrf_token='csrf-safe', token='own-token', guest_name='Tonya',
                selected=['option1'], timezone='Asia/Riyadh', base_url='https://example.com', **extra)


def test_creation_is_draft_and_includes_exact_participants_and_window():
    html = render_group_page('list', {'csrf_token': 'csrf-safe', 'meetings': []})
    for field in ('title', 'duration_min', 'timezone', 'window_start', 'window_end',
                  'guest1_name', 'guest1_email', 'guest2_name', 'guest2_email', 'location'):
        assert f'name="{field}"' in html
    assert 'action="/connections/meetings"' in html
    assert 'Review available times' in html
    assert 'No invitations are sent' in html
    assert 'type="email"' in html
    assert 'type="datetime-local"' in html


def test_host_draft_requires_explicit_activation_before_links():
    html = render_group_page('host', page_data('draft'))
    assert '/connections/meetings/meeting1/activate' in html
    assert 'Activate request' in html
    assert 'book one time accepted by both guests' in html
    assert 'joel-secret' not in html
    assert 'tonya-secret' not in html


def test_host_waiting_has_each_response_share_draft_and_action():
    html = render_group_page('host', page_data())
    for text in ('Tonya', 'Joel', 'Answered', 'Waiting for reply', 'tonya@example.com',
                 'joel@example.com', 'Copy message', 'not been sent', 'Share privately'):
        assert text in html
    assert '/connections/meetings/meeting1/revise' in html
    assert '/connections/meetings/meeting1/cancel' in html


def test_guest_never_exposes_other_guests_even_when_payload_has_them():
    html = render_group_page('guest', page_data())
    for private in ('joel@example.com', 'tonya@example.com', 'joel-secret', 'tonya-secret'):
        assert private not in html
    assert 'No account needed' in html
    assert 'name="selected" value="option1" checked' in html
    assert 'name="revision" value="2"' in html
    assert 'name="csrf_token" value="csrf-safe"' in html
    assert 'name="response" value="submit"' in html
    assert 'action="/m/own-token"' in html
    assert 'None of these times work' in html
    assert 'permission to book one' in html
    assert 'Asia/Riyadh' in html
    assert '9:30' in html


@pytest.mark.parametrize('view', ['host', 'guest'])
def test_pending_never_claims_booked_or_allows_revise_cancel(view):
    html = render_group_page(view, page_data('pending'))
    assert 'Checking the booking' in html
    assert 'not yet confirmed' in html
    assert 'You’re booked' not in html
    assert '/revise' not in html
    assert '/cancel' not in html
    assert 'name="selected"' not in html
    if view == 'host':
        assert '/connections/meetings/meeting1/check' in html


@pytest.mark.parametrize('status', ['cancelled', 'expired', 'booked'])
def test_terminal_guest_cannot_submit_availability(status):
    html = render_group_page('guest', page_data(status))
    assert 'name="selected"' not in html
    assert 'Save availability' not in html


def test_all_forms_use_post_and_csrf():
    html = render_group_page('host', page_data())
    forms = re.findall(r'<form\b.*?</form>', html, re.S)
    assert forms
    assert all('method="post"' in form and 'name="csrf_token"' in form for form in forms)


def test_untrusted_text_and_urls_cannot_inject_html_or_script():
    data = page_data()
    attack = '</script><img src=x onerror=alert(1)>'
    data['meeting'].update(title=attack, location=attack, event_url='javascript:alert(1)')
    data.update(guest_name=attack, error=attack, csrf_token='" onfocus="bad', token='"/><script>bad</script>')
    for view in ('guest', 'host', 'error'):
        html = render_group_page(view, data)
        assert attack not in html
        assert 'href="javascript:' not in html
        assert '<script>bad</script>' not in html
        assert '&lt;img' in html
    html = render_group_page('guest', data)
    assert 'value="&quot; onfocus=&quot;bad"' in html


def test_guest_has_no_external_resources_and_private_link_does_not_leak_in_referer():
    html = render_group_page('guest', page_data())
    assert 'name="referrer" content="same-origin"' in html
    assert 'name="robots" content="noindex,nofollow"' in html
    assert '<script src=' not in html
    assert '<link ' not in html


def test_no_common_explains_next_action_and_host_can_revise():
    html = render_group_page('host', page_data('no_common'))
    assert 'No shared time yet' in html
    assert 'new window' in html
    assert '/revise' in html


def test_booked_calendar_link_rejects_untrusted_schemes():
    data = page_data('booked')
    data['meeting'].update(event_url='javascript:alert(1)', selected_start='2026-10-14T06:30:00Z')
    html = render_group_page('host', data)
    assert 'Meeting booked' in html
    assert 'href="javascript:' not in html
    assert 'Delivery and acceptance' in html


def test_unknown_view_fails_closed():
    with pytest.raises(ValueError):
        render_group_page('unknown', {})


def test_host_reminder_flag_prompts_manual_follow_up():
    data = page_data()
    data['meeting']['needs_reminder'] = True
    assert 'more than 48 hours' in render_group_page('host', data)


def test_guest_timezone_labels_and_expiry_can_follow_selected_timezone():
    html = render_group_page('guest', page_data())
    assert 'data-zone-label' in html
    assert 'data-instant="2026-10-09T00:00:00Z"' in html
    assert "querySelectorAll('[data-zone-label]')" in html
    assert "querySelectorAll('[data-instant]')" in html


def test_host_form_validation_preserves_submitted_fields():
    html = render_group_page('list', {'error': 'Choose a longer window.', 'values': {
        'title': 'Retain this title', 'guest1_name': 'A', 'guest1_email': 'a@example.com',
        'timezone': 'Asia/Riyadh', 'duration_min': 30, 'window_start': '2026-10-14T09:30'}})
    assert 'value="Retain this title"' in html
    assert 'value="a@example.com"' in html
    assert 'value="2026-10-14T09:30"' in html
    assert '<option value="30" selected>' in html


def test_revision_error_retains_submitted_window_instead_of_old_window():
    data = page_data(error='No free times in this window.', values={
        'timezone': 'Asia/Riyadh', 'window_start': '2026-10-16T11:00', 'window_end': '2026-10-16T13:00'})
    html = render_group_page('host', data)
    assert 'value="2026-10-16T11:00"' in html
    assert 'value="2026-10-16T13:00"' in html


def test_guest_fall_back_options_distinguish_start_and_end_offsets_without_javascript():
    data = page_data()
    data['timezone'] = 'America/New_York'
    data['meeting']['options'] = [
        dict(id='first', start='2026-11-01T05:00:00Z', end='2026-11-01T05:30:00Z'),
        dict(id='second', start='2026-11-01T06:00:00Z', end='2026-11-01T06:30:00Z'),
        dict(id='crossing', start='2026-11-01T05:30:00Z', end='2026-11-01T06:30:00Z')]
    html = render_group_page('guest', data)
    labels = re.findall(r'<span data-slot-start="[^"]+" data-slot-end="[^"]+">([^<]+)</span>', html)
    assert len(labels) == 3
    assert labels[0].count('UTC-04:00') == 2
    assert labels[1].count('UTC-05:00') == 2
    assert 'UTC-04:00' in labels[2] and 'UTC-05:00' in labels[2]
    assert labels[0] != labels[1]


def test_timezone_change_distinguishes_fall_back_options_in_executed_javascript():
    if not shutil.which('node'):
        pytest.skip('Node is unavailable')
    setup = r'''
let change;
const select = {value:'America/New_York', id:'guest-timezone', options:[{value:'America/New_York'}],
  append(option){this.options.push(option)}, addEventListener(name,callback){change=callback}};
const slots = [
  {dataset:{slotStart:'2026-11-01T05:00:00Z',slotEnd:'2026-11-01T05:30:00Z'}},
  {dataset:{slotStart:'2026-11-01T06:00:00Z',slotEnd:'2026-11-01T06:30:00Z'}},
  {dataset:{slotStart:'2026-11-01T05:30:00Z',slotEnd:'2026-11-01T06:30:00Z'}}];
globalThis.document = {
  querySelectorAll(selector){return selector==='[data-timezone]'?[select]:selector==='[data-slot-start]'?slots:[]},
  createElement(){return {}}
};
'''
    assertions = r'''
change();
const assert = require('node:assert/strict');
assert.equal((slots[0].textContent.match(/GMT-4/g)||[]).length,2);
assert.equal((slots[1].textContent.match(/GMT-5/g)||[]).length,2);
assert.ok(slots[2].textContent.includes('GMT-4') && slots[2].textContent.includes('GMT-5'));
assert.notEqual(slots[0].textContent, slots[1].textContent);
'''
    subprocess.run(['node', '-e', setup + PAGE_JS + assertions], check=True, capture_output=True, text=True)
