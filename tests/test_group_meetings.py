"""Three-person coordination uses fake providers; no real invitations."""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.store import Store
from app.users import Users, CALENDAR_SCOPE
from app.tools.calendar import CalendarError, Event, EventNotFoundError, GoogleCalendar

NOW = datetime(2026,10,2,tzinfo=timezone.utc)

class Calendar:
    def __init__(self):
        self.busy=[]; self.events={}; self.writes=[]; self.fail_read=False; self.fail_write=False
    def busy_periods(self,start,end):
        if self.fail_read: raise CalendarError('private')
        return self.busy
    def create_event(self,title,start,end,**kwargs):
        self.writes.append(kwargs)
        event=Event(kwargs['event_id'],title,start,end,False,source_url='https://calendar.google.com/event')
        self.events[event.id]=event
        if self.fail_write: raise CalendarError('uncertain')
        return event
    def get_event(self,event_id):
        if event_id not in self.events: raise EventNotFoundError('missing')
        return self.events[event_id]

@pytest.fixture
def site(monkeypatch):
    import app.group_meetings as module
    from app.connections import Connections
    monkeypatch.setattr(module,'render_group_page',lambda view,data:json.dumps({'view':view,**data}))
    db=Users(Store(':memory:')); clock=[NOW]
    user,_=db.sign_in_identity('host','Son','host@example.com',NOW)
    db._update(user.id,NOW,status='approved',google_token_enc='enc',google_scopes=json.dumps([CALENDAR_SCOPE]),calendar_status='connected')
    conn=Connections(None,db,None,SimpleNamespace(),'',lambda:clock[0])
    db.conn.execute('INSERT INTO connection_sessions VALUES (?,?,?,?)',(hashlib.sha256(b'session').hexdigest(),user.id,'csrf',(NOW+timedelta(days=1)).isoformat()));db.conn.commit()
    cal=Calendar(); obj=module.GroupMeetings(conn,db,lambda uid:cal,lambda:clock[0],'https://testserver','s'*32)
    app=FastAPI();app.include_router(obj.router)
    client=TestClient(app,base_url='https://testserver',follow_redirects=False);client.cookies.set('pippa_session','session')
    fields=dict(title='Call',duration_min=60,timezone='Asia/Singapore',window_start='2026-10-14T14:30',window_end='2026-10-14T17:00',guest1_name='Tonya',guest1_email='tonya@example.com',guest2_name='Joel',guest2_email='joel@example.com',location='Video')
    def draft(**changes):return obj.create_draft(user.id,**{**fields,**changes})
    def ready():return obj.activate(user.id,draft()['id'])
    def vote(meeting,index,selected):
        link=meeting['guests'][index]['link']; page=client.get(link).json()
        return client.post(link,data=dict(csrf_token=page['csrf_token'],revision=meeting['revision'],timezone='Asia/Singapore',response='submit',selected=selected))
    return SimpleNamespace(obj=obj,db=db,user=user,cal=cal,client=client,clock=clock,fields=fields,draft=draft,ready=ready,vote=vote)


def test_draft_never_books_and_guest_links_require_activation(site):
    m=site.draft();assert m['status']=='draft' and len(m['options'])==4
    assert not site.cal.writes
    assert all(not g.get('link') for g in m['guests'])


def test_two_independent_votes_book_one_event_with_both_attendees(site):
    m=site.ready(); ids=[o['id'] for o in m['options']]
    assert site.vote(m,0,ids[:2]).status_code==200
    assert not site.cal.writes
    assert site.vote(m,1,ids[1:3]).status_code==200
    result=site.obj.host_view(site.user.id,m['id'])
    assert result['status']=='booked' and result['selected_start']==m['options'][1]['start']
    assert site.cal.writes[0]['attendee_emails']==['tonya@example.com','joel@example.com']
    site.vote(m,1,ids[1:3]);assert len(site.cal.writes)==1


def test_none_and_disjoint_replies_never_book(site):
    m=site.ready();ids=[o['id'] for o in m['options']]
    site.vote(m,0,[]);site.vote(m,1,[ids[0]])
    assert site.obj.host_view(site.user.id,m['id'])['status']=='no_common'
    assert not site.cal.writes
    site.vote(m,0,[ids[1]])
    assert not site.cal.writes


