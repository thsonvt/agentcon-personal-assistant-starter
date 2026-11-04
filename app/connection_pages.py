"""Server-rendered Pippa Connections pages with bounded onboarding status refresh."""

from html import escape
import re
from urllib.parse import quote, urlsplit

from app.users import SignupError, canonical_signup_phone


CSS = """
:root { color-scheme:light dark; --bg:#faf7f1; --surface:#fffdf9; --ink:#392f29; --muted:#71675e;
 --line:#e4dccf; --soft:#f0e9de; --clay:#a14f32; --on-clay:#fffaf4; --good:#4a6346; --bad:#91432f; }
@media (prefers-color-scheme: dark) { :root { --bg:#221f1b; --surface:#2d2924; --ink:#f4ede4;
 --muted:#bdb1a5; --line:#494137; --soft:#373128; --clay:#efaf87; --on-clay:#32251b; --good:#abc39f; --bad:#efaa91; } }
* { box-sizing:border-box } body { margin:0; background:var(--bg); color:var(--ink); font:14px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif }
a { color:var(--clay); overflow-wrap:anywhere } button,input { font:inherit } button,a { -webkit-tap-highlight-color:transparent }
a:focus-visible,button:focus-visible,input:focus-visible { outline:3px solid var(--clay); outline-offset:4px }
.skip { position:absolute; left:16px; top:-100px; z-index:1; background:var(--surface); padding:12px }.skip:focus { top:12px }
.app { max-width:1100px; margin:24px auto; border:1px solid var(--line); border-radius:18px; overflow:hidden; }
.top { padding:22px 30px; display:flex; align-items:center; justify-content:space-between; gap:20px; border-bottom:1px solid var(--line) }
.brand { font:30px/1 Georgia,serif; letter-spacing:-1.5px; display:flex; gap:9px; align-items:center; color:var(--ink); text-decoration:none }
.brand-dot { width:11px; height:11px; border-radius:50%; background:var(--clay) }.meta { color:var(--muted); font-size:12px; overflow-wrap:anywhere; text-align:right }
.shell { display:grid; grid-template-columns:200px minmax(0,1fr); min-height:600px }.side { border-right:1px solid var(--line); padding:34px 24px }
.side-label,.kicker { font-size:12px; letter-spacing:.08em; text-transform:uppercase; color:var(--muted); margin:0 0 22px }.kicker { color:var(--clay); margin-bottom:12px }
.steps { list-style:none; margin:0; padding:0; display:grid; gap:23px }.step { display:flex; align-items:center; gap:11px; color:var(--muted); font-size:13px }
.step-number { display:grid; place-items:center; flex:0 0 27px; height:27px; border:1px solid var(--line); border-radius:50%; font-size:12px }
.step.current { color:var(--ink); font-weight:600 }.step.current .step-number { background:var(--clay); border-color:var(--clay); color:var(--on-clay) }
.step.done .step-number { color:var(--good); background:var(--soft); border-color:var(--soft) }.side-note { margin-top:42px; font-size:12px; color:var(--muted) }
.main { padding:36px; min-width:0 } h1 { font:400 32px/1.2 Georgia,serif; letter-spacing:-.6px; margin:0 0 14px; max-width:560px }
h2 { font-size:16px; font-weight:600; margin:0 } h3 { font-size:14px; font-weight:600; margin:0 0 4px } p { margin:0 0 18px }
.lead { color:var(--muted); max-width:560px }.panel { background:var(--surface); border:1px solid var(--line); border-radius:16px; padding:23px; margin:24px 0 }
.capability { display:flex; gap:13px; align-items:flex-start; margin-bottom:19px }.capability:last-child { margin-bottom:0 }
.icon { display:grid; place-items:center; width:35px; height:35px; flex:0 0 35px; border-radius:10px; background:var(--soft); color:var(--clay) }.icon svg { width:18px; height:18px }
.caption { color:var(--muted); font-size:12px; margin:3px 0 0 }.spaced { margin-top:14px }.actions { display:flex; align-items:center; flex-wrap:wrap; gap:12px; margin-top:25px }
.btn { display:inline-flex; justify-content:center; align-items:center; min-height:44px; border:1px solid var(--line); border-radius:10px; padding:10px 15px; background:var(--surface); color:var(--ink); font-weight:500; text-decoration:none; cursor:pointer; text-align:center }
.btn:hover { background:var(--soft) }.primary { background:var(--clay); border-color:var(--clay); color:var(--on-clay) }.primary:hover { background:var(--clay); filter:brightness(.95) }
.link-button { border:0; background:transparent; color:var(--muted); text-decoration:underline; padding:10px 0; cursor:pointer; min-height:44px }
.section-title,.heading { display:flex; justify-content:space-between; align-items:center; gap:12px; flex-wrap:wrap; margin-bottom:15px }.heading { align-items:flex-start; margin:0 }
.account { color:var(--muted); font-size:13px; overflow-wrap:anywhere }.service { display:grid; grid-template-columns:35px minmax(0,1fr) auto; align-items:center; gap:12px; border-top:1px solid var(--line); padding:19px 0 }.service:last-child { padding-bottom:0 }
.status { font-size:12px; color:var(--muted) }.status.good { color:var(--good) }.status.warn,.error { color:var(--bad) }.pill { background:var(--soft); color:var(--muted); font-size:12px; border-radius:6px; padding:4px 8px }
.message { background:var(--soft); border-radius:12px; padding:14px 16px; margin:16px 0; font-size:13px; overflow-wrap:anywhere }.error { border-left:3px solid var(--bad) }
label { display:block; font-size:13px; margin:16px 0 7px; font-weight:500 } input { width:100%; min-width:0; border:1px solid var(--line); border-radius:9px; padding:11px 12px; font-size:16px; background:var(--bg); color:var(--ink) }
.code { display:block; background:var(--soft); padding:14px 18px; border-radius:10px; font-size:20px; letter-spacing:.08em; margin:18px 0; text-align:center; overflow-wrap:anywhere }
.footer { border-top:1px solid var(--line); padding:14px 30px; display:flex; justify-content:space-between; flex-wrap:wrap; gap:12px; font-size:12px; color:var(--muted) }.footer a { color:var(--muted) } form { margin:0 }.inline-form { display:inline-flex }.tight { margin-top:0 }
@media(max-width:760px) { .app { margin:0; border:0; border-radius:0 }.shell { grid-template-columns:1fr; min-height:0 }.side { padding:17px 22px; border-right:0; border-bottom:1px solid var(--line) }.side-label,.side-note { display:none }.steps { display:flex; justify-content:space-between; gap:9px }.step { flex-direction:column; gap:6px; font-size:11px; text-align:center }.step-number { flex-basis:auto; width:27px }.main { padding:26px 22px }.top { padding:20px 22px } h1 { font-size:29px }.panel { padding:18px }.footer { padding:14px 22px } }
@media(max-width:430px) { .service { grid-template-columns:30px minmax(0,1fr) }.service>.btn { grid-column:2; justify-self:start }.main { padding:25px 16px }.meta { max-width:190px }.actions { gap:9px }.section-title { align-items:flex-start }.heading { display:block }.heading .pill { display:inline-block; margin-bottom:14px } }
"""

