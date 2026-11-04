"""Chat scheduling uses synthetic participants and fake calendar writes only."""
import asyncio
import copy
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.test_group_meetings import site, NOW

TEXT = 'Call for 60 minutes Asia/Singapore. Tonya tonya@example.com: 14 October 2026 14:35 to 17:00. Joel joel@example.com: 14 October 2026 14:35 to 17:00.'

def extraction():
    return dict(title='Call',duration_min=60,timezone='Asia/Singapore',location=None,clarification=None,guests=[
        dict(name=name,email=email,windows=[dict(start='2026-10-14T14:35',end='2026-10-14T17:00',timezone='Asia/Singapore',kind='window',source=TEXT)])
        for name,email in [('Tonya','tonya@example.com'),('Joel','joel@example.com')]])

@pytest.fixture
def chat(site,monkeypatch):
    import app.chat_scheduling as module
    monkeypatch.setattr(module,'render_chat_page',lambda data:json.dumps(data))
    result=[extraction()]
    async def interpreter(messages,now):return copy.deepcopy(result[0])
    obj=module.ChatScheduling(site.obj,interpreter)
    app=FastAPI();app.include_router(obj.router);app.include_router(site.obj.router)
    client=TestClient(app,base_url='https://testserver',follow_redirects=False)
    client.cookies.set('pippa_session','session')
    return SimpleNamespace(**vars(site),chat=obj,result=result,http=client)

def prepared(chat):
    c=chat.chat.create(chat.user.id)
    return asyncio.run(chat.chat.message(chat.user.id,c['id'],c['revision'],TEXT))

def test_common_slot_requires_explicit_confirmation_and_preserves_exact_start(chat):
    c=prepared(chat)
    assert c['status']=='ready' and c['proposal']['start']=='2026-10-14T06:35:00+00:00'
    assert not chat.cal.writes
    booked=chat.chat.confirm(chat.user.id,c['id'],c['revision'])
    assert booked['status']=='booked' and len(chat.cal.writes)==1
    chat.chat.confirm(chat.user.id,c['id'],c['revision'])
    assert len(chat.cal.writes)==1
    assert chat.cal.writes[0]['attendee_emails']==['tonya@example.com','joel@example.com']

def test_partial_and_disjoint_data_ask_without_writing(chat):
    chat.result[0]['guests'][1]['email']=None
    c=prepared(chat)
    assert c['status']=='collecting' and not c['proposal']
    assert 'email' in c['messages'][-1]['content'].lower()
    chat.result[0]=extraction()
    chat.result[0]['guests'][1]['windows'][0].update(start='2026-10-15T14:35',end='2026-10-15T17:00')
    c=asyncio.run(chat.chat.message(chat.user.id,c['id'],c['revision'],TEXT))
    assert c['status']=='collecting' and 'common' in c['messages'][-1]['content'].lower()
    assert not chat.cal.writes

def test_exact_start_is_not_stretched_and_host_busy_is_checked(chat):
    chat.result[0]['guests'][0]['windows'][0].update(kind='start',end=None)
    chat.cal.busy=[(datetime(2026,10,14,6,40,tzinfo=timezone.utc),datetime(2026,10,14,7,tzinfo=timezone.utc))]
    c=prepared(chat)
    assert c['status']=='collecting' and not chat.cal.writes

@pytest.mark.parametrize('change', ['invented_email','invented_source','duplicate','host','duration','timezone','ambiguous_dst','multi_day'])
def test_untrusted_extraction_is_validated(chat,change):
    d=chat.result[0]
    if change=='invented_email':d['guests'][0]['email']='invented@example.com'
    if change=='invented_source':d['guests'][0]['windows'][0]['source']='not in the transcript'
    if change=='duplicate':d['guests'][1]['email']=d['guests'][0]['email']
    if change=='host':d['guests'][0]['email']='host@example.com'
    if change=='duration':d['duration_min']=45
    if change=='timezone':d['timezone']='invalid'
    if change=='ambiguous_dst':d['guests'][0]['windows'][0].update(start='2026-11-01T01:30',end='2026-11-01T03:30',timezone='America/New_York')
    if change=='multi_day':d['guests'][0]['windows'][0]['end']='2026-10-18T17:00'
    c=prepared(chat)
    assert c['status']=='collecting' and not c['proposal'] and not chat.cal.writes

