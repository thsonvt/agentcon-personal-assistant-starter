import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def helper(tmp_path, monkeypatch, scopes):
    path = Path(__file__).parents[1] / "scripts/get_google_token.py"
    spec = importlib.util.spec_from_file_location("token_helper", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    seen = {}
    def from_config(config, requested):
        seen["scopes"] = requested
        return SimpleNamespace(run_local_server=lambda **kwargs: SimpleNamespace(refresh_token="new-token", granted_scopes=scopes))
    monkeypatch.setattr(module.InstalledAppFlow, "from_client_config", from_config)
    monkeypatch.setattr(module, "load_dotenv", lambda: None)
    monkeypatch.setattr(module, "__file__", str(tmp_path / "scripts/get_google_token.py"))
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setattr(sys, "argv", ["get_google_token.py", "--gmail", "--save-env"])
    return module, seen


def test_gmail_helper_refuses_to_replace_calendar_token_if_calendar_permission_omitted(tmp_path, monkeypatch):
    module, _ = helper(tmp_path, monkeypatch, ["https://www.googleapis.com/auth/gmail.readonly"])
    env = tmp_path / ".env"
    env.write_text("GOOGLE_REFRESH_TOKEN=old-token\n")
    with pytest.raises(RuntimeError, match="Calendar"):
        module.main()
    assert "old-token" in env.read_text()


def test_gmail_helper_saves_without_printing_token(tmp_path, monkeypatch, capsys):
    scopes = ["https://www.googleapis.com/auth/calendar", "https://www.googleapis.com/auth/gmail.readonly"]
    module, seen = helper(tmp_path, monkeypatch, scopes)
    module.main()
    assert seen["scopes"] == scopes
    assert "new-token" in (tmp_path / ".env").read_text()
    assert "new-token" not in capsys.readouterr().out
    assert (tmp_path / ".env").stat().st_mode & 0o777 == 0o600


def test_gmail_helper_does_not_replace_token_if_read_permission_omitted(tmp_path, monkeypatch):
    module, _ = helper(tmp_path, monkeypatch, ["https://www.googleapis.com/auth/calendar"])
    with pytest.raises(RuntimeError, match="Gmail"):
        module.main()
    assert not (tmp_path / ".env").exists()
