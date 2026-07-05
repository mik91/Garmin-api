import json

import anthropic

from garmin_ai import config

SYSTEM_PROMPT = (
    "You are a personal health and fitness analyst. You are given aggregated "
    "trend statistics derived from the user's Garmin device data (resting heart "
    "rate, sleep, steps, stress). Give a concise, honest narrative summary of "
    "what the numbers show, calling out notable trends or changes. Do not "
    "invent numbers that weren't given to you, and don't give medical diagnoses "
    "-- suggest consulting a doctor for anything concerning."
)


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)


def generate_summary(summary: dict) -> str:
    client = _client()
    response = client.messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=1024,
        thinking={"type": "adaptive"},
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": (
                    "Here are my recent aggregated Garmin stats as JSON:\n\n"
                    f"{json.dumps(summary, indent=2, default=str)}\n\n"
                    "Give me a short narrative summary of my recent health/fitness trends."
                ),
            }
        ],
    )
    return next((b.text for b in response.content if b.type == "text"), "")


def ask(summary: dict, question: str, history: list[dict] | None = None) -> str:
    client = _client()
    messages = list(history or [])
    messages.append(
        {
            "role": "user",
            "content": (
                f"My recent aggregated Garmin stats (JSON): {json.dumps(summary, default=str)}\n\n"
                f"Question: {question}"
            ),
        }
    )
    response = client.messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=1024,
        thinking={"type": "adaptive"},
        system=SYSTEM_PROMPT,
        messages=messages,
    )
    return next((b.text for b in response.content if b.type == "text"), "")