def test_final_conflict_uses_only_other_mutually_accepted_option(site):
    m=site.ready();ids=[o['id'] for o in m['options']]
    site.vote(m,0,ids)
    site.cal.busy=[(datetime.fromisoformat(m['options'][0]['start']),datetime.fromisoformat(m['options'][0]['end']))]
    site.vote(m,1,ids)
    result=site.obj.host_view(site.user.id,m['id'])
    assert result['status']=='booked' and result['selected_start']==m['options'][2]['start']


def test_unknown_write_reconciles_without_second_insert(site):
    m=site.ready();ids=[m['options'][0]['id']];site.cal.fail_write=True
    site.vote(m,0,ids);site.vote(m,1,ids)
    assert site.obj.host_view(site.user.id,m['id'])['status']=='pending'
    response=site.client.post('/connections/meetings/'+m['id']+'/check',data={'csrf_token':'csrf'})
    assert response.status_code==303
    assert site.obj.host_view(site.user.id,m['id'])['status']=='booked' and len(site.cal.writes)==1


def test_unresolved_pending_cannot_revise_or_cancel(site):
    m=site.ready();ids=[m['options'][0]['id']];site.cal.fail_write=True
    site.vote(m,0,ids);site.vote(m,1,ids);site.cal.events.clear()
    for operation in (lambda:site.obj.cancel(site.user.id,m['id']),lambda:site.obj.revise(site.user.id,m['id'],timezone='Asia/Singapore',window_start='2026-10-15T14:30',window_end='2026-10-15T17:00')):
        with pytest.raises(ValueError):operation()
    assert len(site.cal.writes)==1


def test_guest_privacy_csrf_and_revision_invalidation(site):
    m=site.ready();link=m['guests'][0]['link'];page=site.client.get(link)
    assert 'joel@example.com' not in page.text and m['guests'][1]['link'] not in page.text
    assert 'tonya@example.com' not in page.text
    assert page.headers['cache-control']=='no-store'
    assert site.client.post(link,data={'csrf_token':'bad'}).status_code==403
    assert site.client.post(link,data={},headers={'Origin':'https://evil.test'}).status_code==403
    revised=site.obj.revise(site.user.id,m['id'],timezone='Asia/Singapore',window_start='2026-10-15T14:30',window_end='2026-10-15T17:00')
    assert revised['revision']==2 and revised['status']=='draft'
    assert site.client.get(link).status_code==404


@pytest.mark.parametrize('changes',[{'guest2_email':'TONYA@example.com'},{'guest1_email':'host@example.com'},{'duration_min':45},{'window_start':'2026-10-01T12:00'},{'timezone':'invalid'},{'window_start':'2026-11-01T01:30','window_end':'2026-11-01T03:30','timezone':'America/New_York'}])
def test_bad_inputs_rejected(site,changes):
    with pytest.raises(ValueError):site.draft(**changes)


def test_host_isolation_disabled_and_expiration(site):
    m=site.ready()
    with pytest.raises(ValueError):site.obj.host_view(999,m['id'])
    site.db._update(site.user.id,NOW,status='disabled')
    assert site.client.get(m['guests'][0]['link']).status_code==404
    site.db._update(site.user.id,NOW,status='approved')
    site.clock[0]+=timedelta(days=8)
    assert site.client.get(m['guests'][0]['link']).status_code==410
    assert site.obj.host_view(site.user.id,m['id'])['status']=='expired'


def test_calendar_adapter_sends_both_guests():
    service=MagicMock();start=NOW+timedelta(days=1);end=start+timedelta(hours=1)
    service.events.return_value.insert.return_value.execute.return_value={'id':'id','summary':'Call','start':{'dateTime':start.isoformat()},'end':{'dateTime':end.isoformat()}}
    GoogleCalendar(service).create_event('Call',start,end,attendee_emails=['a@example.com','b@example.com'])
    args=service.events.return_value.insert.call_args.kwargs
    assert args['body']['attendees']==[{'email':'a@example.com'},{'email':'b@example.com'}] and args['sendUpdates']=='all'


def test_guest_none_button_clears_previous_selection(site):
    m=site.ready();site.vote(m,0,[m['options'][0]['id']])
    link=m['guests'][0]['link'];page=site.client.get(link).json()
    assert site.client.post(link,data=dict(csrf_token=page['csrf_token'],revision=m['revision'],timezone='Asia/Singapore',response='submit',none='1',selected=[m['options'][0]['id']])).status_code==200
    assert site.obj.host_view(site.user.id,m['id'])['guests'][0]['selected']==[]


