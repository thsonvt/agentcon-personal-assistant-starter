"""Public booking rendering: escaping and truthful client booking states."""
import json
import re
import shutil
import subprocess

import pytest

from app.booking_pages import render_public_booking, render_booking_settings


def public_data(**extra):
    return dict(profile=dict(username='son', display_name='Son Le Thanh', title='A conversation',
                             timezone='Asia/Singapore', location='Video call', duration_min=30),
                booking_token='token-123', today='2026-10-01', last_day='2026-10-31', **extra)


def test_public_page_contains_accessible_booking_flow():
    html = render_public_booking(public_data())
    for value in ('Son Le Thanh', '30 minutes', 'Asia/Singapore', 'Select a date',
                  'name="name"', 'autocomplete="name"', 'name="email"', 'autocomplete="email"',
                  'name="booking_token"', 'name="start"', 'name="timezone"',
                  'name="website"', 'aria-live="polite"', 'Confirm booking', 'No account needed'):
        assert value in html
    assert 'https://fonts.' not in html


def test_confirmation_explains_google_guest_invitation_sender():
    html = render_public_booking(public_data())
    assert 'from your host through Google Calendar' in html
    assert 'id="invitation-recipient"' in html
    assert 'Delivery and acceptance are not yet confirmed' in html


def test_host_sees_confirmation_email_problem_without_losing_booking():
    html = render_booking_settings({'profile': {}, 'notification_problem_count': 1})
    assert 'Booking confirmation emails need attention: 1' in html
    assert 'Your meetings remain saved' in html


def test_profile_and_token_cannot_inject_markup_or_end_script():
    data = public_data()
    attack = '</script><img src=x onerror=alert(1)>'
    data['profile'].update(display_name=attack, title=attack, location=attack, username=attack)
    data['booking_token'] = attack
    html = render_public_booking(data)
    assert attack not in html
    assert '&lt;img' in html
    payload = re.search(r'<script id="booking-data" type="application/json">(.*?)</script>', html, re.S)
    assert payload
    assert json.loads(payload[1])['profile']['display_name'] == attack
    assert '\\u003c/script' in payload[1]


def test_unavailable_page_does_not_offer_booking():
    html = render_public_booking(public_data(error='This calendar is temporarily unavailable.'))
    assert 'This calendar is temporarily unavailable.' in html
    assert 'name="email"' not in html
    assert 'Confirm booking' not in html
    assert 'booking-data' not in html


def test_settings_preserve_fields_and_escape_errors():
    html = render_booking_settings(dict(profile=dict(username='son', display_name='Son', title='30 minutes',
                timezone='Asia/Singapore', work_start='09:00', work_end='18:00',
                weekdays=[0, 2, 4], location='" onfocus="bad', enabled=True),
                csrf_token='csrf', base_url='https://example.com', calendar_ready=True,
                error='<script>bad</script>'))
    assert 'action="/connections/scheduling"' in html
    assert 'name="weekdays" value="0,2,4"' in html
    assert 'name="csrf_token" value="csrf"' in html
    assert 'https://example.com/son/30min' in html
    assert '<script>bad</script>' not in html
    assert 'value="&quot; onfocus=&quot;bad"' in html
    for field in ('username', 'display_name', 'title', 'timezone', 'work_start', 'work_end', 'location', 'enabled'):
        assert f'name="{field}"' in html


def test_settings_explains_disconnected_calendar_and_booking_rules():
    html = render_booking_settings(dict(profile={}, csrf_token='csrf', calendar_ready=False))
    assert 'Connect Google Calendar' in html
    assert '2 hours' in html
    assert '30 days' in html
    assert 'No account needed' in html


