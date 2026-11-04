"""HTML for the landing and sign-up pages. Plain strings, no template engine; every value is escaped."""

from datetime import datetime, timezone
from html import escape
from urllib.parse import quote

CSS = """
:root { --bg:#f6f7f5; --card:#fff; --ink:#1c2321; --muted:#5b6662; --line:#e2e6e3; --accent:#128c7e;
        --accent-ink:#fff; --soft:#e7f4f1; --warn:#8a5a00; --warn-bg:#fff6e0; --bad:#a1262d; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#101413; --card:#171d1b; --ink:#e7ecea; --muted:#9aa6a1; --line:#2a3330; --accent:#25d3b8;
          --accent-ink:#06201b; --soft:#14302b; --warn:#f3c969; --warn-bg:#2c2412; --bad:#ff8a8f; }
}
* { box-sizing:border-box }
body { margin:0; background:var(--bg); color:var(--ink);
       font:16px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }
main { max-width:560px; margin:0 auto; padding:40px 20px 64px }
h1 { font-size:1.9rem; line-height:1.2; margin:0 0 12px; letter-spacing:-.01em }
h2 { font-size:1.05rem; margin:32px 0 10px }
p { margin:0 0 12px } .muted { color:var(--muted) } small { color:var(--muted) }
.card { background:var(--card); border:1px solid var(--line); border-radius:16px; padding:22px; margin:20px 0 }
.bubbles { display:grid; gap:8px; margin:18px 0 }
.bubble { background:var(--soft); border-radius:14px 14px 14px 4px; padding:9px 13px; width:fit-content; max-width:92% }
.steps { padding-left:20px; margin:0 } .steps li { margin:6px 0 }
.btn { display:inline-flex; align-items:center; gap:10px; background:var(--accent); color:var(--accent-ink);
       padding:13px 20px; border-radius:12px; font-weight:600; text-decoration:none; border:0; font-size:1rem; cursor:pointer }
.btn:focus-visible, input:focus-visible { outline:3px solid var(--accent); outline-offset:2px }
.note { background:var(--warn-bg); color:var(--warn); border-radius:12px; padding:12px 14px; font-size:.93rem }
.error { color:var(--bad); font-weight:600 }
label { display:block; font-weight:600; margin:14px 0 6px }
input { width:100%; padding:12px; border:1px solid var(--line); border-radius:10px; background:var(--bg);
        color:var(--ink); font-size:1rem }
.code { font:700 2rem/1 ui-monospace,Menlo,monospace; letter-spacing:.2em }
a { color:var(--accent); overflow-wrap:anywhere }
.btn { justify-content:center; text-align:center }
.progress { display:flex; flex-wrap:wrap; gap:8px 16px; padding:0; margin:0 0 28px; list-style:none;
            font-size:.85rem; color:var(--muted) }
.progress [aria-current="step"] { color:var(--ink); font-weight:700 }
.status-list { padding:0; list-style:none } .status-list li { padding:12px 0; border-bottom:1px solid var(--line) }
.status-list small { display:block } .actions { display:flex; flex-wrap:wrap; gap:12px; align-items:center }
code { overflow-wrap:anywhere } .code { overflow-wrap:anywhere }
@media (max-width:420px) { main { padding:24px 16px 40px } .card { padding:18px } .btn { width:100% } }
footer { margin-top:40px; font-size:.85rem; color:var(--muted) }
"""