def test_host_form_auth_csrf_and_oversize(site):
    assert site.client.post('/connections/meetings',data={**site.fields,'csrf_token':'bad'}).status_code==403
    assert site.client.post('/connections/meetings',data={**site.fields,'csrf_token':'csrf'}).status_code==303
    assert site.client.post('/connections/meetings',content='x'*17000).status_code==413
    site.client.cookies.clear()
    assert site.client.get('/connections/meetings').headers['location']=='/signin'


def test_unrelated_guest_csrf_and_invalid_selections_cannot_vote(site):
    m=site.ready();a=site.client.get(m['guests'][0]['link']).json();b=site.client.get(m['guests'][1]['link']).json()
    values=dict(csrf_token=a['csrf_token'],revision=1,timezone='Asia/Singapore',response='submit',selected='1')
    assert site.client.post(m['guests'][1]['link'],data=values).status_code==403
    assert site.client.post(m['guests'][1]['link'],data={**values,'csrf_token':'☃'}).status_code==403
    assert site.client.post(m['guests'][1]['link'],data={**values,'csrf_token':b['csrf_token'],'selected':'100'}).status_code==400
    assert site.obj.host_view(site.user.id,m['id'])['guests'][1]['status']=='awaiting'


def test_calendar_failure_and_all_common_conflicts_need_help(site):
    m=site.ready();ids=[m['options'][0]['id']]
    site.vote(m,0,ids);site.cal.fail_read=True;site.vote(m,1,ids)
    assert site.obj.host_view(site.user.id,m['id'])['status']=='needs_help' and not site.cal.writes
    site.cal.fail_read=False
    site.cal.busy=[(datetime.fromisoformat(m['options'][0]['start']),datetime.fromisoformat(m['options'][0]['end']))]
    assert site.obj.check(site.user.id,m['id'])['status']=='needs_help'
    assert not site.cal.writes


def test_cancel_invalidates_links_and_delete_user_removes_private_data(site):
    m=site.ready();assert site.obj.cancel(site.user.id,m['id'])['status']=='cancelled'
    assert site.client.get(m['guests'][0]['link']).status_code==404
    site.db.conn.execute('DELETE FROM users WHERE id=?',(site.user.id,));site.db.conn.commit()
    assert not site.db.conn.execute('SELECT * FROM group_meetings').fetchall()


def test_malformed_calendar_availability_blocks_draft(site):
    site.cal.busy=None
    with pytest.raises(CalendarError):site.draft()


def test_naive_dst_gap_rejected_and_timezone_equivalence(site):
    from app.group_meetings import _when,_zone
    with pytest.raises(ValueError):_when('2026-03-08T02:30',_zone('America/New_York'))
    m=site.draft(timezone='Asia/Riyadh',window_start='2026-10-14T09:30',window_end='2026-10-14T12:00')
    assert m['options'][0]['start']=='2026-10-14T06:30:00+00:00'


def test_guest_submitted_timezone_preserved_and_host_input_errors_keep_form(site):
    m=site.ready();link=m['guests'][0]['link'];page=site.client.get(link).json()
    response=site.client.post(link,data=dict(csrf_token=page['csrf_token'],revision=1,timezone='Asia/Riyadh',response='submit',selected='1'))
    assert response.json()['timezone']=='Asia/Riyadh'
    invalid=site.client.post('/connections/meetings',data={**site.fields,'csrf_token':'csrf','guest2_email':'bad'})
    assert invalid.status_code==400 and invalid.json()['view']=='list'
    assert invalid.json()['values']['guest1_name']=='Tonya'


def test_restart_pending_reconciliation_and_host_isolation(site):
    from app.group_meetings import GroupMeetings
    m=site.ready();site.cal.fail_write=True;ids=[m['options'][0]['id']]
    site.vote(m,0,ids);site.vote(m,1,ids)
    restarted=GroupMeetings(site.obj.connections,site.db,lambda uid:site.cal,lambda:site.clock[0],'https://testserver','s'*32)
    with pytest.raises(ValueError):restarted.check(999,m['id'])
    assert restarted.check(site.user.id,m['id'])['status']=='booked'
    assert len(site.cal.writes)==1


