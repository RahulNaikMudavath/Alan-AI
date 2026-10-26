"""Offline provider used by tests and local development.

It returns deterministic text and performs no network calls, so the test suite
never needs an API key and never incurs cost.
"""

from app.llm.base import LLMProvider


class FakeProvider:
    """Deterministic stand-in for a real model."""

    def __init__(self, response: str = "ok") -> None:
        self._response = response
        self.prompts: list[str] = []

    async def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self._response


# Compile-time-ish check that the fake satisfies the interface.
_provider: LLMProvider = FakeProvider()
