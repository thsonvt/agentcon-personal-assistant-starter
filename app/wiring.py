"""Build the real objects from Settings. Used by main.py (server) and chat.py (terminal)."""

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime

from agents import SQLiteSession, set_default_openai_key
from fastapi import APIRouter
from openai import OpenAI, AsyncOpenAI

from app.agents import assistant_instructions, build_assistant, build_delegate, run_agent
from app.assistant_tools import AppContext
from app.commands import Commands
from app.config import Settings
from app.connections import Connections
from app.chat_interpreter import ChatInterpreter
from app.chat_scheduling import ChatScheduling
from app.crypto import TokenBox
from app.delegate_tools import DelegateContext
from app.nudge import nudge_loop
from app.router import IncomingMessage, Router, Sender
from app.session import trim_session
from app.public import public_routes
from app.public_booking import PublicBooking
from app.booking_email import LoopsBookingEmail
from app.event_networking import EventNetworking
from app.group_meetings import GroupMeetings
from app.group_privacy import install_private_meeting_log_filter
from app.signup import signup_routes
from app.store import Contact, LoopInTask, Store
from app.timeutil import SGT
from app.tools.calendar import CalendarAuthError, GoogleCalendar, build_service
from app.tools.calendly import BookingLink, CalendlyClient
from app.tools.google_oauth import GoogleOAuth
from app.tools.gmail import GmailAuthError, GoogleGmail, build_service as build_gmail_service
from app.tools.voice import Transcriber
from app.tools.elevenlabs import ElevenLabsVoice
from app.web_assistant import WebAssistant
from app.tools.whatsapp import WhatsAppClient
from app.users import User, Users

log = logging.getLogger(__name__)
NAMED_INTRO_TEMPLATE = "loop_in_intro_named"  # "Hi {{1}}, I'm {{2}}'s scheduling assistant. They would like to set up {{3}}…"


def sgt_now() -> datetime:
    return datetime.now(SGT)


def make_transcriber(settings: Settings) -> Transcriber:
    # Give up after a minute: a hung transcription holds the sender's lock, so later messages wait behind it.
    return Transcriber(OpenAI(api_key=settings.openai_api_key, timeout=60, max_retries=1), settings.openai_transcribe_model)


@dataclass
class Runtime:
    store: Store
    router: Router
    app_ctx: AppContext  # Pippa's
    users: Users
    box: TokenBox | None = None
    user_contexts: dict[int, AppContext] = field(default_factory=dict)  # friends', built on first message
    on_user_changed: Callable[[int], None] = lambda user_id: None  # drop a friend's cached context
    routes: list[APIRouter] = field(default_factory=list)
    jobs: list[Callable[[], Awaitable[None]]] = field(default_factory=list)
    status_handler: Callable[[dict], None] | None = None
    public_booking: PublicBooking | None = None
    group_meetings: GroupMeetings | None = None
    event_networking: EventNetworking | None = None
    chat_scheduling: ChatScheduling | None = None
    conference_capture: object | None = None
    capture_ingest: Callable[[IncomingMessage], bool] | None = None
    proactive_briefs: object | None = None
    followups: object | None = None
    web_assistant: WebAssistant | None = None