def test_final_conflict_never_selects_a_different_slot(chat):
    c=prepared(chat);p=c['proposal']
    chat.cal.busy=[(datetime.fromisoformat(p['start']),datetime.fromisoformat(p['end']))]
    with pytest.raises(ValueError):chat.chat.confirm(chat.user.id,c['id'],c['revision'])
    assert not chat.cal.writes

def test_host_proposals_are_not_guest_votes_and_old_routes_reject(chat):
    c=prepared(chat);m=c['meeting'];raw=chat.obj._load(chat.user.id,m['id'])
    assert raw['mode']=='host_provided'
    assert all('selected' not in g and 'link' not in g for g in raw['guests'])
    assert not chat.obj.list_for_host(chat.user.id)
    assert chat.http.get('/connections/meetings/'+m['id']).status_code==404
    assert chat.http.post('/connections/meetings/'+m['id']+'/check',data={'csrf_token':'csrf'}).status_code==404
    assert chat.http.get('/m/'+chat.obj._token(raw,0)).status_code==404

def test_pending_reconciles_after_restart_without_second_write(chat):
    from app.chat_scheduling import ChatScheduling
    c=prepared(chat);chat.cal.fail_write=True
    pending=chat.chat.confirm(chat.user.id,c['id'],c['revision'])
    assert pending['status']=='pending'
    restarted=ChatScheduling(chat.obj,chat.chat.interpreter)
    assert restarted.check(chat.user.id,c['id'],pending['revision'])['status']=='booked'
    assert len(chat.cal.writes)==1

def test_correction_supersedes_late_parse_and_stale_confirmation(chat):
    async def scenario():
        c=chat.chat.create(chat.user.id);started=asyncio.Event();release=asyncio.Event();calls=[]
        async def delayed(messages,now):
            calls.append(messages)
            if len(calls)==1:started.set();await release.wait()
            result=extraction()
            if len(messages)>1:result['clarification']='What day instead?'
            return result
        chat.chat.interpreter=delayed
        old=asyncio.create_task(chat.chat.message(chat.user.id,c['id'],c['revision'],TEXT))
        await started.wait()
        current=chat.chat.view(chat.user.id,c['id'])
        fresh=await chat.chat.message(chat.user.id,c['id'],current['revision'],'Actually choose another day.')
        release.set();await old
        assert chat.chat.view(chat.user.id,c['id'])==fresh
        with pytest.raises(ValueError):chat.chat.confirm(chat.user.id,c['id'],c['revision'])
        assert not chat.cal.writes
    asyncio.run(scenario())

def test_late_interpretation_cannot_restore_deleted_user(chat):
    async def scenario():
        c=chat.chat.create(chat.user.id)
        async def deleted(messages,now):
            chat.db.conn.execute('DELETE FROM users WHERE id=?',(chat.user.id,));chat.db.conn.commit()
            return extraction()
        chat.chat.interpreter=deleted
        with pytest.raises(ValueError):await chat.chat.message(chat.user.id,c['id'],c['revision'],TEXT)
        assert not chat.db.conn.execute('SELECT * FROM scheduling_chats').fetchall()
        assert not chat.db.conn.execute('SELECT * FROM group_meetings').fetchall()
    asyncio.run(scenario())