def page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title><style>{CSS}</style></head>
<body><main>{body}
<footer>Pippa, a personal scheduling agent on WhatsApp.
· <a href="/privacy">Privacy policy</a></footer>
</main></body></html>"""


def _progress(step: int) -> str:
    labels = ("1 · Connect Google", "2 · Verify WhatsApp", "3 · Start chatting")
    items = "".join(
        f'<li aria-current="step">{label}</li>' if index == step else f"<li>{label}</li>"
        for index, label in enumerate(labels, 1)
    )
    return f'<ol class="progress" aria-label="Setup progress">{items}</ol>'


def _beta_note(auto_activate: bool, test_number_mode: bool) -> str:
    activation = (
        "Eligible invited beta testers activate automatically after WhatsApp verification."
        if auto_activate else "Beta access requires approval from Son after WhatsApp verification."
    )
    provider = (
        " Pippa currently uses a Meta test number. Son must add and verify your number in Meta's "
        "test recipients before the bot can reply. Signing in here does not complete that setup."
        if test_number_mode else ""
    )
    return f'<p class="note">{activation}{provider}</p>'


def _whatsapp_link(bot_whatsapp: str, text: str) -> str:
    return escape(f"https://wa.me/{quote(bot_whatsapp, safe='')}?text={quote(text, safe='')}")


def _join_card(code: str, bot_whatsapp: str) -> str:
    button = (
        f'<p><a class="btn" href="{_whatsapp_link(bot_whatsapp, "join " + code)}">Verify on WhatsApp</a></p>'
        f'<small>Or send <b>join {escape(code)}</b> to +{escape(bot_whatsapp)} yourself.</small>'
        if bot_whatsapp else '<p class="note">The bot number is not configured yet. Contact Son for setup.</p>'
    )
    return f"""<div class="card">
<p>Your verification code</p><p class="code">{escape(code)}</p>
<p>Send this code from the <b>same WhatsApp number</b> you entered.</p>
{button}
</div><p class="muted">The code works once and expires in 30 minutes.</p>"""


def landing(seats_left: int, auto_activate: bool = False, test_number_mode: bool = True) -> str:
    if seats_left > 0:
        cta = """<p><a class="btn" href="/signup/google">Sign in with Google</a></p>
<p><a href="/signup/google?gmail=1">Connect Calendar and optional read-only Gmail together</a></p>
<p class="muted">This is a controlled beta. Google authorization is available, but the requested permissions are awaiting verification.
Google may show an unverified-app warning. If access is blocked or a security warning appears, stop and contact Son to check your invitation and setup.
Google production verification is not complete.</p>"""
    else:
        cta = ('<p class="note"><b>Sign-ups are full</b> for this test round. Contact Son about the next opening.</p>'
               '<p><a href="/signup/google">Already connected? Sign in to resume setup</a></p>')
    return page("Pippa · Your calendar, on WhatsApp", f"""
{_progress(1)}
<h1>Pippa</h1><h2>Your calendar, on WhatsApp.</h2>
<p class="muted">Ask about your schedule, create or change meetings, and share a booking link.
Type or send a voice note. Pippa confirms meeting details before sending invitations.</p>
<div class="bubbles"><div class="bubble">What's on my calendar tomorrow?</div>
<div class="bubble">Find a time for a 30-minute meeting this week.</div></div>
<p><b>Gmail is optional.</b> Connect read-only access now or later to find invitation details when you ask.
Pippa cannot send or delete email.</p>
{cta}
<div class="card"><h2 style="margin-top:0">Three steps to get started</h2>
<ol class="steps"><li>Connect Google Calendar so Pippa can read and manage your events.</li>
<li>Enter your WhatsApp number and send your six-character code from that exact number.</li>
<li>Once beta access is active, start with <b>help</b> or ask about tomorrow.</li></ol></div>
{_beta_note(auto_activate, test_number_mode)}
<h2>Your data</h2>
<p class="muted">Pippa stores your name, email, WhatsApp number, encrypted Google credentials, recent chat
history, and information you ask it to remember. Messages, voice notes, and requested calendar or email
content are processed by OpenAI to answer you. The service operator can technically access stored data.
Read the privacy policy below before connecting. To leave, send <b>delete my data</b> to the bot.</p>
""")


def message(title: str, text: str, action: str = "") -> str:
    return page(title, f"<h1>{escape(title, quote=False)}</h1><p>{escape(text, quote=False)}</p>{action}")


def gmail_connection(expected_email: str = "") -> str:
    account = (
        f'<p>Use the <b>same Google account</b>: {escape(expected_email)}.</p>'
        if expected_email else '<p>Use the same Google account you connected for Calendar.</p>'
    )
    return page("Connect Gmail to Pippa", f"""
{_progress(1)}
<h1>Find invitation details in Gmail</h1>
<p>Gmail is optional. Connecting it lets Pippa search and read emails when you ask, including venue links in invitations.</p>
{account}
<p>Google will ask for <b>read-only Gmail access</b>. Pippa cannot send or delete email. The emails you ask it to read
are processed by OpenAI to answer you and may appear in your recent assistant chat history.</p>
<p>Your existing calendar access continues to work without Gmail.</p>
<p class="muted">Google verification of the requested permissions is not complete. If access is blocked
or Google shows a security warning, contact Son to check setup.</p>
<p><a class="btn" href="/signup/google?gmail=1">Connect Gmail with Google</a></p>
<p><a href="/signup/status">Skip for now · Back to setup</a></p>
""")


def try_again(include_gmail: bool = False) -> str:
    url = "/signup/google?gmail=1" if include_gmail else "/signup/google"
    return f'<p><a class="btn" href="{url}">Try again</a></p>'


def number_form(
    first_name: str, error: str = "", phone: str = "", calendly: str = "", *,
    csrf_token: str = "", auto_activate: bool = False, test_number_mode: bool = True,
) -> str:
    err = f'<p class="error" role="alert">{escape(error)}</p>' if error else ""
    return page("Your WhatsApp number", f"""
{_progress(2)}
<h1>Hi {escape(first_name)} 👋</h1>
<p class="muted">Next, verify the number you'll use to talk to Pippa.</p>
<form class="card" method="post" action="/signup/number">
  <input type="hidden" name="csrf_token" value="{escape(csrf_token)}">
  {err}
  <label for="phone">Your WhatsApp number</label>
  <input id="phone" name="phone" type="tel" inputmode="tel" autocomplete="tel" required
         placeholder="+65 8123 4567" value="{escape(phone)}">
  <small>Include your country code. You'll send a code from this same WhatsApp number to verify it.</small>
  <label for="calendly">Booking link <span class="muted">(optional)</span></label>
  <input id="calendly" name="calendly" type="url" placeholder="https://calendly.com/you/30min" value="{escape(calendly)}">
  <small>Shared when someone asks how to book time with you.</small>
  <p style="margin-top:18px"><button class="btn" type="submit">Continue</button></p>
