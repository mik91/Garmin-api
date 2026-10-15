import json
import sys
import types

import pytest

from garmin_ai import config, connections


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(config, "GARMIN_TOKENSTORE", str(tmp_path / "tokens"))
    for key in config.EDITABLE_SETTINGS:
        monkeypatch.setattr(config, key, getattr(config, key))
    monkeypatch.setattr(connections, "ai_status", lambda: {"provider": config.AI_PROVIDER, "model": config.AI_MODEL, "providers": {}})
    connections._pending_mfa.clear()
    return tmp_path


def test_save_ai_persists_and_applies(isolated_settings):
    connections.save_ai({"provider": "codex", "model": " gpt-5.5 "})
    assert config.AI_PROVIDER == "codex" and config.AI_MODEL == "gpt-5.5"
    assert json.loads(config.SETTINGS_PATH.read_text()) == {"AI_PROVIDER": "codex", "AI_MODEL": "gpt-5.5"}
    assert config.load_settings()["AI_PROVIDER"] == "codex"


@pytest.mark.parametrize("body", [{"provider": "gemini"}, {"provider": "claude", "model": "a b"}, {"provider": "claude", "model": 3}])
def test_save_ai_rejects_invalid(body):
    with pytest.raises(ValueError):
        connections.save_ai(body)


def test_save_sync_validates_ranges():
    connections.save_sync({"interval_min": 30, "days": 14})
    assert (config.GARMIN_AUTO_SYNC_INTERVAL_MIN, config.GARMIN_AUTO_SYNC_DAYS) == (30, 14)
    with pytest.raises(ValueError):
        connections.save_sync({"interval_min": 30, "days": 0})
    with pytest.raises(ValueError):
        connections.save_sync({"interval_min": True, "days": 7})


def test_load_settings_ignores_unknown_and_mistyped(isolated_settings):
    config.SETTINGS_PATH.write_text(json.dumps({"AI_PROVIDER": "codex", "GARMIN_PASSWORD": "x", "GARMIN_AUTO_SYNC_DAYS": "7"}))
    assert config.load_settings() == {"AI_PROVIDER": "codex"}


class FakeGarmin:
    needs_mfa = True

    def __init__(self, email, password, return_on_mfa=False):
        self.email, self.full_name = email, "Test Runner"
        self.client = types.SimpleNamespace(dump=self.dump)

    def login(self):
        return ("needs_mfa", None) if self.needs_mfa else (None, None)

    def resume_login(self, state, code):
        if code != "123456":
            raise RuntimeError("Invalid MFA code")

    def dump(self, path):
        from pathlib import Path
        Path(path).mkdir(parents=True, exist_ok=True)
        (Path(path) / "garmin_tokens.json").write_text("{}")

    def _load_profile_and_settings(self):
        pass


@pytest.fixture
def fake_garmin(monkeypatch):
    monkeypatch.setitem(sys.modules, "garminconnect", types.SimpleNamespace(Garmin=FakeGarmin))
    FakeGarmin.needs_mfa = True
    return FakeGarmin


def test_garmin_login_with_mfa_saves_token_not_password(fake_garmin, isolated_settings):
    result = connections.garmin_login({"email": "runner@example.com", "password": "secret"})
    assert result["mfa_required"] and not result["garmin"]["connected"] and result["garmin"]["awaiting_mfa"]
    with pytest.raises(ValueError, match="MFA code was not accepted"):
        connections.garmin_mfa({"code": "000000"})
    result = connections.garmin_mfa({"code": "123456"})
    assert result["garmin"]["connected"] and result["garmin"]["display_name"] == "Test Runner"
    assert "secret" not in config.SETTINGS_PATH.read_text()
    connections.garmin_logout()
    assert not connections.garmin_status()["connected"]


def test_garmin_login_without_mfa(fake_garmin):
    fake_garmin.needs_mfa = False
    result = connections.garmin_login({"email": "runner@example.com", "password": "secret"})
    assert result["garmin"]["connected"] and config.GARMIN_EMAIL == "runner@example.com"


def test_mfa_without_pending_login_fails():
    with pytest.raises(ValueError, match="expired"):
        connections.garmin_mfa({"code": "123456"})