def build_runtime(settings: Settings, sender: Sender | None = None) -> Runtime:
    set_default_openai_key(settings.openai_api_key)
    store = Store(settings.db_path)
    users = Users(store, max_users=settings.max_users, allowed_emails=settings.beta_allowed_emails or None,
                  auto_activate=settings.beta_auto_approve)
    users.ensure_admin(settings.owner_name, settings.owner_whatsapp, sgt_now())
    box = TokenBox(settings.token_key) if settings.signup_enabled else None
    whatsapp = sender or WhatsAppClient(settings.wa_token, settings.wa_phone_number_id)
    followups = None
    if settings.followups_enabled:
        from app.followups import FollowUps
        followups = FollowUps(store, users, whatsapp, sgt_now, settings.followup_reminder_template)
    calendar = GoogleCalendar(
        build_service(settings.google_client_id, settings.google_client_secret, settings.google_refresh_token),
        calendar_id=settings.google_calendar_id,
    )
    app_ctx = AppContext(
        calendar=calendar,
        calendly=CalendlyClient(settings.calendly_token, settings.calendly_event_type_uri),
        store=store,
        now=sgt_now,
        whatsapp=whatsapp,
        owner_phone=settings.owner_whatsapp,
        owner_name=settings.owner_name,
        followups=followups,
        gmail=GoogleGmail(build_gmail_service(settings.google_client_id, settings.google_client_secret,
                                            settings.google_refresh_token)),
    )
    assistant = build_assistant(settings.openai_model)
    delegate = build_delegate(settings.openai_model)
    sessions: dict[str, SQLiteSession] = {}
    user_contexts: dict[int, AppContext] = {}
    web_contexts: dict[int, AppContext] = {}
    group_meetings = None
    proactive_briefs = None

    def session_for(session_id: str) -> SQLiteSession:
        # One conversation memory per chat: each user's own chat, and each (inviter, contact) Delegate chat.
        if session_id not in sessions:
            sessions[session_id] = SQLiteSession(session_id, settings.db_path)
        return sessions[session_id]

    def context_for(user: User, *, web: bool = False) -> AppContext:
        """A friend's own AppContext: their calendar, facts, name and counters. Built once, then cached."""
        if user.id == 1 and not web:
            return app_ctx
        if user.id == 1 and web:
            if user.id not in web_contexts:
                web_contexts[user.id] = replace(app_ctx, whatsapp=None, completed_writes=[], run_no=0,
                    pending_bookings={}, pending_deletes={}, pending_loop_ins={}, pending_groups={})
            return web_contexts[user.id]
        contexts = web_contexts if web else user_contexts
        allowed_statuses = ('approved', 'active') if web else ('active',)
        # A cached provider client must not outlive a disconnected account.
        current = users.get(user.id)
        if (current is None or current.status not in allowed_statuses or not current.google_token_enc
                or current.calendar_authorized is False
                or (web and (current.is_admin or current.event_only or not current.calendar_ready))):
            contexts.pop(user.id, None)
            raise RuntimeError(f"user #{user.id} has no usable Google connection")
        user = current
        if user.id not in contexts:
            if box is None or not user.google_token_enc:
                raise RuntimeError(f"user #{user.id} has no usable Google token")
            service = build_service(
                settings.google_web_client_id, settings.google_web_client_secret, box.decrypt(user.google_token_enc)
            )

            def guard(capability: str) -> None:
                """Recheck immediately before HTTP, including tools in a retained agent run."""
                latest = users.get(user.id)
                if (latest is None or latest.status not in allowed_statuses or latest.google_disconnected
                        or latest.google_revocation_status == 'pending'
                        or (web and (latest.event_only or not latest.calendar_ready))
                        or not latest.google_token_enc or latest.google_token_enc != user.google_token_enc
                        or latest.google_connection_version != user.google_connection_version
                        or getattr(latest, capability + "_authorized") is False):
                    error = CalendarAuthError if capability == "calendar" else GmailAuthError
                    raise error("This Google connection has changed or been disconnected. Open Connections to reconnect.")

            contexts[user.id] = AppContext(
                calendar=GoogleCalendar(service, before_request=lambda: guard("calendar")),
                calendly=BookingLink(user.calendly_url),
                store=store.for_user(user.id),
                now=sgt_now,
                whatsapp=None if web else whatsapp,
                owner_phone=user.phone or "",
                owner_name=user.first_name,
                reauth_message=(
                    f"ERROR: {user.first_name}'s Google access has expired. "
                    f"Ask them to sign in again at {settings.base_url}/signup"
                ),
                intro_template=NAMED_INTRO_TEMPLATE,
                intro_with_owner_name=True,
                gmail=GoogleGmail(build_gmail_service(settings.google_web_client_id, settings.google_web_client_secret,
                                                    box.decrypt(user.google_token_enc)),
                                  before_request=lambda: guard("gmail")) if user.gmail_authorized is not False else None,
                gmail_reauth_message=f"Reconnect Gmail read access at {settings.base_url}/connections/gmail",
                group_meetings=group_meetings,
                followups=followups,
            )
        return contexts[user.id]

    def forget_user(user_id: int) -> None:
        user_contexts.pop(user_id, None)
        web_contexts.pop(user_id, None)
        if proactive_briefs is not None:
            if users.get(user_id) is None:
                proactive_briefs.remove(user_id)
            else:
                proactive_briefs.pause(user_id)

    async def preparation_context(user, text):
        if (proactive_briefs is None or user is None
                or re.search(r'\b(book|schedule|reschedule|move|cancel|delete|invite|send|remember|save|forget|set|remind|reminder|snooze|follow[ -]?up)\b', text, re.I)
                or not re.search(r'\b(prepar\w*|prep|rehears\w*|examples?|brief\w*|those|these|continue|help me|talking points)\b', text, re.I)):
            return ''
        try:
            return await asyncio.wait_for(proactive_briefs.conversation_context(user), timeout=20)
        except Exception:
            return 'Current meeting preparation could not be revalidated. Do not rely on previous briefs; refresh the relevant sources.'

    async def record_brief(session, text, reply):
        await session.add_items([{'role': 'user', 'content': text}, {'role': 'assistant', 'content': reply}])
        await trim_session(session)

    async def run_owner(text: str) -> str:
        if settings.conference_capture_enabled and text.lower().startswith('assistant:'):
            text = text.split(':', 1)[1].strip()
        session = session_for(settings.owner_whatsapp)
        if proactive_briefs is not None:
            reply = await proactive_briefs.answer(settings.owner_whatsapp, text)
            if reply is not None:
                await record_brief(session, text, reply)
                return reply
        app_ctx.meeting_preparation_context = await preparation_context(users.get(1), text)
        try:
            return await run_agent(assistant, app_ctx, session, text)
        finally:
            app_ctx.meeting_preparation_context = ''

    def cannot_open_calendar() -> str:
        if box is None:
            return "The test is paused right now. You'll hear when it's back."
        return f"Your Google access needs refreshing. Please sign in again at {settings.base_url}/signup"

    async def run_user(user: User, text: str) -> str:
        if settings.conference_capture_enabled and text.lower().startswith('assistant:'):
            text = text.split(':', 1)[1].strip()
        try:
            ctx = context_for(user)
        except (ValueError, RuntimeError):
            log.exception("Could not open user #%d's calendar", user.id)
            return cannot_open_calendar()
        session = session_for(user.phone)
        if proactive_briefs is not None:
            reply = await proactive_briefs.answer(user.phone, text)
            if reply is not None:
                await record_brief(session, text, reply)
                return reply
        ctx.meeting_preparation_context = await preparation_context(user, text)
        try:
            return await run_agent(assistant, ctx, session, text)
        finally:
            ctx.meeting_preparation_context = ''

    async def run_contact(contact: Contact, task: LoopInTask, text: str) -> str:
        inviter = users.get(contact.user_id)
        if inviter is None or not (inviter.is_admin or inviter.status == "active"):
            # Never fall back to someone else's context: the invite simply stops.
            if inviter is not None:
                store.for_user(inviter.id).set_task_status(task.id, "cancelled", sgt_now())
            return "Sorry, this request is no longer active."
        try:
            base = context_for(inviter)
        except (ValueError, RuntimeError):
            log.exception("Could not open user #%d's calendar for an invite", inviter.id)
            return "Sorry, I can't check the calendar right now. I'll let them know."
        # Own copy of the context, so a contact's run and the inviter's run never share `completed_writes`.
        contact_app = replace(base, completed_writes=[], gmail=None, pending_bookings={}, pending_loop_ins={}, pending_deletes={})
        d = DelegateContext(app=contact_app, task_id=task.id, contact=contact)
        return await run_agent(delegate, contact_app, session_for(f"{contact.user_id}:{contact.phone}"), text, context=d)

    # Media downloads always need the real client, even when chat.py prints replies to the terminal.
    media = WhatsAppClient(settings.wa_token, settings.wa_phone_number_id)
    transcriber = make_transcriber(settings)

    def transcribe(media_id: str) -> str:
        return transcriber.transcribe(*media.download_media(media_id))

    def transcribe_for_sender(media_id: str, phone: str) -> str:
        language, prompt = "auto", ""
        if phone == settings.owner_whatsapp:
            language = settings.owner_voice_language
            contacts = store.list_contacts()
        else:
            user = users.by_phone(phone)
            # Invited external contacts must not receive another user's contact vocabulary.
            contacts = store.for_user(user.id).list_contacts() if user and user.status == "active" else []
        if contacts:
            prompt = "Names: " + ", ".join(c.name for c in contacts[:20])
        return transcriber.transcribe(*media.download_media(media_id), language=language, prompt=prompt)

    oauth = None
    if settings.signup_enabled:
        oauth = GoogleOAuth(
            settings.google_web_client_id, settings.google_web_client_secret, f"{settings.base_url}/signup/callback"
        )

    def revoke(token: str) -> None:
        if oauth is not None:
            oauth.revoke(token)

    commands = Commands(
        users,
        whatsapp,
        sgt_now,
        decrypt=box.decrypt if box else (lambda sealed: sealed),
        revoke=revoke,
        on_user_removed=forget_user,
        base_url=settings.base_url,
        test_number_mode=settings.whatsapp_test_mode,
    )
    router = Router(
        owner_phone=settings.owner_whatsapp,
        store=store,
        sender=whatsapp,
        run_owner=run_owner,
        now=sgt_now,
        run_contact=run_contact,
        transcribe=transcribe,
        transcribe_for_sender=transcribe_for_sender,
        commands=commands,
        users=users,
        run_user=run_user,
    )
    routes = [public_routes(settings.signup_enabled)]  # home + privacy pages Google requires (SON-73)
    public_booking = None
    event_networking = None
    chat_scheduling = None
    web_assistant = None
    if settings.signup_enabled:
        connections = Connections(
            oauth=oauth, users=users, box=box, commands=commands,
            bot_whatsapp=settings.bot_whatsapp, now=sgt_now,
            on_user_changed=forget_user, test_number_mode=settings.whatsapp_test_mode,
        )
        routes.append(connections.router)
        routes.append(
            signup_routes(
                oauth=oauth, users=users, box=box, commands=commands,
                bot_whatsapp=settings.bot_whatsapp, session_secret=settings.session_secret, now=sgt_now,
                on_user_changed=forget_user,
                test_number_mode=settings.whatsapp_test_mode,
                connections_callback=connections.callback,
            )
        )

        def calendar_for_booking(user_id: int) -> GoogleCalendar:
            # A public page needs verified Calendar access, not WhatsApp activation.
            user = users.get(user_id)
            if (user is None or user.is_admin or user.status not in ("approved", "active")
                    or not user.calendar_ready or user.google_revocation_status == "pending"):
                raise CalendarAuthError("This host's calendar is unavailable. Please try again later.")
            service = build_service(settings.google_web_client_id, settings.google_web_client_secret,
                                    box.decrypt(user.google_token_enc))

            def guard() -> None:
                latest = users.get(user_id)
                if (latest is None or latest.status not in ("approved", "active")
                        or not latest.calendar_ready or latest.google_revocation_status == "pending"
                        or latest.google_token_enc != user.google_token_enc
                        or latest.google_connection_version != user.google_connection_version):
                    raise CalendarAuthError("This host's calendar connection has changed. Please try again later.")

            return GoogleCalendar(service, before_request=guard, check_selected_calendars=True)

        public_booking = PublicBooking(connections, users, calendar_for_booking, sgt_now,
                                       settings.base_url, settings.session_secret,
                                       host_email=LoopsBookingEmail(settings.loops_api_key, settings.booking_host_email_id))
        group_meetings = GroupMeetings(connections, users, calendar_for_booking, sgt_now,
                                      settings.base_url, settings.session_secret)
        install_private_meeting_log_filter()
        app_ctx.group_meetings = group_meetings
        routes.append(group_meetings.router)
        chat_scheduling = ChatScheduling(group_meetings, ChatInterpreter(
            AsyncOpenAI(api_key=settings.openai_api_key, timeout=45, max_retries=0), settings.openai_model))
        routes.append(chat_scheduling.router)
        def web_instructions(ctx, agent):
            return assistant_instructions(ctx, agent).replace('on WhatsApp', 'in the Pippa web app') + (
                '\nWeb channel: WhatsApp guest outreach is unavailable. Do not offer to message guests. '
                'Use Calendar tools and the existing explicit confirmation/readback rules. '
                'The user can also use /connections/chat for group scheduling.'
            )

        def web_tool(tool):
            async def invoke(ctx, arguments):
                current = users.get(ctx.context.store.user_id)
                if not web_assistant._ready(current):
                    return 'ERROR: This account is unavailable. Reconnect through Connections.'
                try:
                    if ctx.context.calendar.before_request is not None:
                        ctx.context.calendar.before_request()
                except CalendarAuthError:
                    return 'ERROR: This Google connection changed. Reconnect through Connections.'
                return await tool.on_invoke_tool(ctx, arguments)
            return replace(tool, on_invoke_tool=invoke)

        web_agent = assistant.clone(name='Pippa web', instructions=web_instructions,
                                    tools=[web_tool(tool) for tool in assistant.tools
                                           if tool.name not in {'start_loop_in', 'cancel_loop_in'}])

        async def run_web(user, text):
            ctx = context_for(user, web=True)
            session = session_for(f'{user.id}:web')
            try:
                if proactive_briefs is not None:
                    reply = await proactive_briefs.answer(user.phone, text)
                    if reply is not None:
                        await record_brief(session, text, reply)
                        return reply
                ctx.meeting_preparation_context = await preparation_context(user, text)
                return await run_agent(web_agent, ctx, session, text)
            finally:
                ctx.meeting_preparation_context = ''
                if users.get(user.id) is None:
                    await session.clear_session()
                    # A late model tool can finish after account deletion.
                    for table in ('contacts', 'profile_facts'):
                        users.conn.execute(f'DELETE FROM {table} WHERE user_id=?', (user.id,))
                    users.conn.commit()
                    sessions.pop(f'{user.id}:web', None)
                    web_contexts.pop(user.id, None)

        web_assistant = WebAssistant(connections, run_web,
                                    ElevenLabsVoice(settings.elevenlabs_api_key, settings.elevenlabs_voice_id),
                                    settings.base_url)
        routes.append(web_assistant.router)
        if settings.event_networking_enabled:
            event_networking = EventNetworking(connections, users, public_booking, sgt_now, settings.base_url)
            routes.append(event_networking.router)
        # Public username routes go last, after every reserved application route.
        routes.append(public_booking.router)

    def phone_for_user(user_id: int) -> str | None:
        user = users.get(user_id)
        return user.phone if user and user.status == "active" else None

    async def nudges() -> None:
        await nudge_loop(store, whatsapp, settings.owner_whatsapp, sgt_now, phone_for_user=phone_for_user)

    conference_capture = None
    jobs = [nudges]
    if followups is not None:
        jobs.append(followups.run)
    if public_booking and public_booking.host_email.ready:
        async def booking_notifications() -> None:
            while True:
                try:
                    await asyncio.to_thread(public_booking.retry_notifications)
                except Exception:
                    log.warning('Booking confirmation retry failed; receipts retained')
                await asyncio.sleep(60)
        jobs.append(booking_notifications)
    if settings.proactive_briefs_enabled:
        from app.proactive import ProactiveBriefs
        from app.proactive_model import BriefModel
        from app.meeting_context import MeetingContext

        def calendar_for_briefs(user_id: int) -> GoogleCalendar:
            user = users.get(user_id)
            if user is None:
                raise CalendarAuthError('Calendar unavailable')
            return context_for(user).calendar

        def context_for_preparation(user_id: int) -> AppContext:
            user = proactive_briefs._current(user_id)
            if user is None:
                raise CalendarAuthError('Meeting preparation is paused or connection changed')
            return context_for(user)

        proactive_briefs = ProactiveBriefs(
            store, users, calendar_for_briefs,
            BriefModel(AsyncOpenAI(api_key=settings.openai_api_key, timeout=40, max_retries=0), settings.openai_model),
            whatsapp, sgt_now, evidence_for_event=MeetingContext(context_for_preparation),
        )
        commands.proactive_command = proactive_briefs.command
        router.before_notices = proactive_briefs.deliver_queued
        jobs.append(proactive_briefs.run)
    if followups is not None:
        def deliver_private_notices(phone):
            if proactive_briefs is not None:
                proactive_briefs.deliver_queued(phone)
            followups.deliver_queued(phone)
        router.before_notices = deliver_private_notices
    if settings.conference_capture_enabled:
        from app.capture_store import CaptureStore
        from app.capture_model import CaptureModel
        from app.capture_pages import CapturePages
        from app.conference_capture import ConferenceCapture

        captures = CaptureStore(store, sgt_now)
        pages = CapturePages(captures, users, sgt_now, settings.base_url, settings.session_secret)
        capture_model = CaptureModel(AsyncOpenAI(api_key=settings.openai_api_key, timeout=45, max_retries=0),
                                     settings.capture_model or settings.openai_model)
        conference_capture = ConferenceCapture(captures, users, store, whatsapp, capture_model,
                                               transcriber, media, sgt_now, pages, commands)
        # Fixed private routes precede the username catch-all, regardless of signup mode.
        routes.insert(0, pages.router)
        jobs.append(conference_capture.run)
        install_private_meeting_log_filter()

    return Runtime(
        store=store, router=router, app_ctx=app_ctx, users=users, box=box,
        user_contexts=user_contexts, on_user_changed=forget_user, routes=routes, jobs=jobs,
        status_handler=commands.receipts.record_status,
        public_booking=public_booking,
        group_meetings=group_meetings,
        event_networking=event_networking,
        chat_scheduling=chat_scheduling,
        web_assistant=web_assistant,
        conference_capture=conference_capture,
        capture_ingest=conference_capture.ingest if conference_capture else None,
        proactive_briefs=proactive_briefs,
        followups=followups,
    )