def test_http_auth_origin_csrf_size_and_revision(chat):
    assert chat.http.post('/connections/chat',data={'csrf_token':'bad','revision':0,'message':TEXT}).status_code==403
    assert chat.http.post('/connections/chat',data={'csrf_token':'csrf','revision':0,'message':TEXT},headers={'origin':'https://evil.test'}).status_code==403
    assert chat.http.post('/connections/chat',content='x'*17000).status_code==413
    response=chat.http.post('/connections/chat',data={'csrf_token':'csrf','revision':0,'message':TEXT})
    assert response.status_code==303
    c=chat.http.get(response.headers['location']).json()['chat']
    assert chat.http.post('/connections/chat/'+c['id']+'/confirm',data={'csrf_token':'csrf','revision':999}).status_code==409
    assert not chat.cal.writes
    chat.http.cookies.clear()
    assert chat.http.get('/connections/chat/'+c['id']).headers['location']=='/signin'

def test_http_success_clarification_confirmation_and_check(chat):
    response=chat.http.post('/connections/chat',data={'csrf_token':'csrf','revision':0,'message':TEXT})
    path=response.headers['location'];c=chat.http.get(path).json()['chat']
    assert not chat.cal.writes
    chat.cal.fail_write=True
    assert chat.http.post(path+'/confirm',data={'csrf_token':'csrf','revision':c['revision']}).status_code==303
    assert chat.http.get(path).json()['chat']['status']=='pending'
    assert chat.http.post(path+'/check',data={'csrf_token':'csrf','revision':c['revision']}).status_code==303
    assert chat.http.get(path).json()['chat']['status']=='booked'
    assert len(chat.cal.writes)==1


def test_calendar_failure_is_truthful_and_does_not_create_proposal(chat):
    chat.cal.fail_read=True
    c=prepared(chat)
    assert c['status']=='collecting' and c['proposal'] is None
    assert 'could not check' in c['messages'][-1]['content']
    assert not chat.cal.writes


def test_unknown_write_not_found_remains_pending_and_blocks_overlap(chat):
    c=prepared(chat);chat.cal.fail_write=True
    p=chat.chat.confirm(chat.user.id,c['id'],c['revision']);chat.cal.events.clear()
    assert chat.chat.check(chat.user.id,c['id'],p['revision'])['status']=='pending'
    chat.chat.confirm(chat.user.id,c['id'],p['revision'])
    assert len(chat.cal.writes)==1
    with pytest.raises(ValueError):asyncio.run(chat.chat.message(chat.user.id,c['id'],p['revision'],'Move it'))
    other=prepared(chat)
    assert other['proposal']['start']==p['proposal']['end']
    # Older guest-link drafts share the same durable pending claim.
    legacy=chat.draft()
    assert datetime.fromisoformat(legacy['options'][0]['start'])>=datetime.fromisoformat(p['proposal']['end'])


def test_existing_group_and_public_pending_holds_are_respected(chat):
    m=chat.ready();chat.cal.fail_write=True
    ids=[m['options'][0]['id']];chat.vote(m,0,ids);chat.vote(m,1,ids)
    chat.db.conn.execute('CREATE TABLE public_booking_requests (user_id INTEGER,status TEXT,result TEXT)')
    chat.db.conn.execute('INSERT INTO public_booking_requests VALUES (?,?,?)',(chat.user.id,'pending',json.dumps({'start':'2026-10-14T07:30:00+00:00','end':'2026-10-14T09:00:00+00:00'})))
    chat.db.conn.commit()
    c=prepared(chat)
    assert c['status']=='collecting' and c['proposal'] is None
    assert len(chat.cal.writes)==1


def test_host_identity_changes_invalidate_chat_and_confirmation(chat):
    c=prepared(chat)
    chat.db._update(chat.user.id,NOW,google_subject='another-subject')
    with pytest.raises(ValueError,match='changed'):chat.chat.view(chat.user.id,c['id'])
    with pytest.raises(ValueError,match='changed'):chat.chat.confirm(chat.user.id,c['id'],c['revision'])
    assert not chat.cal.writes


def test_disabled_host_and_other_user_cannot_read_or_confirm(chat):
    c=prepared(chat)
    with pytest.raises(ValueError):chat.chat.view(999,c['id'])
    chat.db._update(chat.user.id,NOW,status='disabled')
    with pytest.raises(ValueError):chat.chat.confirm(chat.user.id,c['id'],c['revision'])
    assert not chat.cal.writes


