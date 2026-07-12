import json

import anthropic

from garmin_ai import config

SYSTEM_PROMPT = (
    "You are a personal health and fitness analyst. You are given aggregated "
    "trend statistics derived from the user's Garmin device data (resting heart "
    "rate, sleep, steps, stress, and activity summaries by type such as running, "
    "swimming, and cycling). Give a concise, honest narrative summary of what "
    "the numbers show, calling out notable trends or changes. When activity "
    "type summaries are present, distinguish between sports instead of blending "
    "them together. If running details include best efforts such as best_5k, "
    "best_10k, best_1_mile, longest_run, recent_runs, or weekly volume, use "
    "those fields directly for race-goal or pacing questions before asking the "
    "user for more data. Do not invent numbers that weren't given to you, and "
    "don't give medical diagnoses -- suggest consulting a doctor for anything "
    "concerning."
)


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
        max_tokens=1024,
        thinking={"type": "adaptive"},
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    )
    return next((b.text for b in response.content if b.type == "text"), "")


def _openai_message(content: str) -> str:
    client = _openai_client()
    response = client.responses.create(
        model=config.OPENAI_MODEL,
        instructions=SYSTEM_PROMPT,
        input=content,
        max_output_tokens=1024,
    )
    if getattr(response, "output_text", None):
        return response.output_text
    return ""


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