STATUSES = {
    "not_connected": ("Not connected", ""),
    "connected": ("Connected", "good"),
    "needs_attention": ("Needs attention", "warn"),
    "checking": ("Checking", ""),
    "unknown": ("Not checked", ""),
}
ICON_PATHS = {
    "calendar": '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M16 3v4M8 3v4M3 11h18m-13 5h3"/>',
    "gmail": '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3 6 9 7 9-7"/>',
    "whatsapp": '<path d="M21 11.5a9 9 0 0 1-9 9 9 9 0 0 1-4-.9L3 21l1.4-4.9a9 9 0 1 1 16.6-4.6Z"/>',
    "shield": '<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Zm-4 9 3 3 5-6"/>',
}


def _text(value) -> str:
    return escape(str(value if value is not None else ""), quote=True)


def _link(label: str, url: str, primary: bool = False) -> str:
    return f'<a class="btn{" primary" if primary else ""}" href="{_text(url)}">{_text(label)}</a>'


def _icon(name: str) -> str:
    return f'<span class="icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">{ICON_PATHS[name]}</svg></span>'


def _capability(icon: str, title: str, detail: str) -> str:
    return f'<div class="capability">{_icon(icon)}<div><h3>{title}</h3><p class="caption">{detail}</p></div></div>'


