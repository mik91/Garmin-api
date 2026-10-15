import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from garmin_ai import config

SYSTEM_PROMPT = (
    "You are a personal health and fitness analyst. You are given aggregated "
    "trend statistics derived from the user's Garmin device data (resting heart "
    "rate, sleep, steps, stress, and activity summaries by type such as running, "
    "swimming, and cycling). The context may also include an insight_engine "
    "report with current metrics, previous-period metrics, 90-day baselines, "
    "sport summaries, and pre-detected signals. Treat those signals as the "
    "highest-priority evidence, but weigh each signal by its confidence and "
    "sample_size and describe low-confidence signals as tentative. Use "
    "personal_goals when present, and use recent_daily_trend and "
    "recent_sleep_trend to distinguish a sustained pattern from a single-day "
    "change. Explain conclusions in plain language and mention when the data is "
    "sparse. Give concise, honest coaching-style guidance that "
    "separates recovery, sleep, training load, consistency, and sport-specific "
    "performance. When activity type summaries are present, distinguish between "
    "sports instead of blending them together. If running details include best "
    "efforts such as best_5k, best_10k, best_1_mile, longest_run, recent_runs, "
    "or weekly volume, use those fields to describe recent performance, but do "
    "not turn ordinary training results into a race-time prediction. When race_prediction_model "
    "is present, it is the sole authority for numerical race predictions: quote "
    "its expected and range values exactly, explain its evidence "
    "and limitations, and never calculate, adjust, or substitute another finish "
    "time. Garmin is comparison-only: if garmin_comparison is present, label it "
    "clearly as a benchmark and never describe it as the model's expected value. "
    "The model's expected value describes current fitness; do not assume improvement "
    "for a future race unless the structured model explicitly provides a projected "
    "value. When race_prediction_model is absent, do not give a numerical race-time "
    "prediction; say that a verified race or time trial is needed. Do not invent numbers that "
    "weren't given to you, and don't give medical diagnoses -- suggest "
    "consulting a doctor for anything concerning."
)


PROVIDERS = {"claude": "Claude Code CLI", "codex": "Codex CLI"}
# Older .env files used API provider names; map them onto the matching CLI.
PROVIDER_ALIASES = {"anthropic": "claude", "openai": "codex"}
# Keys that would make the CLIs bill an API account instead of the user's subscription.
API_KEY_ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "CODEX_API_KEY")


class AIProviderError(ValueError):
    pass


def provider_name() -> str:
    provider = PROVIDER_ALIASES.get(config.AI_PROVIDER, config.AI_PROVIDER)
    if provider in PROVIDERS:
        return provider
    raise AIProviderError(
        f"Unsupported AI_PROVIDER '{config.AI_PROVIDER}'. Use 'claude' or 'codex'."
    )


def configured_model() -> str:
    return config.AI_MODEL or "default"


def _cli_command() -> str:
    return config.CLAUDE_CLI if provider_name() == "claude" else config.CODEX_CLI


def _cli_path() -> str | None:
    return shutil.which(_cli_command())


def is_configured() -> bool:
    return _cli_path() is not None


def missing_config_message() -> str:
    provider = provider_name()
    if provider == "claude":
        return "Install Claude Code and run `claude` once to sign in, or set CLAUDE_CLI in .env."
    return "Install the Codex CLI and run `codex login`, or set CODEX_CLI in .env."


def _cli_env() -> dict:
    env = os.environ.copy()
    for name in API_KEY_ENV_VARS:
        env.pop(name, None)
    return env


def _run(args: list[str], prompt: str, workdir: str) -> subprocess.CompletedProcess:
    path = _cli_path()
    if path is None:
        raise AIProviderError(missing_config_message())
    try:
        result = subprocess.run(
            [path, *args], input=prompt, capture_output=True, text=True, encoding="utf-8",
            errors="replace", cwd=workdir, env=_cli_env(), timeout=config.AI_CLI_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as e:
        raise AIProviderError(f"{PROVIDERS[provider_name()]} timed out after {config.AI_CLI_TIMEOUT_S}s.") from e
    if result.returncode != 0:
        raise AIProviderError(f"{PROVIDERS[provider_name()]} failed: {_error_detail(result)}")
    return result


def _error_detail(result: subprocess.CompletedProcess) -> str:
    lines = (result.stderr or result.stdout).strip().splitlines()
    if not lines:
        return f"exit code {result.returncode}"
    detail = lines[-1].removeprefix("ERROR: ")
    try:
        payload = json.loads(detail)
    except json.JSONDecodeError:
        return detail
    # Claude reports {"result": ...}; Codex reports {"error": {"message": ...}}.
    error = payload.get("error")
    return payload.get("result") or (error.get("message") if isinstance(error, dict) else None) or detail


def _claude_message(content: str) -> str:
    # Run in an empty temp dir so no project CLAUDE.md, settings or hooks leak in.
    with tempfile.TemporaryDirectory() as workdir:
        system_file = Path(workdir) / "system.txt"
        system_file.write_text(SYSTEM_PROMPT, encoding="utf-8")
        args = ["-p", "--output-format", "json", "--system-prompt-file", str(system_file),
                "--tools", "", "--no-session-persistence"]
        if config.AI_MODEL:
            args += ["--model", config.AI_MODEL]
        result = _run(args, content, workdir)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return result.stdout.strip()
    if payload.get("is_error"):
        raise AIProviderError(f"Claude Code CLI failed: {payload.get('result') or 'unknown error'}")
    return payload.get("result") or ""


def _codex_message(content: str) -> str:
    with tempfile.TemporaryDirectory() as workdir:
        output_file = Path(workdir) / "answer.md"
        args = ["exec", "--skip-git-repo-check", "--ephemeral", "--sandbox", "read-only",
                "--color", "never", "-o", str(output_file)]
        if config.AI_MODEL:
            args += ["--model", config.AI_MODEL]
        # Codex exec has no system prompt flag, so the instructions lead the prompt.
        prompt = f"{SYSTEM_PROMPT}\n\nAnswer directly; do not run commands or edit files.\n\n{content}"
        result = _run(args + ["-"], prompt, workdir)
        if output_file.exists():
            return output_file.read_text(encoding="utf-8").strip()
    return result.stdout.strip()


def _message(content: str) -> str:
    if provider_name() == "codex":
        return _codex_message(content)
    return _claude_message(content)


def generate_summary(summary: dict) -> str:
    content = (
        "Here are my recent aggregated Garmin stats as JSON:\n\n"
        f"{json.dumps(summary, indent=2, default=str)}\n\n"
        "Give me a short narrative summary of my recent health/fitness trends."
    )
    return _message(content)


def ask(summary: dict, question: str) -> str:
    content = (
        f"My recent aggregated Garmin stats (JSON): {json.dumps(summary, default=str)}\n\n"
        f"Question: {question}"
    )
    return _message(content)


def chat(summary: dict, messages: list[dict]) -> str:
    transcript = "\n".join(
        f"{message['role'].title()}: {message['content']}"
        for message in messages
        if message.get("content")
    )
    content = (
        "My Garmin dashboard context as JSON:\n\n"
        f"{json.dumps(summary, indent=2, default=str)}\n\n"
        "Continue this conversation. Answer the latest user message directly, "
        "use the earlier turns for follow-up context, and do not repeat the "
        "whole summary unless I ask for it.\n\n"
        f"{transcript}"
    )
    return _message(content)
