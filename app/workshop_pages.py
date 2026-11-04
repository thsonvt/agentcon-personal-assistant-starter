"""Small, server-rendered pages for the local Calendar workshop."""
from html import escape

from fastapi.responses import HTMLResponse


def page(title, content, status=200, *, user=None, current='connections'):
    from app.web_pages import MARK, render_sidebar

    identity = dict(name=user.first_name if user else 'Your workspace',
                    email=user.email if user else '', local_calendar=True)
    return HTMLResponse(f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><title>{escape(title)} · Pippa</title>
<link rel="stylesheet" href="/pippa-web-assets/app.css"></head><body>
<a class="skip" href="#workshop-content">Skip to content</a><div class="app-shell">
{render_sidebar(identity, current)}
<main class="main"><header class="topbar"><div><span class="eyebrow">YOUR EVERYDAY ASSISTANT</span><h1>Your workspace</h1></div><span class="online">On your laptop</span></header>
<div id="workshop-content" class="workshop-scroll"><div class="workshop-panel"{' role="alert"' if status >= 400 else ''}>
<div class="workshop-heading"><div class="pippa-mark">{MARK}</div><p class="eyebrow">A LITTLE SPACE TO THINK</p><h2>{escape(title)}</h2></div>
<div class="workshop-content">{content}</div></div></div></main></div></body></html>''', status_code=status,
        headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'same-origin', 'X-Content-Type-Options': 'nosniff'})


def form(action, label, csrf, disabled=False):
    return (f'<form method="post" action="{escape(action)}"><input type="hidden" name="csrf" '
            f'value="{escape(csrf)}"><button type="submit"{" disabled" if disabled else ""}>{escape(label)}</button></form>')
