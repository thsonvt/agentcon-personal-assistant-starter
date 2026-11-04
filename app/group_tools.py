"""Owner-only group request tools. Preparing or sharing a request sends nothing."""
import hashlib
import json
from urllib.parse import quote

from app.assistant_tools import _invitation_identity, tool_errors
from app.booking import BOOKING_LOCK


def _service(app):
    service = app.group_meetings
    if service is None:
        raise ValueError("Group scheduling is unavailable. Open Connections to check your account.")
    if app.store.user_id == 1:
        raise ValueError("Use your signed-in web account in Connections to prepare this group request: "
                         + service.base_url + "/connections/meetings. The legacy administrator is not a web identity.")
    return service


def _fingerprint(meeting):
    details = {key: meeting.get(key) for key in ("id", "title", "duration_min", "timezone", "location", "revision", "options")}
    details["guests"] = [{"name": g["name"], "email": g["email"]} for g in meeting["guests"]]
    return hashlib.sha256(json.dumps(details, sort_keys=True).encode()).hexdigest()


def _link(service, meeting):
    return service.base_url + "/connections/meetings/" + quote(str(meeting["id"]), safe="")


def _proposal(service, meeting):
    guests = "; ".join(f"{g['name']} <{g['email']}>" for g in meeting["guests"])
    slots = "; ".join(f"{o['start']} to {o['end']}" for o in meeting["options"])
    return (f"This meeting has not been booked and no messages have been sent. Please review: {meeting['title']}, "
            f"{meeting['duration_min']} minutes; {guests}; timezone {meeting['timezone']}; "
            f"options: {slots}; location: {meeting.get('location') or 'not specified'}. "
            f"Approve to make the private links ready to share. Both guests must choose an acceptable common time. "
            f"Review: {_link(service, meeting)} [request: {meeting['id']}]")


@tool_errors
def prepare_group_meeting(app, title, duration_min, timezone, window_start, window_end,
                          guest1_name, guest1_email, guest2_name, guest2_email, location=""):
    service = _service(app)
    identities = []
    for name, email in ((guest1_name, guest1_email), (guest2_name, guest2_email)):
        result = _invitation_identity(app, name.strip(), email.strip())
        if isinstance(result, str):
            app.booking_reply = result.removeprefix("CLARIFY: ")
            return result
        identities.append(result)
    meeting = service.create_draft(app.store.user_id, title=title, duration_min=duration_min, timezone=timezone,
        window_start=window_start, window_end=window_end, guest1_name=identities[0][0], guest1_email=identities[0][1],
        guest2_name=identities[1][0], guest2_email=identities[1][1], location=location or "")
    app.pending_groups = {meeting["id"]: (app.run_no, _fingerprint(meeting))}
    app.booking_reply = _proposal(service, meeting)
    return "CONFIRM: " + app.booking_reply


@tool_errors
def activate_group_meeting(app, meeting_id, confirmed=False):
    service = _service(app)
    with BOOKING_LOCK:
        meeting = service.host_view(app.store.user_id, meeting_id)
        if meeting["status"] != "draft":
            return group_meeting_status(app, meeting_id)
        expected = (app.run_no - 1, _fingerprint(meeting))
        if not confirmed or app.pending_groups.get(meeting_id) != expected:
            app.pending_groups = {meeting_id: (app.run_no, _fingerprint(meeting))}
            app.booking_reply = _proposal(service, meeting)
            return "CONFIRM: " + app.booking_reply
        app.pending_groups.clear()
        meeting = service.activate(app.store.user_id, meeting_id)
    app.booking_reply = ("Private invitation links are ready to share. The meeting has not been booked; "
                         "both guests still need to choose acceptable times. No messages were sent. "
                         "Copy each guest's prepared message here: " + _link(service, meeting))
    return app.booking_reply


@tool_errors
def group_meeting_status(app, meeting_id):
    service = _service(app)
    meeting = service.host_view(app.store.user_id, meeting_id)
    replies = "; ".join(f"{g['name']}: {g.get('status', 'awaiting')}" for g in meeting["guests"])
    result = f"{meeting['title']}: {meeting['status']}. {replies}. "
    if meeting["status"] == "booked":
        result += f"Scheduled: {meeting.get('selected_start', '')} ({meeting['timezone']}). Invitation delivery and RSVP are unconfirmed. "
    else:
        result += "Do not treat this request as a completed booking. "
    result += "Details and next action: " + _link(service, meeting)
    app.booking_reply = result
    return result
