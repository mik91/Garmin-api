import json

import anthropic

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


MAX_OUTPUT_TOKENS = 8192
TRUNCATED_NOTE = "\n\n*(This answer was cut off at the length limit. Ask me to continue.)*"


class AIProviderError(ValueError):
    pass


def provider_name() -> str:
    if config.AI_PROVIDER in {"anthropic", "openai"}:
        return config.AI_PROVIDER
    raise AIProviderError(
        f"Unsupported AI_PROVIDER '{config.AI_PROVIDER}'. Use 'anthropic' or 'openai'."
    )


def configured_model() -> str:
    return config.OPENAI_MODEL if provider_name() == "openai" else config.ANTHROPIC_MODEL


def is_configured() -> bool:
    if provider_name() == "openai":
        return bool(config.OPENAI_API_KEY)
    return bool(config.ANTHROPIC_API_KEY)


def missing_config_message() -> str:
    if provider_name() == "openai":
        return "Set OPENAI_API_KEY in .env to enable AI insights."
    return "Set ANTHROPIC_API_KEY in .env to enable AI insights."


def _anthropic_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)


def _openai_client():
    from openai import OpenAI

    return OpenAI(api_key=config.OPENAI_API_KEY)


def _anthropic_message(content: str) -> str:
    client = _anthropic_client()
    response = client.messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=MAX_OUTPUT_TOKENS,
        thinking={"type": "adaptive"},
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    )
    text = next((b.text for b in response.content if b.type == "text"), "")
    return text + TRUNCATED_NOTE if text and response.stop_reason == "max_tokens" else text


def _openai_message(content: str) -> str:
    client = _openai_client()
    response = client.responses.create(
        model=config.OPENAI_MODEL,
        instructions=SYSTEM_PROMPT,
        input=content,
        max_output_tokens=MAX_OUTPUT_TOKENS,
    )
    text = getattr(response, "output_text", None) or ""
    truncated = getattr(response, "status", None) == "incomplete"
    return text + TRUNCATED_NOTE if text and truncated else text


def _message(content: str) -> str:
    if provider_name() == "openai":
        return _openai_message(content)
    return _anthropic_message(content)


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