</form>{_beta_note(auto_activate, test_number_mode)}""")


def done(
    first_name: str, code: str, bot_whatsapp: str, *,
    auto_activate: bool = False, test_number_mode: bool = True,
) -> str:
    return page("Verify your WhatsApp number", f"""
{_progress(2)}<h1>Almost there, {escape(first_name)}!</h1>
{_join_card(code, bot_whatsapp)}
{_beta_note(auto_activate, test_number_mode)}
<p><a href="/signup/number">Correct my WhatsApp number</a></p>
<p><a href="/signup/status">Check setup status</a></p>""")


def onboarding_status(
    user, bot_whatsapp: str, *, csrf_token: str = "", test_number_mode: bool = True,
    auto_activate: bool = False, now: datetime | None = None,
) -> str:
    calendar = getattr(user, "calendar_authorized", None)
    gmail = getattr(user, "gmail_authorized", None)
    calendar_state = "Calendar access authorized" if calendar is True else (
        "Calendar access needs checking" if calendar is None else "Calendar access not authorized"
    )
    gmail_state = "Gmail access authorized (read-only)" if gmail is True else (
        "Gmail access needs checking" if gmail is None else "Gmail not connected (optional)"
    )
    phone_verified = user.joined and bool(user.phone) and user.phone == user.typed_phone
    mismatched_join = user.joined and not phone_verified
    phone_state = "WhatsApp number verified" if phone_verified else "Waiting for WhatsApp verification"
    phone = (user.phone if user.joined else user.typed_phone) or "No number entered"
    active = user.status == "active"
    blocked = user.status in {"disabled", "rejected"}
    access_state = "Beta access active" if active else (
        f"Beta access {escape(user.status)}" if blocked else "Beta access pending"
    )
    calendar_action = '' if calendar is True else '<small><a href="/signup/google">Reconnect Calendar</a></small>'
    gmail_action = '<small><a href="/signup/gmail">Connect or reconnect Gmail · Optional</a></small>'
    if mismatched_join and not blocked:
        heading = "Check your WhatsApp number"
        next_step = (
            '<p class="note">This setup used different WhatsApp numbers. Reconnect Google to confirm your account, '
            'then enter and verify the number you want to use.</p><p><a class="btn" href="/signup/google">Reconnect Google</a></p>'
            if not active else '<p class="note">Contact Son to safely verify a change to your linked WhatsApp number.</p>'
        )
    elif active:
        heading = "Pippa is ready"
        action = (
            f'<p><a class="btn" href="{_whatsapp_link(bot_whatsapp, "help")}">Say hello on WhatsApp</a></p>'
            if bot_whatsapp else '<p class="note">Contact Son for the bot number.</p>'
        )
        next_step = f"""<div class="card"><h2 style="margin-top:0">Try your first message</h2>