def _form(data: dict, action: str, content: str, inline: bool = False) -> str:
    return (f'<form method="post" action="/connections/{action}" class="{"inline-form" if inline else "form"}">'
            f'<input type="hidden" name="csrf_token" value="{_text(data.get("csrf_token"))}">{content}</form>')


def _status(data: dict, service: str) -> str:
    value = data.get(service + "_status", "not_connected")
    return value if value in STATUSES else "unknown"


def _status_label(data: dict, service: str) -> str:
    label, style = STATUSES[_status(data, service)]
    return f'<span class="status {style}">{label}</span>'


def _whatsapp_url(data: dict, text: str) -> str:
    number = str(data.get("bot_whatsapp") or "").removeprefix("+")
    if not re.fullmatch(r"[1-9][0-9]{6,14}", number):
        return ""
    return f'https://wa.me/{number}?text={quote(text, safe="")}'


def _booking_url(value) -> str:
    """Never turn an untrusted stored URL into an executable or unrelated link."""
    url = str(value or "")
    try:
        parsed = urlsplit(url)
        if (parsed.scheme == "https" and parsed.hostname == "calendly.com" and not parsed.username
                and not parsed.password and not parsed.port and parsed.path.strip("/")
                and not any(char.isspace() or ord(char) < 32 for char in url) and "\\" not in url):
            return url
    except ValueError:
        pass
    return ""


def _beta_note(data: dict) -> str:
    if data.get("beta_approved"):
        text = "Beta access is approved. Finish any remaining connection steps to activate Pippa."
    elif data.get("auto_activate"):
        text = "Eligible invited beta testers activate automatically after Calendar setup and WhatsApp verification."
    else:
        text = "Beta access requires approval from Son after WhatsApp verification."
    if data.get("test_number_mode", True):
        text += (" Pippa currently uses a Meta test number. Son must add and verify your number in Meta’s test recipients "
                 "before the bot can reply. Linking your number here does not complete that setup.")
    return f'<p class="message">{text}</p>'


def _google_warning() -> str:
    return ('<p class="caption spaced">Google verification of the requested permissions is not complete. '
            'Google may show an unverified-app warning. If access is blocked or a security warning appears, '
            'stop and contact Son to check your invitation and setup.</p>')


def _check(data: dict) -> str:
    return _form(data, "check", '<button class="btn" type="submit">Check connections</button>', True)


def _whatsapp_state(data: dict) -> tuple[str, str]:
    if data.get("active") and data.get("joined"):
        onboarding = data.get("onboarding") or {}
        if onboarding.get("reply_status") == "failed":
            blocked = str(onboarding.get("reply_error_code")) == "131030"
            uncertain = onboarding.get("reply_error_code") is None
            return ("Replies blocked" if blocked else "Reply not confirmed" if uncertain else "Reply failed",
                    "Your number is verified and beta access is approved. "
                    + ("Son needs to complete Meta test-recipient setup before Pippa can reply."
                       if blocked else "The latest reply could not be confirmed. See the delivery details below."
                       if uncertain else "The latest reply failed. See the delivery details below."))
        return "Active", "Your number is linked and beta access is active. Reply delivery is tracked separately below."
    if data.get("approval_pending") and not data.get("auto_activate"):
        return "Waiting for beta approval", ("Your number is linked. " if data.get("joined") else "") + "Son will approve your beta access. Complete any remaining connection steps below."
    if data.get("beta_approved"):
        if _status(data, "calendar") != "connected" or data.get("calendar_ready") is False:
            next_step = "then refresh Connections." if data.get("joined") else "then verify your WhatsApp number with its country code."
            return "Beta access approved", "Connect and check Google Calendar, " + next_step
        if not data.get("joined"):
            return "Beta access approved", "Verify your WhatsApp number with its country code using a new join code."
        return "Beta access approved", "Your number is linked. Refresh Connections to check activation."
    if data.get("joined"):
        return "Setup in progress", "Your number is linked. Check your Calendar connection to finish setup."
    return "Not linked", "Link your number with a private one-time join code."


