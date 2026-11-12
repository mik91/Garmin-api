import json
import subprocess
from pathlib import Path

import pytest

from garmin_ai import ai_insights, config


@pytest.fixture
def fake_cli(monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        if "-o" in args:
            Path(args[args.index("-o") + 1]).write_text("codex answer", encoding="utf-8")
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 0, json.dumps({"result": "claude answer"}), "")

    monkeypatch.setattr(ai_insights.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(ai_insights.subprocess, "run", run)
    monkeypatch.setattr(config, "AI_MODEL", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    return calls


def test_legacy_provider_names_map_to_clis(monkeypatch):
    monkeypatch.setattr(config, "AI_PROVIDER", "anthropic")
    assert ai_insights.provider_name() == "claude"
    monkeypatch.setattr(config, "AI_PROVIDER", "openai")
    assert ai_insights.provider_name() == "codex"
    monkeypatch.setattr(config, "AI_PROVIDER", "gemini")
    with pytest.raises(ai_insights.AIProviderError):
        ai_insights.provider_name()


def test_claude_cli_runs_without_api_keys(monkeypatch, fake_cli):
    monkeypatch.setattr(config, "AI_PROVIDER", "claude")
    assert ai_insights.chat({}, [{"role": "user", "content": "hi"}]) == "claude answer"
    args, kwargs = fake_cli[0]
    assert args[:2] == ["/bin/claude", "-p"] and "--model" not in args
    assert "ANTHROPIC_API_KEY" not in kwargs["env"] and "OPENAI_API_KEY" not in kwargs["env"]
    assert "User: hi" in kwargs["input"]


def test_codex_cli_reads_last_message(monkeypatch, fake_cli):
    monkeypatch.setattr(config, "AI_PROVIDER", "codex")
    monkeypatch.setattr(config, "AI_MODEL", "some-model")
    assert ai_insights.chat({}, [{"role": "user", "content": "hi"}]) == "codex answer"
    args, kwargs = fake_cli[0]
    assert args[:2] == ["/bin/codex", "exec"] and args[args.index("--model") + 1] == "some-model"
    assert kwargs["input"].startswith(ai_insights.SYSTEM_PROMPT)


def test_cli_failure_surfaces_message(monkeypatch):
    monkeypatch.setattr(config, "AI_PROVIDER", "codex")
    monkeypatch.setattr(ai_insights.shutil, "which", lambda name: name)
    error = 'ERROR: {"type":"error","error":{"message":"Model not supported."}}'
    monkeypatch.setattr(ai_insights.subprocess, "run",
                        lambda args, **kwargs: subprocess.CompletedProcess(args, 1, "", error))
    with pytest.raises(ai_insights.AIProviderError, match="Codex CLI failed: Model not supported."):
        ai_insights.chat({}, [{"role": "user", "content": "hi"}])


def test_missing_cli_is_not_configured(monkeypatch):
    monkeypatch.setattr(config, "AI_PROVIDER", "claude")
    monkeypatch.setattr(ai_insights.shutil, "which", lambda name: None)
    assert not ai_insights.is_configured()


class _FakeBlock:
    def __init__(self, text):
        self.type, self.text = "text", text


class _FakeResponse:
    def __init__(self, stop_reason="end_turn", text="api answer"):
        self.stop_reason, self.content = stop_reason, [_FakeBlock(text)]


@pytest.fixture
def fake_api(monkeypatch):
    import anthropic

    calls, responses = [], []

    class FakeClient:
        def __init__(self, **kwargs):
            self.beta = self
            self.messages = self

        def create(self, **kwargs):
            calls.append(kwargs)
            return responses.pop(0) if responses else _FakeResponse()

    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
    monkeypatch.setattr(config, "AI_PROVIDER", "api")
    monkeypatch.setattr(config, "AI_MODEL", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    return calls, responses


def test_api_chat_caches_system_and_data_context(fake_api):
    calls, _ = fake_api
    answer = ai_insights.chat({"hrv": 50}, [
        {"role": "user", "content": "How is my recovery?"},
        {"role": "assistant", "content": "Fine."},
        {"role": "user", "content": "And sleep?"},
    ])
    assert answer == "api answer"
    request = calls[0]
    assert request["model"] == "claude-opus-5-5"
    assert request["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert request["fallbacks"] == [{"model": "claude-opus-4-8"}]
    opening = request["messages"][0]["content"]
    assert '"hrv": 50' in opening[0]["text"] and opening[0]["cache_control"] == {"type": "ephemeral"}
    assert opening[1]["text"] == "How is my recovery?"
    assert [m["role"] for m in request["messages"]] == ["user", "assistant", "user"]


def test_api_refusal_and_missing_key_are_reported(fake_api, monkeypatch):
    _, responses = fake_api
    responses.append(_FakeResponse(stop_reason="refusal", text=""))
    with pytest.raises(ai_insights.AIProviderError, match="declined"):
        ai_insights.ping()
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert not ai_insights.is_configured()
    with pytest.raises(ai_insights.AIProviderError, match="ANTHROPIC_API_KEY"):
        ai_insights.ping()


def test_hosted_connections_offer_only_the_api(monkeypatch):
    from garmin_ai import connections

    monkeypatch.setattr(config, "HOSTED", True)
    monkeypatch.setattr(config, "AI_PROVIDER", "api")
    assert list(connections.ai_status()["providers"]) == ["api"]
    with pytest.raises(ValueError):
        connections.save_ai({"provider": "claude", "model": ""})
    with pytest.raises(ValueError):
        connections.cli_login({"provider": "claude"})
