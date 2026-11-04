"""SON-92: writes require current, complete availability and exact meeting details."""
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from types import SimpleNamespace

import pytest

from app import assistant_tools as host, delegate_tools as delegate
from app.timeutil import parse_dt
from app.tools.calendar import CalendarError, GoogleCalendar
from tests.fakes import FakeCalendarService, _Request
from tests.test_assistant_tools import make_app
from tests.test_delegate_tools import make as make_delegate

START = '2026-10-02T15:00:00+08:00'
END = '2026-10-02T16:00:00+08:00'
BUSY = [{'start': START, 'end': END}]
OWN = {'id': 'own', 'summary': 'Call', 'start': {'dateTime': START}, 'end': {'dateTime': END}}


def writes(svc):
    return [c for c in svc.calls if c[0] in ('insert', 'patch')]


def personal(app):
    return host.create_event(app, 'Focus', START, END, booking_mode='personal')


def invitation(app, **extra):
    return host.create_event(app, 'Call', START, END, 'guest@example.com',
        booking_mode='invitation', attendee_name='Guest', **extra)


def test_owner_busy_slot_never_writes():
    svc = FakeCalendarService(busy=BUSY)
    assert personal(make_app(svc)).startswith('ERROR:')
    assert not writes(svc)


def test_changed_availability_after_confirmation_never_writes():
    svc = FakeCalendarService()
    app = make_app(svc)
    assert invitation(app).startswith('CONFIRM:')
    svc.busy = BUSY
    app.run_no += 1
    assert invitation(app, confirmed=True).startswith('ERROR:')
    assert not writes(svc)
    assert not app.pending_bookings


@pytest.mark.parametrize('payload', [None, {}, {'calendars': {}}, {'calendars': {'primary': {}}},
    {'calendars': {'primary': {'busy': None}}}, {'calendars': {'primary': {'busy': [{}]}}},
    {'calendars': {'primary': {'busy': [{'start': '2026-10-02T15:00', 'end': END}]}}},
    {'calendars': {'primary': {'busy': [{'start': END, 'end': START}]}}}])
def test_incomplete_freebusy_is_failure_not_free(payload):
    svc = FakeCalendarService()
    svc.freebusy = lambda: SimpleNamespace(query=lambda **kw: _Request(payload))
    with pytest.raises(CalendarError):
        GoogleCalendar(svc).busy_periods(parse_dt(START), parse_dt(END))
    assert personal(make_app(svc)).startswith('ERROR:')
    assert not writes(svc)


def test_move_excludes_only_itself_not_merged_overlap():
    svc = FakeCalendarService(items=[OWN, {**OWN, 'id': 'other'}], busy=BUSY)
    out = host.update_event(make_app(svc), 'own', new_start=START)
    assert out.startswith('ERROR:')
    assert not writes(svc)
    assert svc.items[0] == OWN


def test_move_can_overlap_its_own_old_time():
    svc = FakeCalendarService(items=[OWN], busy=BUSY)
    assert host.update_event(make_app(svc), 'own', new_start='2026-10-02T15:30+08:00').startswith('Updated:')
    assert len(writes(svc)) == 1


@pytest.mark.parametrize('other, blocked', [
    ({**OWN, 'id': 'other'}, True),
    ({**OWN, 'id': 'other', 'transparency': 'transparent'}, False),
    ({**OWN, 'id': 'other', 'attendees': [{'self': True, 'responseStatus': 'declined'}]}, False),
    ({'id': 'other', 'status': 'cancelled'}, False),
    ({'id': 'other', 'start': {'date': '2026-10-02'}, 'end': {'date': '2026-10-03'}}, True),
    ({**OWN, 'id': 'other', 'start': {'dateTime': '2026-10-02T07:00:00Z'}, 'end': {'dateTime': '2026-10-02T08:00:00Z'}}, True),
    ({**OWN, 'id': 'other', 'start': {'dateTime': END}, 'end': {'dateTime': '2026-10-02T17:00:00+08:00'}}, False),
])
def test_move_blocking_semantics(other, blocked):
    svc = FakeCalendarService(items=[OWN, other])
    out = host.update_event(make_app(svc), 'own', new_start=START)
    assert out.startswith('ERROR:' if blocked else 'Updated:')
    assert bool(writes(svc)) is not blocked