def _first_message(data: dict) -> str:
    if not data.get("active") or not data.get("joined"):
        return ""
    onboarding = data.get("onboarding") or {}
    if onboarding.get("reply_status") == "failed" and str(onboarding.get("reply_error_code")) == "131030":
        return ""
    if _status(data, "calendar") != "connected":
        return '<div class="actions">' + _link("Restore Calendar access →", "/connections/calendar", True) + '</div>'
    url = _whatsapp_url(data, "What does my calendar look like tomorrow?")
    if not url:
        return '<p class="message">The bot number is not configured. Contact Son for setup.</p>'
    return ('<div class="message">Try: “What does my calendar look like tomorrow?”</div><div class="actions">'
            + _link("Talk to Pippa →", url, True) + '</div><p class="caption spaced">WhatsApp opens with your request ready. Send it when you’re ready.</p>')


def _welcome(data: dict) -> str:
    full = '<p class="message">This beta round is full. Existing users can still sign in to manage their connections.</p>' if data.get("seats_left") == 0 else ""
    return ('<p class="kicker">Meet your everyday assistant</p><h1>Less juggling.<br>More room in your day.</h1>'
            '<p class="lead">Connect your calendar, then talk to Pippa on WhatsApp. Add email help whenever you’re ready.</p>'
            '<div class="panel">' + _capability("calendar", "A little help with your schedule", "Find time, check your plans, and arrange meetings.")
            + _capability("gmail", "Email context, if you want it", "Optional read-only Gmail. You stay in control.")
            + _capability("whatsapp", "Right where you already chat", "No new inbox to keep up with.") + '</div>' + full
            + '<div class="actions">' + _link("Continue with Google →", "/signin/google", True) + '</div>'
            '<p class="caption spaced">Signing in identifies your Pippa account. It does not grant Calendar or Gmail access. '
            'Returning users go straight to Connections.</p>' + _beta_note(data))


def _service_page(data: dict, service: str) -> str:
    calendar = service == "calendar"
    title = "Make space for what matters." if calendar else "A little context from your inbox."
    lead = "Let Pippa help with your Google Calendar." if calendar else "Connect Gmail so Pippa can find the email details you ask about."
    capabilities = (_capability("calendar", "See your availability", "Read events and check for open time.")
                    + _capability("calendar", "Help arrange your plans", "Create and update events when you ask.")) if calendar else (
                    _capability("gmail", "Find messages and useful details", "Search and read email when you ask for help.")
                    + _capability("shield", "Read-only access", "Pippa cannot send, delete, or change email with this permission."))
    connected = _status(data, service) == "connected"
    detail = data.get(service + "_detail")
    action = _link("Continue →", "/connections/gmail" if calendar else "/connections/whatsapp", True) if connected else _link(
        ("Reconnect" if _status(data, service) == "needs_attention" else "Connect") + (" Calendar →" if calendar else " Gmail →"),
        f"/connections/google?service={service}", True)
    second = _link("Back to Connections", "/connections") if calendar else _link("Skip for now", "/connections/whatsapp")
    return (f'<p class="kicker">{"Step 2 of 4" if calendar else "Step 3 of 4 · optional"}</p><h1>{title}</h1><p class="lead">{lead}</p>'
            f'<p class="account">Google account: {_text(data.get("email"))}. Use this same account when connecting.</p>'
            f'<div class="panel">{capabilities}</div>{_status_label(data, service)}'
            + (f'<p class="caption spaced">{_text(detail)}</p>' if detail else "")
            + f'<div class="actions">{action}{second}</div>'
            + '<p class="caption spaced">' + ("This does not grant access to Gmail, Drive, or Docs." if calendar else
            "Gmail is optional. Calendar works without it. You can add Gmail later from Connections. Emails you ask Pippa to read are processed by OpenAI and may appear in your recent assistant chat history.") + '</p>'
            + _google_warning() + f'<div class="actions">{_check(data)}</div>')


