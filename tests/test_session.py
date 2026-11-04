import asyncio

from agents import SQLiteSession

from app.session import trim_session


def turn(n):
    """One exchange: user message, tool call, tool output, assistant reply (4 items)."""
    return [
        {"role": "user", "content": f"question {n}"},
        {"type": "function_call", "call_id": f"c{n}", "name": "list_events", "arguments": "{}"},
        {"type": "function_call_output", "call_id": f"c{n}", "output": "No events"},
        {"role": "assistant", "content": f"answer {n}"},
    ]


def run(coro):
    return asyncio.run(coro)


def test_short_session_untouched():
    session = SQLiteSession("s1")
    run(session.add_items(turn(1)))
    run(trim_session(session, keep=30))
    assert len(run(session.get_items())) == 4


def test_long_session_trimmed_and_starts_with_user_message():
    session = SQLiteSession("s2")
    for n in range(10):  # 40 items
        run(session.add_items(turn(n)))
    run(trim_session(session, keep=30))
    items = run(session.get_items())
    assert len(items) <= 30
    assert items[0] == {"role": "user", "content": "question 3"}
    assert items[-1] == {"role": "assistant", "content": "answer 9"}