def test_move_checks_later_pages_before_writing():
    svc = FakeCalendarService(items=[OWN])
    events = svc.events()
    pages = {None: {'items': [OWN], 'nextPageToken': 'more'}, 'more': {'items': [{**OWN, 'id': 'other'}]}}
    events.list = lambda **kw: _Request(pages[kw.get('pageToken')])
    svc.events = lambda: events
    assert host.update_event(make_app(svc), 'own', new_start=START).startswith('ERROR:')
    assert not writes(svc)


@pytest.mark.parametrize('page', [None, {}, {'items': None}, {'items': [{}]}, {'items': [{**OWN, 'id': 'other', 'end': {}}]}, {'items': [], 'nextPageToken': 'same'}])
def test_move_malformed_or_repeating_pages_fail_closed(page):
    svc = FakeCalendarService(items=[OWN])
    events = svc.events()
    events.list = lambda **kw: _Request(page)
    svc.events = lambda: events
    assert host.update_event(make_app(svc), 'own', new_start=START).startswith('ERROR:')
    assert not writes(svc)


@pytest.mark.parametrize('email', [None, '', 'bad address', 'a@example.com,b@example.com'])
def test_delegate_missing_email_never_silently_makes_personal_event(email):
    svc = FakeCalendarService()
    d = make_delegate(svc, email=email)
    assert delegate.book_agreed_time(d, START).startswith(('ERROR:', 'CLARIFY:'))
    assert not writes(svc)
    assert d.app.store.get_task(d.task_id).status == 'waiting'


def test_location_changes_require_new_confirmation_and_are_written():
    svc = FakeCalendarService()
    app = make_app(svc)
    first = invitation(app, location='Library')
    assert all(s in first for s in ('Library', 'Asia/Singapore', '60 min', 'Guest', 'guest@example.com'))
    app.run_no += 1
    assert invitation(app, location='https://meet.google.com/supplied', confirmed=True).startswith('CONFIRM:')
    assert not writes(svc)
    app.run_no += 1
    result = invitation(app, location='https://meet.google.com/supplied', confirmed=True)
    assert result.startswith('Created:')
    assert 'https://meet.google.com/supplied' in result
    assert writes(svc)[0][1]['body']['location'] == 'https://meet.google.com/supplied'


def test_unspecified_location_is_explicit_in_proposal():
    assert 'location not specified' in invitation(make_app()).lower()


def test_host_and_delegate_share_one_check_write_critical_section():
    """Hold host write while delegate starts: it must recheck after host commits."""
    svc = FakeCalendarService()
    events = svc.events()
    original_insert = events.insert
    host_inserting = threading.Event()
    release = threading.Event()
    delegate_started = threading.Event()

    def insert(**kw):
        req = original_insert(**kw)
        def execute():
            if kw['body']['summary'] == 'Focus':
                host_inserting.set()
                assert release.wait(3)
            svc.busy = BUSY
            return req.execute()
        return SimpleNamespace(execute=execute)

    events.insert = insert
    svc.events = lambda: events
    app = make_app(svc)
    d = make_delegate(svc)
    def book_guest():
        delegate_started.set()
        return delegate.book_agreed_time(d, START)
    with ThreadPoolExecutor(2) as pool:
        a = pool.submit(personal, app)
        assert host_inserting.wait(3)
        b = pool.submit(book_guest)
        assert delegate_started.wait(3)
        # Avoid barrier deadlock when correct serialization prevents guest's check.
        try:
            with pytest.raises(TimeoutError):
                b.result(timeout=0.1)
        finally:
            release.set()
        results = [a.result(), b.result()]
    assert len(writes(svc)) == 1
    assert sum(r.startswith(('Created:', 'Booked:')) for r in results) == 1


