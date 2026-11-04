"""Authenticated, native browser interface for the existing Pippa assistant."""
import html
import json


MARK = '''<svg viewBox="0 0 40 40" fill="none" aria-hidden="true" focusable="false">
<path d="M20 4v9m0 14v9M4 20h9m14 0h9M8.7 8.7l6.4 6.4m9.8 9.8l6.4 6.4M8.7 31.3l6.4-6.4m9.8-9.8l6.4-6.4" stroke="currentColor" stroke-width="4" stroke-linecap="round"/>
</svg>'''


def render_web_page(data):
    name = html.escape(str(data.get('name') or 'there'))
    email = html.escape(str(data.get('email') or ''))
    state = json.dumps(data).replace('&', '\\u0026').replace('<', '\\u003c').replace('>', '\\u003e')
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Pippa — your everyday assistant</title><link rel="stylesheet" href="/pippa-web-assets/app.css">
<script src="/pippa-web-assets/handsfree.js" defer></script><script src="/pippa-web-assets/app.js" defer></script></head><body>
<a class="skip" href="#message">Skip to message</a><div class="app-shell">
<aside class="sidebar"><a href="/app" class="brand" aria-label="Pippa home">pippa<span>{MARK}</span></a>
<p class="tagline">A little less on your mind.</p>
<nav class="desktop-nav" aria-label="Pippa"><a href="/app" aria-current="page">Conversation</a><a href="/connections/chat">Schedule a meeting</a><a href="/connections">Connections</a></nav>
<details class="settings-menu"><summary>Settings</summary><nav aria-label="Settings"><a href="/connections/chat">Schedule a meeting</a><a href="/connections">Connections</a></nav></details>
<div class="account"><span class="avatar" aria-hidden="true">{MARK}</span><div><strong>{name}</strong><span>{email}</span></div></div></aside>
<main class="main"><header class="topbar"><div><span class="eyebrow">YOUR EVERYDAY ASSISTANT</span><h1>A moment with Pippa</h1></div><span class="online">Here to help</span></header>
<div id="chat-scroll" class="chat-scroll"><section id="welcome" class="welcome"><div class="pippa-mark">{MARK}</div><p class="eyebrow">A LITTLE SPACE TO THINK</p><h2>Hi {name}.<br>What's on your mind?</h2><p>Make a plan, remember a thought, or clear a little room in your day.</p>
<div class="suggestions" aria-label="Message ideas"><button type="button" data-prompt="Help me plan my day.">Plan my day <span aria-hidden="true">↗</span></button><button type="button" data-prompt="What can you help me with?">Explore what Pippa can do <span aria-hidden="true">↗</span></button></div></section>
<ol id="messages" class="messages" aria-label="Conversation"></ol></div>
<section class="composer-area" aria-label="Talk to Pippa"><p id="status" role="status" aria-live="polite"></p>
<details id="voice-status-details" hidden><summary>Connection details</summary><p id="voice-status-detail"></p></details>
<p id="live-transcript" class="live-transcript" aria-live="polite"></p>
<button id="refresh" class="play" type="button" hidden>Refresh conversation</button>
<form id="composer">
<div class="voice-actions"><button id="handsfree" type="button" aria-pressed="false">Start conversation</button><button id="stop-audio" type="button" hidden>Stop audio</button><button id="toggle-text" type="button" aria-expanded="true" aria-controls="text-entry">Type a message</button></div>
<div id="text-entry" class="text-entry"><label class="sr-only" for="message">Your message</label><textarea id="message" name="message" placeholder="Type a message…" rows="1" maxlength="4000" required></textarea><button id="send" class="send" type="submit">Send <span aria-hidden="true">↑</span></button></div>
<details class="more-controls"><summary>More controls</summary><button id="mic" type="button" aria-pressed="false">Record voice</button></details>
</form>
<details class="voice-help"><summary>Voice and privacy</summary><div><p id="voice-hint">Start conversation to speak naturally. Pippa replies aloud and listens again.</p><p>Voice uses ElevenLabs. Your microphone is on while listening; Stop conversation turns it off. <a href="/privacy">Privacy</a></p><p>Pippa can make mistakes. Check important details before taking action.</p></div></details>
<noscript><p>Enable JavaScript to chat with Pippa. <a href="/connections/chat">Scheduling chat</a> also works without JavaScript.</p></noscript></section>
</main></div><script id="pippa-state" type="application/json">{state}</script></body></html>'''
