"""Exercises the JSON-retry path in AnthropicLLM without hitting the network.

We fake the `anthropic.AsyncAnthropic` class so we can script the sequence of
LLM responses and assert the retry behavior defined in the PRD (section 12 +
non-functional 13).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

import pytest

from app.services.llm import AnthropicLLM, InvalidLLMJSON


@dataclass
class _TextBlock:
    text: str
    type: str = "text"


@dataclass
class _Message:
    content: List[_TextBlock]


class _FakeMessages:
    def __init__(self, scripted_texts: List[str]) -> None:
        self._queue = list(scripted_texts)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._queue:
            raise AssertionError("Script exhausted - test expected fewer calls.")
        return _Message(content=[_TextBlock(text=self._queue.pop(0))])


class _FakeClient:
    def __init__(self, scripted_texts: List[str]) -> None:
        self.messages = _FakeMessages(scripted_texts)


def _make_llm(monkeypatch, scripted_texts: List[str], retries: int = 3) -> tuple[AnthropicLLM, _FakeClient]:
    fake_client = _FakeClient(scripted_texts)

    # Swap out the constructor-side `from anthropic import AsyncAnthropic`
    import app.services.llm as llm_mod

    class _Stub:
        def __init__(self, *_args, **_kwargs):
            pass

    class _AsyncAnthropicShim:
        def __init__(self, *_args, **_kwargs):
            self._inner = fake_client

        # Proxy attribute access
        def __getattr__(self, name):
            return getattr(self._inner, name)

    # Patch the imported class inside AnthropicLLM.__init__'s lazy import path.
    # We do that by patching the `anthropic` module entry point.
    import sys, types

    fake_mod = types.ModuleType("anthropic")
    fake_mod.AsyncAnthropic = _AsyncAnthropicShim  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", fake_mod)

    llm = AnthropicLLM(
        api_key="sk-test",
        model="claude-sonnet-4-6",
        temperature=0.3,
        max_tokens=1000,
        max_json_retries=retries,
    )
    return llm, fake_client


@pytest.mark.asyncio
async def test_first_attempt_valid_json_succeeds(monkeypatch):
    llm, fake = _make_llm(monkeypatch, ['{"ok": true}'])
    out = await llm.generate("prompt")
    assert out == {"ok": True}
    assert len(fake.messages.calls) == 1


@pytest.mark.asyncio
async def test_retries_on_bad_json_then_succeeds(monkeypatch):
    llm, fake = _make_llm(
        monkeypatch,
        ["not json at all", "still broken", '{"ok": true}'],
    )
    out = await llm.generate("prompt")
    assert out == {"ok": True}
    assert len(fake.messages.calls) == 3


@pytest.mark.asyncio
async def test_strips_markdown_fences(monkeypatch):
    llm, _ = _make_llm(monkeypatch, ['```json\n{"ok": true}\n```'])
    assert await llm.generate("prompt") == {"ok": True}


@pytest.mark.asyncio
async def test_gives_up_after_max_retries(monkeypatch):
    llm, fake = _make_llm(monkeypatch, ["nope", "still nope", "final nope"], retries=3)
    with pytest.raises(InvalidLLMJSON):
        await llm.generate("prompt")
    assert len(fake.messages.calls) == 3