def test_confirmation_readback_has_year_even_when_end_is_next_day():
    app = make_app()
    out = host.create_event(app, 'Late call', '2026-10-02T23:30+08:00', '2026-10-03T00:30+08:00',
        'guest@example.com', booking_mode='invitation', attendee_name='Guest')
    assert '2026' in out and '3 Oct' in out and '60 min' in out


def test_all_day_conflict_uses_calendar_timezone_across_singapore_midnight():
    # LA Oct 1 all day ends Oct 2 15:00 Singapore; it blocks 14:30 but not 15:00.
    svc = FakeCalendarService(items=[OWN])
    events = svc.events()
    events.list = lambda **kw: _Request({'timeZone': 'America/Los_Angeles', 'items': [
        OWN, {'id': 'holiday', 'start': {'date': '2026-10-01'}, 'end': {'date': '2026-10-02'}}]})
    svc.events = lambda: events
    app = make_app(svc)
    assert host.update_event(app, 'own', new_start='2026-10-02T14:30+08:00').startswith('ERROR:')
    assert not writes(svc)
    assert host.update_event(app, 'own', new_start=START).startswith('Updated:')


def test_delegate_provider_failure_leaves_task_open_and_sends_nothing():
    svc = FakeCalendarService()
    svc.freebusy = lambda: SimpleNamespace(query=lambda **kw: _Request({'calendars': {}}))
    d = make_delegate(svc)
    assert delegate.book_agreed_time(d, START).startswith('ERROR:')
    assert not writes(svc)
    assert not d.app.whatsapp.sent
    assert d.app.store.get_task(d.task_id).status == 'waiting'


def test_move_provider_network_failure_does_not_patch():
    svc = FakeCalendarService(items=[OWN])
    events = svc.events()
    events.list = lambda **kw: _Request(exc=ConnectionError('offline'))
    svc.events = lambda: events
    assert host.update_event(make_app(svc), 'own', new_start=START).startswith('ERROR:')
    assert not writes(svc)


def test_provider_nanosecond_or_naive_event_response_fails_closed():
    svc = FakeCalendarService(items=[OWN, {**OWN, 'id': 'other', 'start': {'dateTime': '2026-10-02T15:00'}}])
    assert host.update_event(make_app(svc), 'own', new_start=START).startswith('ERROR:')
    assert not writes(svc)


def test_move_result_reports_full_effective_time_duration_and_location():
    svc = FakeCalendarService(items=[{**OWN, 'location': 'Office'}])
    out = host.update_event(make_app(svc), 'own', new_start='2026-10-02T16:30+08:00')
    assert all(s in out for s in ('2026', 'Asia/Singapore', '60 min', 'Office'))


def test_provider_timed_event_with_explicit_iana_zone_blocks_move():
    other = {'id': 'other', 'start': {'dateTime': '2026-10-02T08:00:00', 'timeZone': 'Europe/London'},
             'end': {'dateTime': '2026-10-02T09:00:00', 'timeZone': 'Europe/London'}}
    svc = FakeCalendarService(items=[OWN, other])
    periods = GoogleCalendar(svc).busy_periods_excluding(parse_dt(START), parse_dt(END), 'own')
    assert periods == [(parse_dt(START), parse_dt(END))]


@pytest.mark.parametrize('operation', ['create', 'move'])
@pytest.mark.parametrize('payload', [None, {}, {'id': 'written', 'start': {}, 'end': {}}])
def test_malformed_write_reply_reports_unknown_outcome_not_success(operation, payload):
    svc = FakeCalendarService(items=[OWN])
    events = svc.events()
    name = 'insert' if operation == 'create' else 'patch'
    def write(**kw):
        svc.calls.append((name, kw))
        return _Request(payload)
    setattr(events, name, write)
    svc.events = lambda: events
    app = make_app(svc)
    out = personal(app) if operation == 'create' else host.update_event(app, 'own', new_start=START)
    assert out.startswith('ERROR:')
    assert 'check the calendar before retrying' in out.lower()
    assert not app.completed_writes
    assert len(writes(svc)) == 1
