"""The only door to a language model.

Everything above this module (drafter, validator, rogue mode) talks to an
``LLM``: a list of chat messages in, text and tool calls out. That keeps the
model swappable (Tencent Hunyuan, DeepSeek or OpenAI through the same
OpenAI-compatible API) and lets every test run offline against a
``ScriptedLLM``.

Nothing here is a security control. Model output is untrusted: tool-call
arguments are parsed leniently so a formatting slip gets a repair round
instead of a crash, but what they *mean* is checked by deterministic code
further down the pipeline.
"""

from __future__ import annotations

import copy
import json
import os
import re
import threading
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable
from urllib.parse import urlparse

import openai

Role = Literal["drafter", "validator"]
DEFAULT_TIMEOUT = 30.0  # seconds; a live demo should fail fast rather than hang


class LLMError(Exception):
    """The model could not be reached or returned something unusable."""


class LLMConfigError(LLMError):
    """Required GV_* settings are missing or malformed."""


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]
    id: str | None = None
    parse_error: str | None = None  # set when the model's arguments were not a JSON object


@dataclass(frozen=True)
class LLMResponse:
    text: str | None
    tool_calls: tuple[ToolCall, ...] = ()
    usage: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class LLM(Protocol):
    name: str

    def chat(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        temperature: float = 0.0,
    ) -> LLMResponse: ...


# --- Parsing model output -----------------------------------------------------

_FENCE = re.compile(r"^```[a-zA-Z0-9_-]*\s*(.*?)\s*```$", re.S)
_TRAILING_COMMA = re.compile(r",\s*([}\]])")


def parse_arguments(raw: str | Mapping[str, Any] | None) -> tuple[dict[str, Any], str | None]:
    """Tool-call arguments as a dict, plus an error message if they were unusable.

    Models get the JSON slightly wrong in predictable ways: code fences, a
    JSON string wrapped in another JSON string, prose around the object, or
    trailing commas. Those are repaired here; anything else is reported so the
    caller can ask the model to try again.
    """
    if raw is None:
        return {}, None
    if isinstance(raw, Mapping):
        return dict(raw), None
    text = raw.strip()
    if not text:
        return {}, None
    if fenced := _FENCE.match(text):
        text = fenced.group(1)

    candidates = [text]
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start : end + 1])
    candidates += [_TRAILING_COMMA.sub(r"\1", c) for c in list(candidates)]

    first_error: json.JSONDecodeError | None = None
    for candidate in candidates:
        try:
            value = json.loads(candidate, strict=False)
        except json.JSONDecodeError as e:
            first_error = first_error or e
            continue
        if isinstance(value, str):  # double-encoded: "{\"steps\": ...}"; each level is shorter
            return parse_arguments(value)
        if isinstance(value, dict):
            return value, None
        return {}, f"arguments must be a JSON object, got {type(value).__name__}"
    assert first_error is not None
    snippet = text if len(text) <= 120 else text[:117] + "..."
    return {}, f"arguments are not valid JSON ({first_error.msg} at char {first_error.pos}): {snippet}"


def response_from_completion(completion: Any) -> LLMResponse:
    """Map an OpenAI-SDK ``ChatCompletion`` to our provider-neutral ``LLMResponse``."""
    usage = completion.usage.model_dump(exclude_none=True) if getattr(completion, "usage", None) else {}
    if not completion.choices:
        return LLMResponse(text=None, usage=usage)
    message = completion.choices[0].message

    calls: list[ToolCall] = []
    for tc in message.tool_calls or ():
        function = getattr(tc, "function", None)
        if function is None:  # custom (non-function) tools are never offered
            continue
        arguments, error = parse_arguments(function.arguments)
        calls.append(ToolCall(function.name, arguments, id=tc.id, parse_error=error))
    legacy = getattr(message, "function_call", None)
    if not calls and legacy is not None:  # pre-"tools" function calling, still used by some endpoints
        arguments, error = parse_arguments(legacy.arguments)
        calls.append(ToolCall(legacy.name, arguments, parse_error=error))

    return LLMResponse(text=message.content or None, tool_calls=tuple(calls), usage=usage)


# --- Real endpoint ------------------------------------------------------------


