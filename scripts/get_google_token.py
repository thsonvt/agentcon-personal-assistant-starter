"""One-time helper: sign in with your personal Google account and print a refresh token.

Run on your own computer (it opens a browser):
    pip install -e ".[dev]"
    python scripts/get_google_token.py
Needs GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env. Works with a "Web application" OAuth client
whose authorized redirect URIs include http://localhost:8765/ (a "Desktop app" client works too).
"""

import os
import argparse
from pathlib import Path

from dotenv import load_dotenv, set_key
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/calendar"]
PORT = 8765  # fixed, so it can be registered as a redirect URI on the Web client


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gmail", action="store_true", help="Also request optional read-only Gmail access")
    parser.add_argument("--save-env", action="store_true", help="Save the refresh token to .env without printing it")
    args = parser.parse_args()
    load_dotenv()
    scopes = SCOPES + (["https://www.googleapis.com/auth/gmail.readonly"] if args.gmail else [])
    flow = InstalledAppFlow.from_client_config(
        {
            "installed": {
                "client_id": os.environ["GOOGLE_CLIENT_ID"],
                "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [f"http://localhost:{PORT}/"],
            }
        },
        scopes,
    )
    creds = flow.run_local_server(port=PORT, access_type="offline", prompt="consent")
    if not creds.refresh_token:
        raise RuntimeError("Google did not return a refresh token; existing settings have not been changed.")
    if args.gmail and "https://www.googleapis.com/auth/gmail.readonly" not in (creds.granted_scopes or []):
        raise RuntimeError("Gmail read permission was not granted; existing settings have not been changed.")
    if args.gmail and SCOPES[0] not in (creds.granted_scopes or []):
        raise RuntimeError("Calendar permission was not granted; existing settings have not been changed.")
    if args.save_env:
        destination = Path(__file__).resolve().parents[1] / ".env"
        set_key(str(destination), "GOOGLE_REFRESH_TOKEN", creds.refresh_token)
        destination.chmod(0o600)
        print("Google refresh token saved to .env. No token was printed. Update the deployed secret through your normal release process.")
        return
    print("\nAdd this line to your .env:\n")
    print(f"GOOGLE_REFRESH_TOKEN={creds.refresh_token}")


if __name__ == "__main__":
    main()
