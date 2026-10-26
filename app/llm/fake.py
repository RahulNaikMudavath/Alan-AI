"""Offline providers used by default in development and by the whole test suite.

Both are deterministic: the same input always produces the same output, in this
process and any other. That is what makes ordering and extraction assertions
meaningful.

The embedding fake is a **bag-of-words hasher**, not a model. It scores word
overlap (unigrams plus adjacent word pairs), so ``"dark mode"`` is close to
``"prefers dark mode"``. It does not understand synonyms: ``"diet"`` is not
close to ``"vegetarian"``. Treat it as plumbing verification only.

The extraction fake is a **rule-based heuristic**, not a model. It keeps
first-person sentences from user messages and labels them with a few cue words.
It exists so the prompt→parse→store pipeline can run offline and so tests can
count how many times a provider was called. Real extraction quality requires
``openai_compatible``.
"""

import hashlib
import json
import re
from collections.abc import Sequence

from app.llm.base import EmbeddingProvider, LLMError, LLMProvider
from app.llm.prompt_contract import MESSAGES_MARKER
from app.retrieval.vectors import l2_normalize

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)

# Bigrams count for less than single words so a shared word still dominates.
_UNIGRAM_WEIGHT = 1.0
_BIGRAM_WEIGHT = 0.5

_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
_FIRST_PERSON_RE = re.compile(
    r"\b(i|i'm|i’m|i am|i've|i’ve|i'll|i’ll|my|mine|we|our|us)\b", re.IGNORECASE
)
_PREFERENCE_RE = re.compile(
    r"\b(prefer|like|love|hate|favorite|favourite|always|never|rather|enjoy)\b",
    re.IGNORECASE,
)
_EVENT_RE = re.compile(
    r"\b(tomorrow|yesterday|today|next week|next month|last week|moved|started"
    r"|finished|birthdays?|anniversar(?:y|ies))\b"
    r"|\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)s?\b",
    re.IGNORECASE,
)

# Keeps demo output under the schema's raw-item ceiling.
_MAX_FAKE_FACTS = 10
_FAKE_FACT_CHARS = 400


class FakeProvider:
    """Deterministic stand-in for a text model that emits a fixed string."""

    name = "fake"
    model = "fake-v1"

    def __init__(self, response: str = "ok") -> None:
        self._response = response
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self._response

    def close(self) -> None:
        """Nothing to release."""


class ScriptedLLMProvider:
    """Returns queued responses, so tests can pin exact model output."""

    name = "scripted"
    model = "scripted-v1"

    def __init__(self, responses: Sequence[str]) -> None:
        self._responses = list(responses)
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self._responses:
            raise LLMError("scripted provider has no response left")
        return self._responses.pop(0)

    def close(self) -> None:
        """Nothing to release."""


class DeterministicLLMProvider:
    """Heuristic extractor that emits the same JSON shape a real model must."""

    name = "fake"
    model = "recallkit-rule-based-v1"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        messages = self._read_messages(prompt)
        facts: list[dict[str, object]] = []
        for message in messages:
            if message.get("role") != "user":
                continue
            index = message.get("index")
            content = message.get("content")
            if not isinstance(index, int) or not isinstance(content, str):
                continue
            for sentence in _SENTENCE_RE.split(content):
                fact = self._sentence_to_fact(sentence, index)
                if fact is not None:
                    facts.append(fact)
                if len(facts) >= _MAX_FAKE_FACTS:
                    return json.dumps({"memories": facts}, ensure_ascii=False)
        return json.dumps({"memories": facts}, ensure_ascii=False)

    def close(self) -> None:
        """Nothing to release."""

    @staticmethod
    def _read_messages(prompt: str) -> list[dict[str, object]]:
        if MESSAGES_MARKER not in prompt:
            raise LLMError("fake provider could not find the conversation payload")
        tail = prompt.split(MESSAGES_MARKER, 1)[1].lstrip()
        line = tail.splitlines()[0] if tail else ""
        try:
            messages = json.loads(line)
        except ValueError as exc:
            raise LLMError("fake provider could not parse the conversation payload") from exc
        if not isinstance(messages, list):
            raise LLMError("fake provider expected a list of messages")
        return [message for message in messages if isinstance(message, dict)]

    @staticmethod
    def _sentence_to_fact(sentence: str, index: int) -> dict[str, object] | None:
        text = sentence.strip().rstrip(".!?").strip()
        if not text or not _FIRST_PERSON_RE.search(text):
            return None
        if _PREFERENCE_RE.search(text):
            kind = "preference"
        elif _EVENT_RE.search(text):
            kind = "event"
        else:
            kind = "fact"
        return {
            "content": text[:_FAKE_FACT_CHARS].strip(),
            "kind": kind,
            "confidence": 0.9,
            "source_message_indexes": [index],
        }


class DeterministicEmbeddingProvider:
    """Hash-based embedding provider: offline, free, and repeatable."""

    def __init__(self, dimensions: int = 256) -> None:
        if dimensions <= 0:
            raise ValueError("dimensions must be positive")
        self.dimensions = dimensions
        # Dimensions are part of the name because vector length is part of
        # comparability: 256-dim and 512-dim vectors must not be mixed.
        self.model = f"recallkit-fake-hash-v1-{dimensions}"

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def close(self) -> None:
        """Nothing to release."""

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        tokens = _TOKEN_RE.findall(text.lower())
        features = [(token, _UNIGRAM_WEIGHT) for token in tokens]
        features += [
            (f"{first} {second}", _BIGRAM_WEIGHT)
            for first, second in zip(tokens, tokens[1:], strict=False)
        ]
        for feature, weight in features:
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[index] += sign * weight
        return l2_normalize(vector)


# Cheap static check that the fakes still satisfy the interfaces they claim to.
_llm: LLMProvider = FakeProvider()
_extraction_llm: LLMProvider = DeterministicLLMProvider()
_embeddings: EmbeddingProvider = DeterministicEmbeddingProvider()
