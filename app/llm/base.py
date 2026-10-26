"""The replaceable LLM provider interface.

Nothing calls a model yet: Milestone 1 stores exactly what the client sends.
The point of this file is to fix the seam now, so adding extraction later means
writing one new class instead of rewiring the service layer.
"""

from typing import Protocol, runtime_checkable


@runtime_checkable
class LLMProvider(Protocol):
    """Contract every model integration must satisfy.

    Implementations are responsible for their own credentials and must never
    receive secrets from RecallKit configuration.
    """

    async def complete(self, prompt: str) -> str:
        """Return the model's text response to ``prompt``."""
        ...
