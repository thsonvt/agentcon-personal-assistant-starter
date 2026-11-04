"""The Assistant's contact and invite tools. Plain functions returning text, like assistant_tools."""

from app.assistant_tools import AppContext, tool_errors
from app.config import normalize_phone
from app.store import AmbiguousContact, OpenTaskExists
from app.timeutil import fmt_dt, parse_dt
from app.tools.whatsapp import WhatsAppError

INTRO_TEMPLATE = "loop_in_intro"


def _find(app: AppContext, name: str):
    try:
        contact = app.store.find_contact(name)
    except AmbiguousContact as exc:
        raise ValueError(f"More than one contact matches {name!r}: {exc}. Which one?") from exc
    if contact is None:
        raise ValueError(f"No saved contact called {name!r}. Ask {app.owner_name} for their WhatsApp number and save it first.")
    return contact


@tool_errors
def save_contact(app: AppContext, name: str, phone: str, email: str | None = None) -> str:
    digits = normalize_phone(phone)
    if not name.strip():
        raise ValueError("The contact needs a name.")
    if len(digits) == 8 and digits[0] in "689":
        digits = "65" + digits  # a local Singapore number typed without +65
    if len(digits) < 10:
        raise ValueError("That phone number looks too short. Include the country code, e.g. +65 8123 4567.")
    if digits == normalize_phone(app.owner_phone):
        raise ValueError(f"That's {app.owner_name}'s own number.")
    contact = app.store.add_contact(name, digits, email or None)
    return f"Saved contact: {contact.name} (+{contact.phone[:2]} …{contact.phone[-4:]}){', ' + contact.email if contact.email else ''}"


@tool_errors
def list_contacts(app: AppContext) -> str:
    contacts = app.store.list_contacts()
    if not contacts:
        return "No saved contacts yet."
    lines = []
    for c in contacts:
        task = app.store.get_open_task(c.id)
        status = f" [open invite: {task.purpose}, {task.status}]" if task else ""
        email = f" <{c.email}>" if c.email else " (no saved email)"
        lines.append(f"- {c.name} (…{c.phone[-4:]}){email}{status}")
    return "Contacts:\n" + "\n".join(lines)


@tool_errors
def start_loop_in(
    app: AppContext, contact_name: str, purpose: str, duration_min: int, window_start: str, window_end: str
) -> str:
    contact = _find(app, contact_name)
    if not purpose.strip():
        raise ValueError("Say what the meeting is, e.g. 'a 30-minute call'.")
    if not 5 <= duration_min <= 480:
        raise ValueError("Duration must be between 5 and 480 minutes.")
    start_dt, end_dt = parse_dt(window_start), parse_dt(window_end)
    now = app.now()
    start_dt = max(start_dt, now)
    if end_dt <= start_dt:
        raise ValueError("The time window must end in the future and after it starts.")
    other = app.store.find_open_invite(contact.phone)
    if other and other[0].user_id != app.store.user_id:
        # One open invite per phone across all users, or the router couldn't tell whose Delegate answers (D25).
        raise ValueError(f"{contact.name} already has an open invite from someone else. Try again later.")
    try:
        task = app.store.create_loop_in_task(contact.id, purpose.strip(), duration_min, start_dt, end_dt, now)
    except OpenTaskExists as exc:
        raise ValueError(f"{contact.name} already has an open invite. Cancel it first if you want a new one.") from exc
    first_name = contact.name.split()[0]
    try:
        params = [first_name, app.owner_name, purpose.strip()] if app.intro_with_owner_name else [first_name, purpose.strip()]
        app.whatsapp.send_template(contact.phone, app.intro_template, params)
    except WhatsAppError as exc:
        app.store.set_task_status(task.id, "cancelled", now)
        return f"ERROR: Couldn't send the WhatsApp invite to {contact.name}: {exc}"
    return (
        f"Invite sent to {contact.name} for {purpose.strip()} ({duration_min} min), "
        f"between {fmt_dt(start_dt)} and {fmt_dt(end_dt)}. I'll arrange it with them and report back."
    )


@tool_errors
def cancel_loop_in(app: AppContext, contact_name: str) -> str:
    contact = _find(app, contact_name)
    task = app.store.get_open_task(contact.id)
    if task is None:
        return f"{contact.name} has no open invite."
    app.store.set_task_status(task.id, "cancelled", app.now())
    return f"Cancelled the invite to {contact.name} ({task.purpose})."