def test_client_only_confirms_explicit_success_and_never_retries_post_automatically():
    html = render_public_booking(public_data())
    classifier = re.search(r'function bookingOutcome\(httpStatus, body\) \{(.*?)\n\}', html, re.S)
    assert classifier
    if not shutil.which('node'):
        pytest.skip('Node is unavailable')
    js = 'function bookingOutcome(httpStatus, body) {' + classifier[1] + '\n}\n'
    cases = [(200, {'status': 'confirmed'}, 'confirmed'), (201, {'status': 'confirmed'}, 'confirmed'),
             (202, {'status': 'unknown'}, 'unknown'), (500, {'status': 'confirmed'}, 'unknown'),
             (200, {'status': 'pending'}, 'unknown'), (409, {}, 'unavailable'),
             (429, {}, 'limited'), (403, {}, 'expired')]
    js += '\n'.join(f'if (bookingOutcome({status}, {json.dumps(body)}) !== {json.dumps(expected)}) process.exit(1);'
                    for status, body, expected in cases)
    subprocess.run(['node', '-e', js], check=True)
    assert html.count("method: 'POST'") == 1
    assert 'Please do not submit another booking' in html


def test_unavailable_page_without_profile_has_no_private_information_or_form():
    html = render_public_booking({'error': 'Booking is paused.'})
    assert 'Your host' in html
    assert 'Booking is paused.' in html
    assert 'booking-form' not in html


def test_settings_does_not_turn_untrusted_base_url_into_executable_link():
    html = render_booking_settings({'profile': {'username': 'son'}, 'base_url': 'javascript:alert(1)',
                                    'csrf_token': 'csrf', 'calendar_ready': False})
    assert 'href="javascript:' not in html


def test_pending_booking_offers_explicit_check_using_frozen_submission():
    html = render_public_booking(public_data())
    assert 'id="check-booking"' in html
    assert 'Check booking status' in html
    assert 'snapshotBookingPayload' in html
    assert 'body:new URLSearchParams(submittedPayload)' in html
    snapshot = re.search(r'function snapshotBookingPayload\(entries\) \{(.*?)\n\}', html, re.S)
    assert snapshot
    js = 'function snapshotBookingPayload(entries) {' + snapshot[1] + '\n}\n'
    js += '''
const original = [['booking_token','token'],['name','Original'],['start','2026-10-02T01:00:00Z']];
const frozen = snapshotBookingPayload(original);
original[1][1] = 'Changed';
const first = new URLSearchParams(frozen);
const retry = new URLSearchParams(frozen);
if (retry.get('name') !== 'Original' || retry.get('booking_token') !== 'token' || first.toString() !== retry.toString()) process.exit(1);
'''
    subprocess.run(['node', '-e', js], check=True)


def test_settings_validation_matches_backend_limits():
    html = render_booking_settings({'profile': {}, 'csrf_token': 'csrf', 'calendar_ready': True})
    assert 'pattern="[a-z][a-z0-9-]{2,31}"' in html
    assert 'minlength="3" maxlength="32"' in html
    assert 'name="work_start"' in html and 'step="1800"' in html
    assert 'Anyone with your link can book a meeting.' in html


def test_visitor_date_bounds_follow_absolute_horizon_in_selected_timezone():
    data = public_data()
    data.update(now='2026-10-01T00:00:00+00:00', horizon_end='2026-10-31T00:00:00+00:00')
    html = render_public_booking(data)
    payload = re.search(r'<script id="booking-data" type="application/json">(.*?)</script>', html, re.S)
    assert json.loads(payload[1])['now'] == data['now']
    helper = re.search(r'function visitorDateBounds\(config, timeZone\) \{(.*?)\n\}', html, re.S)
    assert helper
    js = 'function visitorDateBounds(config, timeZone) {' + helper[1] + '\n}\n'
    js += 'const config=' + json.dumps(data) + ';\n'
    js += '''
const cases = [
 ['America/Los_Angeles','2026-09-30','2026-10-30'],
 ['Asia/Singapore','2026-10-01','2026-10-31'],
 ['Pacific/Kiritimati','2026-10-01','2026-10-31']
];
for (const [zone,first,last] of cases) {
 const result=visitorDateBounds(config,zone);
 if(result.first!==first || result.last!==last) throw Error(zone+':'+JSON.stringify(result));
}
const fallback=visitorDateBounds({today:'2026-10-01',last_day:'2026-10-31'},'America/Los_Angeles');
if(fallback.first!=='2026-10-01' || fallback.last!=='2026-10-31') process.exit(1);
'''
    subprocess.run(['node', '-e', js], check=True)
