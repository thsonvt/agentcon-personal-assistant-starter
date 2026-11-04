"""Talk to the agent in your terminal instead of WhatsApp.

    python chat.py                 # you are the owner
    python chat.py --as 6580000000 # pretend to be someone else (e.g. a contact you invited)
"""

import argparse
import asyncio
import uuid

from dotenv import load_dotenv

from app.config import load_settings, normalize_phone
from app.router import IncomingMessage
from app.wiring import build_runtime


class ConsoleSender:
    def send_text(self, to: str, body: str) -> str:
        print(f"\n🤖 → {to}:\n{body}\n")
        return f"console-{uuid.uuid4().hex[:8]}"

    def send_template(self, to: str, name: str, params: list[str], language: str = "en") -> str:
        print(f"\n📨 template '{name}' → {to}: {params}\n")
        return f"console-{uuid.uuid4().hex[:8]}"

    def mark_read_typing(self, message_id: str) -> None:
        print("🤖 typing…")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--as", dest="as_phone", default="owner", help='"owner" or a phone number')
    args = parser.parse_args()

    load_dotenv()
    settings = load_settings()
    runtime = build_runtime(settings, sender=ConsoleSender())
    phone = settings.owner_whatsapp if args.as_phone == "owner" else normalize_phone(args.as_phone)
    print(f"Chatting as {phone}. Ctrl+C or 'quit' to exit.")

    async def loop() -> None:
        while True:
            try:
                text = await asyncio.to_thread(input, "you> ")
            except (EOFError, KeyboardInterrupt):
                return
            if text.strip().lower() in {"quit", "exit"}:
                return
            await runtime.router.handle(IncomingMessage(sender=phone, text=text, message_id=uuid.uuid4().hex))

    asyncio.run(loop())


if __name__ == "__main__":
    main()
