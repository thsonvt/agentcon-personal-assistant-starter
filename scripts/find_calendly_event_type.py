"""One-time helper: list your Calendly event types so you can copy CALENDLY_EVENT_TYPE_URI.

    python scripts/find_calendly_event_type.py
Needs CALENDLY_TOKEN in .env.
"""

import os

import httpx
from dotenv import load_dotenv


def main() -> None:
    load_dotenv()
    headers = {"Authorization": f"Bearer {os.environ['CALENDLY_TOKEN']}"}
    with httpx.Client(base_url="https://api.calendly.com", headers=headers, timeout=10) as http:
        me = http.get("/users/me")
        me.raise_for_status()
        user_uri = me.json()["resource"]["uri"]
        types = http.get("/event_types", params={"user": user_uri})
        types.raise_for_status()
    for event_type in types.json()["collection"]:
        status = "active" if event_type.get("active") else "inactive"
        print(f"{event_type['name']} ({status})")
        print(f"  CALENDLY_EVENT_TYPE_URI={event_type['uri']}")
        print(f"  booking link: {event_type['scheduling_url']}\n")


if __name__ == "__main__":
    main()
