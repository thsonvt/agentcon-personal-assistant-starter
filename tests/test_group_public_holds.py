import json
from tests.test_public_booking import site


def test_public_booking_excludes_uncertain_group_slot(site):
    site.publish()
    site.db.conn.execute("CREATE TABLE group_meetings (id TEXT PRIMARY KEY,user_id INTEGER,data TEXT)")
    site.db.conn.execute("INSERT INTO group_meetings VALUES (?,?,?)",("g1",site.user.id,json.dumps({
        "status":"pending","selected_start":"2026-10-01T10:00:00+08:00","selected_end":"2026-10-01T11:00:00+08:00"})))
    site.db.conn.commit()
    result=site.client.get('/booking/son/slots',params={'date':'2026-10-01','tz':'Asia/Singapore'})
    assert result.status_code==200
    assert all(not s['start'].startswith(('2026-10-01T10:00','2026-10-01T10:30')) for s in result.json()['slots'])
    response=site.book(site.start())
    assert response.status_code==409
    assert site.cal.writes==[]