def test_identity_change_during_availability_prevents_proposal(chat):
    def changed(start,end):
        chat.db._update(chat.user.id,NOW,google_subject='another')
        return []
    chat.cal.busy_periods=changed
    c=prepared(chat)
    assert c['status']=='collecting' and not c['proposal']
    assert not chat.db.conn.execute('SELECT * FROM group_meetings').fetchall()


def test_user_deletion_during_calendar_write_does_not_restore_chat(chat):
    original=chat.cal.create_event
    def deleting(*args,**kwargs):
        event=original(*args,**kwargs)
        chat.db.conn.execute('DELETE FROM users WHERE id=?',(chat.user.id,));chat.db.conn.commit()
        return event
    c=prepared(chat);chat.cal.create_event=deleting
    with pytest.raises(ValueError):chat.chat.confirm(chat.user.id,c['id'],c['revision'])
    assert not chat.db.conn.execute('SELECT * FROM scheduling_chats').fetchall()
    assert not chat.db.conn.execute('SELECT * FROM group_meetings').fetchall()
    assert len(chat.cal.events)==1


def test_parse_timeout_and_bad_output_do_not_promise_booking(chat,monkeypatch):
    import app.chat_scheduling as module
    monkeypatch.setattr(module,'PARSE_TIMEOUT',0.01)
    async def slow(messages,now):await asyncio.sleep(0.1)
    chat.chat.interpreter=slow
    c=prepared(chat)
    assert 'could not interpret' in c['messages'][-1]['content']
    assert c['status']=='collecting' and not chat.cal.writes


def test_message_and_conversation_bounds(chat):
    c=chat.chat.create(chat.user.id)
    with pytest.raises(ValueError):asyncio.run(chat.chat.message(chat.user.id,c['id'],0,'x'*4001))
    assert chat.chat.view(chat.user.id,c['id'])['revision']==0
    chat.result[0]={}
    for _ in range(15):
        c=asyncio.run(chat.chat.message(chat.user.id,c['id'],c['revision'],'hello'))
    with pytest.raises(ValueError,match='full'):asyncio.run(chat.chat.message(chat.user.id,c['id'],c['revision'],'hello'))
    assert not chat.cal.writes


def test_overnight_window_and_explicit_timezone_offsets(chat):
    for guest in chat.result[0]['guests']:
        guest['windows'][0].update(start='2026-10-14T23:35',end='2026-10-15T01:00')
    c=prepared(chat)
    assert c['proposal']['start']=='2026-10-14T15:35:00+00:00'
    # A fabricated UTC offset must not silently override the named timezone.
    for guest in chat.result[0]['guests']:
        guest['windows'][0].update(start='2026-10-14T23:35+03:00',end='2026-10-15T01:00+03:00')
    c=asyncio.run(chat.chat.message(chat.user.id,c['id'],c['revision'],TEXT))
    assert c['status']=='collecting' and not c['proposal']


def test_confirm_expired_proposal_and_no_overlap_never_write(chat):
    c=prepared(chat);chat.clock[0]+=timedelta(days=8)
    with pytest.raises(ValueError,match='expired'):chat.chat.confirm(chat.user.id,c['id'],c['revision'])
    assert not chat.cal.writes


def test_crash_after_group_write_before_chat_save_is_reconciled(chat,monkeypatch):
    c=prepared(chat);original=chat.chat._save
    def crash(*args,**kwargs):raise RuntimeError('simulated process failure')
    monkeypatch.setattr(chat.chat,'_save',crash)
    with pytest.raises(RuntimeError):chat.chat.confirm(chat.user.id,c['id'],c['revision'])
    monkeypatch.setattr(chat.chat,'_save',original)
    assert chat.chat.view(chat.user.id,c['id'])['status']=='booked'
    assert chat.chat.confirm(chat.user.id,c['id'],c['revision'])['status']=='booked'
    assert len(chat.cal.writes)==1