def _onboarding_feedback(data: dict) -> str:
    onboarding = data.get("onboarding") or {}
    attempt = onboarding.get("attempt_status", "waiting")
    attempts = {
        "waiting": "Waiting for your join message. Opening WhatsApp does not send it.",
        "number_mismatch": "Pippa received your join message from a different WhatsApp number. Check the country code and number below, then send the current code from that exact number.",
        "code_expired": "Pippa received your message, but the join code has expired. Get a new code below and send it from the same number.",
        "verified": "WhatsApp number verified. Pippa received your join message and confirmed its sender.",
    }
    reply = onboarding.get("reply_status", "not_sent")
    replies = {
        "not_sent": "Reply has not been sent.",
        "accepted": "Meta accepted the reply; delivery is not confirmed.",
        "delivered": "Reply delivered to WhatsApp.",
        "read": "Reply read in WhatsApp.",
        "failed": "Reply could not be delivered. Contact Son to check WhatsApp setup.",
    }
    reply_text = replies.get(reply, "Reply delivery is not confirmed.")
    if reply == "failed" and onboarding.get("reply_error_code") is None:
        reply_text = "Pippa could not confirm the reply; delivery is unknown. Send status in WhatsApp to check again."
    if reply == "failed" and str(onboarding.get("reply_error_code")) == "131030":
        reply_text = "Pippa received your message, but Meta has not enabled replies to this number. Son needs to complete Meta test-recipient setup."
    html = f'<div class="message" role="status"><p>{attempts.get(attempt, "WhatsApp verification is not confirmed.")}</p><p>{reply_text}</p>'
    if onboarding.get("attempted_at"):
        html += f'<p class="caption">Last join message: {_text(onboarding["attempted_at"])}</p>'
    if reply == "failed" and attempt == "verified" and data.get("joined"):
        instruction = ("After Son completes Meta setup, send status to Pippa to retry the reply check."
                       if str(onboarding.get("reply_error_code")) == "131030" else
                       "Send status to Pippa to retry the reply check.")
        html += f'<p>{instruction}</p>'
        retry_url = _whatsapp_url(data, "status")
        if retry_url:
            html += '<div class="actions">' + _link("Retry WhatsApp check", retry_url) + '</div>'
            html += '<p class="caption spaced">WhatsApp opens with status ready. Send it yourself from your verified number. Opening the chat does not confirm reply delivery.</p>'
    return html + '</div>'


def _onboarding_poll(data: dict) -> str:
    # The endpoint exposes an opaque revision, never phone numbers or join codes.
    revision = data.get("onboarding_revision")
    if revision is None:
        return ""
    return f'<span id="onboarding-status" data-revision="{_text(revision)}" hidden></span>' + """<script>
(() => {
  const initial = document.getElementById("onboarding-status").dataset.revision;
  let deadline = Date.now() + 120000;
  let edited = false, stopped = false, inFlight = false, resumePending = false;
  let timer = null, epoch = 0;
  const available = () => document.visibilityState === "visible" && navigator.onLine;
  let wasAvailable = available();
  const canPoll = () => !edited && !stopped && available() && Date.now() < deadline;
  function clearTimer() {
    if (timer !== null) window.clearTimeout(timer);
    timer = null;
  }
  function schedule(delay) {
    clearTimer();
    if (canPoll()) timer = window.setTimeout(poll, delay);
  }
  function preserveEdits() {
    edited = true;
    resumePending = false;
    epoch++;
    clearTimer();
  }
  document.querySelectorAll('form[action="/connections/whatsapp"] input').forEach(input => {
    input.addEventListener("input", preserveEdits);
    input.addEventListener("change", preserveEdits);
  });
  async function poll() {
    clearTimer();
    if (!canPoll() || inFlight) return;
    inFlight = true;
    resumePending = false;
    const requestEpoch = epoch;
    try {
      const response = await fetch("/connections/whatsapp/status", {credentials: "same-origin", cache: "no-store", signal: AbortSignal.timeout(5000)});
      if (requestEpoch !== epoch || !canPoll()) return;
      if (!response.ok) { stopped = true; return; }
      const status = await response.json();
      if (requestEpoch === epoch && canPoll() && typeof status.revision === "string" && status.revision !== initial) {
        stopped = true;
        window.location.reload();
      }
    } catch (_) { /* Manual Refresh status remains available. */ }
    finally {
      inFlight = false;
      schedule(resumePending ? 0 : 8000);
    }
  }
  function availabilityChanged() {
    const nowAvailable = available();
    if (nowAvailable === wasAvailable) return;
    wasAvailable = nowAvailable;
    epoch++;
    clearTimer();
    if (!nowAvailable || edited || stopped) return;
    // Returning from WhatsApp gets a fresh, bounded window and an immediate check.
    deadline = Date.now() + 120000;
    if (inFlight) resumePending = true;
    else poll();
  }
  document.addEventListener("visibilitychange", availabilityChanged);
  window.addEventListener("online", availabilityChanged);
  window.addEventListener("offline", availabilityChanged);
  schedule(8000);
})();
</script>"""