def test_new_request_respects_pending_group_and_public_holds(site):
    m=site.ready();site.cal.fail_write=True;ids=[m['options'][0]['id']]
    site.vote(m,0,ids);site.vote(m,1,ids)
    fresh=site.draft()
    assert fresh['options'][0]['start']==m['options'][2]['start']
    site.db.conn.execute('CREATE TABLE public_booking_requests (user_id INTEGER,status TEXT,result TEXT)')
    site.db.conn.execute('INSERT INTO public_booking_requests VALUES (?,?,?)',(site.user.id,'pending',json.dumps({'start':m['options'][2]['start'],'end':m['options'][3]['end']})))
    site.db.conn.commit()
    with pytest.raises(ValueError,match='No free times'):site.draft()


def test_stale_revision_and_expired_form_do_not_mutate_votes(site):
    m=site.ready();link=m['guests'][0]['link'];p=site.client.get(link).json()
    body=dict(csrf_token=p['csrf_token'],revision=99,timezone='Asia/Singapore',response='submit',selected='1')
    assert site.client.post(link,data=body).status_code==409
    assert site.obj.host_view(site.user.id,m['id'])['guests'][0]['status']=='awaiting'
    site.clock[0]+=timedelta(days=8)
    assert site.client.post(link,data={**body,'revision':1}).status_code==410
    assert not site.cal.writes


def test_wrong_provider_confirmation_stays_pending(site):
    original=site.cal.create_event
    def wrong(*args,**kwargs):
        event=original(*args,**kwargs)
        return Event(event.id,event.title,event.start+timedelta(minutes=30),event.end+timedelta(minutes=30),False)
    site.cal.create_event=wrong
    m=site.ready();ids=[m['options'][0]['id']];site.vote(m,0,ids);site.vote(m,1,ids)
    assert site.obj.host_view(site.user.id,m['id'])['status']=='pending'


@pytest.mark.parametrize('start,end',[
    ('2026-11-01T01:00:00-05:00','2026-11-01T03:00:00-05:00'),
    ('2026-11-01T01:15:00-05:00','2026-11-01T03:00:00-05:00'),
    ('2026-11-01T01:30:00-04:00','2026-11-01T03:00:00-05:00'),
])
def test_dst_fold_candidates_stay_within_explicit_checked_window(site,start,end):
    site.clock[0]=datetime(2026,10,20,tzinfo=timezone.utc)
    m=site.draft(timezone='America/New_York',window_start=start,window_end=end)
    lower,upper=datetime.fromisoformat(start),datetime.fromisoformat(end)
    assert all(lower<=datetime.fromisoformat(o['start']) and datetime.fromisoformat(o['end'])<=upper for o in m['options'])
    assert len({o['start'] for o in m['options']})==len(m['options'])


@pytest.mark.parametrize('pending',[False,True])
def test_expired_terminal_guest_links_reveal_no_details_but_host_can_reconcile(site,pending):
    m=site.ready();link=m['guests'][0]['link'];page=site.client.get(link).json()
    site.cal.fail_write=pending
    ids=[m['options'][0]['id']];site.vote(m,0,ids);site.vote(m,1,ids)
    expected='pending' if pending else 'booked'
    assert site.obj.host_view(site.user.id,m['id'])['status']==expected
    site.clock[0]+=timedelta(days=8)
    response=site.client.get(link)
    assert response.status_code==410
    assert 'Tonya' not in response.text and 'Call' not in response.text and m['id'] not in response.text
    response=site.client.post(link,data=dict(csrf_token=page['csrf_token'],revision=1,timezone='Asia/Singapore',response='submit',selected='1'))
    assert response.status_code==410
    assert site.obj.host_view(site.user.id,m['id'])['status']==expected
    assert not site.obj.host_view(site.user.id,m['id'])['guests'][0]['link']
    assert site.obj.check(site.user.id,m['id'])['status']=='booked'
    assert len(site.cal.writes)==1


def test_inflight_booking_result_cannot_resurrect_deleted_user_meeting(site):
    original=site.cal.create_event
    def delete_owner_before_response(*args,**kwargs):
        event=original(*args,**kwargs)
        site.db.conn.execute('DELETE FROM users WHERE id=?',(site.user.id,))
        site.db.conn.commit()
        return event
    site.cal.create_event=delete_owner_before_response
    m=site.ready();ids=[m['options'][0]['id']]
    site.vote(m,0,ids);site.vote(m,1,ids)
    assert not site.db.conn.execute('SELECT * FROM group_meetings').fetchall()
    assert len(site.cal.events)==1  # Local deletion makes no promise about Google's event.