<div class="bubbles"><div class="bubble">help</div><div class="bubble">What's on my calendar tomorrow?</div>
<div class="bubble">Find a time for a 30-minute meeting this week.</div></div>{action}</div>"""
    elif blocked:
        heading = "Your beta access"
        next_step = '<p class="note">Contact Son to review your beta access.</p>'
    elif not user.joined:
        heading = "Verify your WhatsApp number"
        expires = getattr(user, "join_code_expires_at", None)
        expired = False
        if expires:
            try:
                deadline = datetime.fromisoformat(expires)
                if deadline.tzinfo is None:
                    deadline = deadline.replace(tzinfo=timezone.utc)
                expired = deadline <= (now or datetime.now(timezone.utc))
            except (TypeError, ValueError):
                expired = True
        if expired:
            next_step = '<p class="note">Your verification code has expired. Enter your number again to get a new code.</p>'
        elif user.join_code:
            next_step = _join_card(user.join_code, bot_whatsapp)
        else:
            next_step = '<p class="note">Enter your WhatsApp number to receive a verification code.</p>'
        next_step += '<p><a href="/signup/number">Correct my WhatsApp number</a></p>'
    else:
        heading = "Your number is verified"
        next_step = '<p>Your verification is complete. Beta access is still pending; contact Son if you need help.</p>'
    note = _beta_note(auto_activate, test_number_mode) if not active and not blocked else ""
    if active and test_number_mode:
        note = '<p class="note">Pippa uses a Meta test number. If replies stop, contact Son to check your test-recipient setup.</p>'
    return page("Your Pippa setup", f"""
{_progress(3 if active else 2)}<h1>{heading}</h1>
<p>Hi {escape(user.first_name)} · {escape(user.email or "No Google account connected")}</p>
<ul class="status-list" aria-label="Connection status">
<li><b>{calendar_state}</b>{calendar_action}</li>
<li><b>{gmail_state}</b>{gmail_action}</li>
<li><b>{phone_state}</b><small>{escape(phone)}</small></li>
<li><b>{access_state}</b></li></ul>
{next_step}{note}
<p><a href="/signup/status">Refresh setup status</a></p>
<form method="post" action="/signup/logout">
<input type="hidden" name="csrf_token" value="{escape(csrf_token)}">
<button class="btn" type="submit">Sign out of this browser</button>
</form>""")


def home_closed() -> str:
    """The home page while sign-up is off: what the bot is, without a way in yet."""
    return page("Pippa · Your calendar, on WhatsApp", """
<h1>Pippa</h1>
<h2>Your calendar, on WhatsApp.</h2>
<p class="muted">A personal assistant you message on WhatsApp. It reads and manages your Google Calendar,
understands voice notes, and can arrange a meeting time with someone for you.</p>
<p class="note">This is a small private test. Sign-ups aren't open right now.</p>
""")


def privacy() -> str:
    return page("Privacy policy", """