def _whatsapp(data: dict) -> str:
    status, detail = _whatsapp_state(data)
    html = ('<p class="kicker">Step 4 of 4</p><h1>Say hello on WhatsApp.</h1>'
            '<p class="lead">Link your chat to the account you just set up.</p>'
            f'<div class="panel"><h2>{status}</h2><p class="caption">{detail}</p>')
    if data.get("onboarding"):
        html += _onboarding_feedback(data)
    if data.get("joined"):
        html += f'<p class="account spaced">{_text(data.get("phone"))}</p>' + _first_message(data)
    if data.get("calendar_ready") is False and not data.get("active"):
        html += '<div class="actions">' + _link("Connect or reconnect Calendar →", "/connections/calendar", True) + '</div>'
    elif not data.get("joined"):
        saved_phone = str(data.get("phone") or "")
        try:
            canonical = canonical_signup_phone(saved_phone) if saved_phone else ""
        except SignupError:
            canonical = ""
        # Preserve legacy local input for correction; valid saved numbers use explicit + form.
        phone_value = data.get("phone_input", "+" + canonical if canonical else saved_phone)
        country_code = data.get("country_code", "+65")
        html += _form(data, "whatsapp", '<label for="country_code">Country code</label>'
            f'<input id="country_code" name="country_code" type="tel" autocomplete="tel-country-code" required maxlength="4" value="{_text(country_code)}" placeholder="+65" aria-describedby="country-hint">'
            '<p id="country-hint" class="caption spaced">+65 is Singapore. Change this to your country code if needed.</p>'
            '<label for="phone">WhatsApp phone number</label>'
            f'<input id="phone" name="phone" type="tel" autocomplete="tel-national" required value="{_text(phone_value)}" placeholder="8123 4567" aria-describedby="phone-hint">'
            '<p id="phone-hint" class="caption spaced">Use the WhatsApp number you will send your join code from. Enter your local number with the country code above, or paste a full international number beginning with +.</p>'
            '<div class="actions"><button class="btn primary" type="submit">Get a join code →</button></div>')
        if saved_phone and not canonical:
            html += '<p class="message error">Confirm the country code and save your number again before sending a join code.</p>'
        if canonical:
            html += f'<p class="account spaced">Send from this exact WhatsApp number: +{_text(canonical)}</p>'
        code = str(data.get("join_code") or "")
        if code and canonical and not data.get("join_expired"):
            html += f'<code class="code">join {_text(code)}</code><p class="caption">Send this yourself from the same WhatsApp number. Opening WhatsApp does not link your number.</p>'
            if data.get("join_code_expires_at"):
                html += f'<p class="caption spaced">Code expires: {_text(data["join_code_expires_at"])}. Saving the same number keeps this code until it expires.</p>'
            url = _whatsapp_url(data, "join " + code)
            html += ('<div class="actions">' + _link("Open WhatsApp →", url, True) + '</div>') if url else '<p class="message">The bot number is not configured. Contact Son for setup.</p>'
        elif data.get("join_expired"):
            html += '<p class="message">Your join code has expired. Get a new code above before sending.</p>'
    return html + '</div>' + _beta_note(data) + '<div class="actions">' + _link("Refresh status", "/connections/whatsapp") + _link("Back to Connections", "/connections") + '</div>' + _onboarding_poll(data)


def _revocation(data: dict) -> str:
    status = data.get("revocation_status")
    if not status:
        return ""
    if status in ("revoked", "confirmed", "success", "succeeded"):
        return '<p class="message">Google access was disconnected locally. Provider revocation is confirmed.</p>'
    return ('<p class="message">Google access is disconnected locally. Provider revocation is not confirmed. '
            'Calendar and Gmail use through this connection have stopped. '
            '<a href="https://myaccount.google.com/connections" rel="noopener noreferrer">Remove Pippa’s access in your Google account</a>.</p>')