class OpenAICompatLLM(LLM):
    """Any OpenAI-compatible chat-completions endpoint with tool calling.

    Some compatible endpoints reject ``tool_choice="required"``. The first
    such rejection downgrades this client to ``"auto"``; the drafter's repair
    round then handles a model that answers in text instead of calling a tool.
    """

    def __init__(
        self,
        *,
        model: str,
        base_url: str | None = None,
        api_key: str | None = None,
        name: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = 2,
        extra_body: Mapping[str, Any] | None = None,
        client: Any = None,
    ):
        self.model = model
        host = urlparse(base_url).hostname if base_url else None
        self.name = name or (f"{model}@{host}" if host else model)
        self._extra_body = dict(extra_body) if extra_body else None
        self._required_supported = True
        if client is None:
            client = openai.OpenAI(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=max_retries)
        self._client = client

    def chat(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        temperature: float = 0.0,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": temperature}
        if tools:
            kwargs["tools"] = tools
            if tool_choice is not None:
                if tool_choice == "required" and not self._required_supported:
                    tool_choice = "auto"
                kwargs["tool_choice"] = tool_choice
        if self._extra_body:
            kwargs["extra_body"] = self._extra_body

        try:
            try:
                completion = self._client.chat.completions.create(**kwargs)
            except openai.BadRequestError:
                if kwargs.get("tool_choice") != "required":
                    raise
                kwargs["tool_choice"] = "auto"
                completion = self._client.chat.completions.create(**kwargs)
                self._required_supported = False  # the retry worked, so "required" was the problem
        except openai.OpenAIError as e:
            raise LLMError(f"{self.name}: {type(e).__name__}: {e}") from e
        return response_from_completion(completion)


def llm_from_env(role: Role, *, environ: Mapping[str, str] | None = None) -> LLM:
    """Build the drafter's or the validator's model from GV_* settings.

    Reads the process environment, falling back to a ``.env`` file found from
    the working directory upwards (the environment wins). Pass ``environ`` to
    use exactly those settings and nothing else (tests).

    The validator may use its own endpoint (``GV_VALIDATOR_BASE_URL`` /
    ``GV_VALIDATOR_API_KEY``) so it can be a different model family from the
    drafter: two models are less likely to share one blind spot.
    """
    if role not in ("drafter", "validator"):
        raise ValueError(f"role must be 'drafter' or 'validator', not {role!r}")
    env = dict(environ) if environ is not None else _env_with_dotenv()

    def get(key: str) -> str | None:
        value = (env.get(key) or "").strip()
        return value or None

    prefix = "GV_DRAFTER" if role == "drafter" else "GV_VALIDATOR"
    model = get(f"{prefix}_MODEL")
    base_url = (get("GV_VALIDATOR_BASE_URL") if role == "validator" else None) or get("GV_LLM_BASE_URL")
    api_key = (get("GV_VALIDATOR_API_KEY") if role == "validator" else None) or get("GV_LLM_API_KEY")
    missing = [
        key
        for key, value in ((f"{prefix}_MODEL", model), ("GV_LLM_BASE_URL", base_url), ("GV_LLM_API_KEY", api_key))
        if value is None
    ]
    if missing:
        raise LLMConfigError(
            f"cannot build the {role} model: set {', '.join(missing)} (see .env.example)"
        )
    if role == "validator" and get("GV_VALIDATOR_BASE_URL") and not get("GV_VALIDATOR_API_KEY"):
        # Never send the drafter's key to a different provider.
        raise LLMConfigError("GV_VALIDATOR_BASE_URL is set, so set GV_VALIDATOR_API_KEY too")

    extra_body = None
    if raw := get(f"{prefix}_EXTRA_BODY"):
        try:
            extra_body = json.loads(raw)
        except json.JSONDecodeError as e:
            raise LLMConfigError(f"{prefix}_EXTRA_BODY is not valid JSON: {e.msg}") from e
        if not isinstance(extra_body, dict):
            raise LLMConfigError(f"{prefix}_EXTRA_BODY must be a JSON object")
    try:
        timeout = float(get("GV_LLM_TIMEOUT") or DEFAULT_TIMEOUT)
    except ValueError as e:
        raise LLMConfigError("GV_LLM_TIMEOUT must be a number of seconds") from e

    return OpenAICompatLLM(
        model=model, base_url=base_url, api_key=api_key, timeout=timeout, extra_body=extra_body
    )


def _env_with_dotenv() -> dict[str, str]:
    from dotenv import dotenv_values, find_dotenv

    path = find_dotenv(usecwd=True)
    from_file = {k: v for k, v in dotenv_values(path).items() if v is not None} if path else {}
    # An exported-but-empty variable should not hide a value in .env.
    return {**from_file, **{k: v for k, v in os.environ.items() if v.strip()}}


# --- Test double ----------------------------------------------------------------

ScriptStep = LLMResponse | Callable[[list[dict]], LLMResponse]


@dataclass(frozen=True)
class RecordedCall:
    messages: list[dict]
    tools: list[dict] | None
    tool_choice: str | dict | None
    temperature: float


class ScriptedLLM(LLM):
    """Replays queued responses in order and records every call (deep-copied).

    A queued item may be a callable ``messages -> LLMResponse`` for replies
    that depend on what the model was sent. Running out of script raises
    ``LLMError``, so a test notices an unexpected extra round-trip.
    """

    def __init__(self, script: Iterable[ScriptStep] = (), *, name: str = "scripted"):
        self.name = name
        self._script: deque[ScriptStep] = deque(script)
        self._lock = threading.Lock()
        self.calls: list[RecordedCall] = []

    def queue(self, *steps: ScriptStep) -> None:
        with self._lock:
            self._script.extend(steps)

    @property
    def remaining(self) -> int:
        return len(self._script)

    def chat(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        temperature: float = 0.0,
    ) -> LLMResponse:
        with self._lock:
            self.calls.append(
                RecordedCall(copy.deepcopy(messages), copy.deepcopy(tools), tool_choice, temperature)
            )
            if not self._script:
                raise LLMError(f"{self.name}: no scripted response left (call {len(self.calls)})")
            step = self._script.popleft()
        return step(copy.deepcopy(messages)) if callable(step) else step


def tool_response(
    name: str, arguments: Mapping[str, Any] | str, *, text: str | None = None, call_id: str | None = None
) -> LLMResponse:
    """A response that calls one tool. A str ``arguments`` is parsed like raw model output."""
    parsed, error = parse_arguments(arguments)
    return LLMResponse(text=text, tool_calls=(ToolCall(name, parsed, id=call_id, parse_error=error),))


def text_response(text: str) -> LLMResponse:
    return LLMResponse(text=text)