<h1>Privacy policy</h1>
<p class="muted">Pippa, a personal scheduling assistant on WhatsApp. Last updated 3 October 2026.</p>
<h2>Early-access waitlist</h2>
<p>If you join the waitlist, your email address and first name are stored in Loops to personalize early-access invitations and
Pippa product updates. If you choose to answer the optional questions, we also store the scheduling tools you use,
your scheduling challenges, and what you would like Pippa to help with. We use these answers to understand what to build
and how to help you get started. Please avoid including private meeting details or other people's personal information.
Joining the waitlist does not connect your Google Calendar or WhatsApp account.
Use the unsubscribe link in waitlist emails, or contact us below to request removal. Deleting your bot
account does not automatically remove your separate waitlist subscription.</p>
<p>The signup security check uses Cloudflare Turnstile to help detect automated submissions.
Pippa uses hashed network and email identifiers for signup rate limits. These checks do not grant
access to your Google Calendar, Gmail or WhatsApp.</p>
<p>For work email addresses, Pippa checks the domain's public mail-hosting records through Google Public DNS.
Only the domain is sent for this check, not your full email address. Connecting your account still requires Google sign-in.</p>
<h2>What it collects</h2>
<p>Your name and email address from Google sign-in; your WhatsApp number; an access token for your
Google Calendar (stored encrypted); the messages and voice notes you send the bot and its replies (recent
chat history); facts you ask it to remember; and contacts you save for arranging meetings.</p>
<h2>How it uses your data</h2>
<p>Only to answer you: to read, create, change and delete events on your Google Calendar when you ask, to
arrange meetings with contacts you invite, and to remember what you told it to remember. Google Calendar data
is used for nothing else, is not used to train AI models, and the bot does not sell or share your data for
advertising.</p>
<h2>Public booking pages</h2>
<p>If a host publishes a booking page, visitors can see the host's chosen display name, meeting details
and available times. Private calendar event titles and descriptions are not shown. When a visitor confirms
a booking, Pippa processes their name and email and shares them with the host and Google Calendar
to create the event and request an invitation. Pippa stores booking details and a one-way request fingerprint
for duplicate prevention, rather than retaining the visitor's name and email in its booking database.
Public bookings do not require a visitor account, Gmail access,
or AI processing. Pippa also uses limited request information to prevent abuse and duplicate bookings.</p>
<p>Hosts can pause their page from Connections. Disconnecting Google stops new bookings. Deleting the host's
Pippa account removes its booking page and Pippa's stored booking records; it does not automatically delete
events already created in Google Calendar. Visitors can contact the address below about their booking data.
To change or cancel a meeting, contact the host; public self-service changes are not yet available.</p>
<h2>Private meeting coordination</h2>
<p>A host can prepare a meeting with two guests and share a different private availability link with each.
Pippa stores the guest names, email addresses and time selections for this request. Guests do not connect
their calendars. When both guests accept a common option and the host remains free, Pippa creates one
Google Calendar event including both guests. Participant email addresses are included in that shared invitation.</p>
<p>Treat each private link as a key: anyone it is forwarded to could respond for that guest. Links expire,
and replacing the options invalidates earlier replies. The host can cancel an unbooked request. The first
version prepares messages for the host to share; it does not automatically send scheduling outreach.
Deleting the host's Pippa account removes its stored coordination records, but not existing Google events.</p>
<h2>Scheduling chat</h2>
<p>You can paste guest availability, names and email addresses into the signed-in scheduling chat.
The conversation is stored with your Pippa account and processed by OpenAI to extract scheduling details.
Only share information you have permission to use. Pippa compares the supplied availability with your
Google Calendar and shows a proposed meeting. It creates the event and requests invitations only when
you confirm the displayed details. Host-provided availability does not mean a guest has RSVP’d.
Deleting your Pippa account removes these chats and proposals; existing Google events remain.</p>
<h2>Optional Gmail access</h2>
<p>If you connect Gmail separately, Pippa can search and read your emails only to answer your requests, for example
to find a location in an invitation. It cannot send or delete email. Retrieved email content is processed by OpenAI
and may be included in recent chat history. Gmail access is optional and can be revoked through your Google account.</p>
<h2>Who processes it</h2>
<p>In the web app, microphone recordings are sent to ElevenLabs for transcription. If you choose
to play a reply, its text is sent to ElevenLabs to generate speech. Pippa does not save raw web
recordings or generated audio. ElevenLabs processing is subject to its own retention policies;
this integration does not promise zero retention. Reviewed transcripts and conversations are
stored with your Pippa account and processed by OpenAI. Typed chat works without voice.</p>
<p>Messages travel through WhatsApp (Meta). Text and voice notes are processed by OpenAI to understand and
answer them. Calendar data comes from Google. The service and its database run on Railway. The operator
(Pippa) runs the server and could technically access stored data; he doesn't look at it.</p>
<h2>How to leave</h2>
<p>Send <b>delete my data</b> to the bot and reply <b>yes</b>: your account, saved facts, contacts, invites and
chat history are deleted and the bot's Google access is revoked. You can also remove access at any time at
<a href="https://myaccount.google.com/permissions">myaccount.google.com/permissions</a>.</p>
<h2>Contact</h2>
<p>Questions: <a href="mailto:maintainer@example.com">maintainer@example.com</a></p>
""")
