"""Short-term conversation memory: keep each session to its most recent items."""

from agents import SQLiteSession


async def trim_session(session: SQLiteSession, keep: int = 30) -> None:
    """Keep at most `keep` items, starting at a user message.

    Cutting in the middle of a tool call (call without its output) makes the
    next model request invalid, so the kept history always begins with the
    first user message inside the newest `keep` items.
    """
    items = await session.get_items()
    if len(items) <= keep:
        return
    tail = items[-keep:]
    first_user = next(
        (i for i, item in enumerate(tail) if isinstance(item, dict) and item.get("role") == "user"),
        None,
    )
    tail = tail[first_user:] if first_user is not None else []
    await session.clear_session()
    if tail:
        await session.add_items(tail)