def _dashboard(data: dict) -> str:
    status, detail = _whatsapp_state(data)
    pill = "Pippa is active" if status == "Active" else status if status in ("Waiting for beta approval", "Beta access approved", "Replies blocked", "Reply failed", "Reply not confirmed") else "Setup in progress"
    html = (f'<div class="heading"><div><p class="kicker">Your workspace</p><h1>Connections</h1></div><span class="pill">{pill}</span></div>'
            '<p class="lead">The accounts Pippa can use, and how you reach her.</p>' + _revocation(data)
            + '<section class="panel"><div class="section-title"><div><h2>Google account</h2>'
            f'<div class="account">{_text(data.get("email"))}</div></div><a href="/connections/disconnect">Disconnect Google</a></div>')
    defaults = {"calendar": "Read events · create and update events", "gmail": "Read and search only"}
    for service, label in (("calendar", "Google Calendar"), ("gmail", "Gmail · optional")):
        state = _status(data, service)
        detail_text = data.get(service + "_detail") or (defaults[service] if state == "connected" else
            "Access needs to be renewed. Tasks using this connection are paused." if state == "needs_attention" else
            "Access has not been verified. Check the connection before use." if state in ("unknown", "checking") else
            "Calendar permission is required for scheduling." if service == "calendar" else "Add email context when you’re ready.")
        action = "Manage" if state == "connected" else "Reconnect" if state == "needs_attention" else "Review" if state in ("unknown", "checking") else "Connect"
        html += (f'<div class="service" data-service="{service}" data-status="{state}">{_icon(service)}<div><h3>{label}</h3>'
                 f'{_status_label(data, service)}<p class="caption">{_text(detail_text)}</p></div>{_link(action, "/connections/" + service)}</div>')
    checked = _text(data.get("checked_at")) or "Not checked yet"
    html += f'<p class="caption spaced">Last checked: {checked}</p><div class="actions">{_check(data)}</div></section>'
    html += (f'<section class="panel"><div class="section-title"><h2>Talk to Pippa</h2><span class="status {"good" if status == "Active" else ""}">{status}</span></div>'
             + _capability("whatsapp", "WhatsApp", _text(detail)) + _first_message(data))
    if data.get("onboarding"):
        html += _onboarding_feedback(data)
    if status != "Active":
        html += '<div class="actions">' + _link("View activation status" if data.get("joined") else "Link WhatsApp →", "/connections/whatsapp", not data.get("joined")) + '</div>'
    if data.get("test_number_mode", True):
        html += '<p class="caption spaced">Meta test number: Son must also add and verify your number as a test recipient before Pippa can reply.</p>'
    html += ('</section><section class="panel"><div class="section-title"><h2>Meet with two guests</h2>'
             + _link("Open Pippa", "/app", True)
             + _link("Plan in chat", "/connections/chat", True)
             + '</div><p class="caption">Paste their availability, find a shared time, then confirm the invitations.</p>'
             + _link("Or collect replies with private links", "/connections/meetings") + '</section>'
             + '<section class="panel"><div class="section-title"><h2>Your Pippa booking page</h2>'
             + _link("Manage page", "/connections/scheduling")
             + '</div><p class="caption">Choose a personal username and let people book a 30-minute meeting on your Google Calendar. Set your hours, then publish your link.</p></section>'
             + '<section class="panel"><div class="section-title"><h2>Your booking link <span class="caption">· optional</span></h2>')
    booking = str(data.get("booking_url") or "")
    html += _link("Edit link" if booking else "Add link", "/connections/booking") + '</div><p class="caption">Let Pippa share your Calendly page when you ask.</p>'
    safe_url = _booking_url(booking)
    if safe_url:
        html += f'<p class="account spaced"><a href="{_text(safe_url)}" rel="noopener noreferrer">{_text(booking)}</a></p>'
    elif booking:
        html += '<p class="caption spaced">Your saved link needs attention. Edit it to use a valid HTTPS Calendly booking page.</p>'
    return html + '<p class="caption spaced">A saved link only. No Calendly account access or booking sync.</p></section>'


def _booking(data: dict) -> str:
    value = _text(data.get("booking_url"))
    html = ('<p class="kicker">Your workspace</p><h1>Your Calendly link</h1><p class="lead">Add the public booking page you want Pippa to share.</p>'
            '<div class="panel">' + _form(data, "booking", '<label for="booking_url">Booking URL</label>'
            f'<input id="booking_url" type="url" name="booking_url" value="{value}" placeholder="https://calendly.com/your-name/intro" aria-describedby="booking-hint">'
            '<p id="booking-hint" class="caption spaced">Use an HTTPS calendly.com page with your booking path. Leave blank to remove your saved link.</p>'
            '<div class="actions"><button class="btn primary" type="submit">Save link</button>' + _link("Cancel", "/connections") + '</div>'))
    if value:
        html += '<div class="actions">' + _form(data, "booking", '<input type="hidden" name="booking_url" value=""><button class="link-button" type="submit">Remove link</button>') + '</div>'
    return html + '</div><p class="caption">A saved link only. No Calendly account access or booking sync. Saving does not verify that the page is available or make a booking.</p>'


def _disconnect(data: dict) -> str:
    return ('<p class="kicker">Manage your connection</p><h1>Disconnect Google from Pippa?</h1>'
            '<p class="lead">Pippa will stop using both Calendar and Gmail through this connection and remove its saved tokens.</p>'
            '<div class="panel"><p>Access is disabled locally immediately. Google provider revocation is a separate result; we’ll show whether it was confirmed.</p>'
            '<p>Your Pippa account, WhatsApp link, and saved booking link stay in place. Disconnecting does not delete previously stored Pippa data.</p>'
            + _form(data, "disconnect", '<div class="actions"><button class="btn primary" type="submit">Disconnect Google</button>'
            + _link("Keep connected", "/connections") + '</div>') + '</div>')


def render_connection_page(view: str, data: dict) -> str:
    """Render server-confirmed state. Unknown capabilities never imply granted access."""
    renderers = {"welcome": _welcome, "calendar": lambda d: _service_page(d, "calendar"),
                 "gmail": lambda d: _service_page(d, "gmail"), "whatsapp": _whatsapp,
                 "dashboard": _dashboard, "booking": _booking, "disconnect": _disconnect}
    body = renderers[view](data) if view in renderers else '<h1>Let’s get you back on track.</h1><p class="lead">This request could not be completed.</p>' + _link("Back to Connections", "/connections")
    messages = ""
    if data.get("notice"):
        messages += f'<div class="message" role="status">{_text(data["notice"])}</div>'
    if data.get("error"):
        messages += f'<div class="message error" role="alert">{_text(data["error"])}</div>'
    steps = ""
    completed = (bool(data.get("email")), _status(data, "calendar") == "connected", _status(data, "gmail") == "connected", bool(data.get("joined") and data.get("active")))
    for i, (key, label, done) in enumerate(zip(("welcome", "calendar", "gmail", "whatsapp"), ("Sign in", "Calendar", "Gmail · optional", "WhatsApp"), completed), 1):
        state = " current" if view == key else " done" if done else ""
        current = ' aria-current="step"' if view == key else ""
        steps += f'<li data-step="{key}" class="step{state}"{current}><span class="step-number">{"✓" if done else i}</span><span>{label}</span></li>'
    logout = _form(data, "logout", '<button class="link-button" type="submit">Sign out</button>', True) if data.get("email") else ""
    identity = _text(data.get("email")) or "Your day, a little lighter."
    title = {"welcome": "Welcome", "dashboard": "Connections", "calendar": "Google Calendar", "gmail": "Optional Gmail", "whatsapp": "WhatsApp", "booking": "Booking link", "disconnect": "Disconnect Google"}.get(view, "Connection error")
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{title} · Pippa</title><style>{CSS}</style></head><body><a class="skip" href="#content">Skip to content</a><div class="app">'
            f'<header class="top"><a class="brand" href="/"><span class="brand-dot" aria-hidden="true"></span>pippa</a><div class="meta">{identity}</div></header>'
            f'<div class="shell"><aside class="side"><p class="side-label">{"Your workspace" if view in ("dashboard", "booking", "disconnect") else "A small setup"}</p>'
            f'<ol class="steps" aria-label="Setup progress">{steps}</ol><p class="side-note">A few useful connections.<br>One familiar conversation.</p></aside>'
            f'<main class="main" id="content">{messages}{body}</main></div><footer class="footer"><span>Made for the way you already plan.</span>'
            f'<span><a href="/privacy">Privacy &amp; data deletion</a></span>{logout}</footer></div></body></html>')
