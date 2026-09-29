"""The model adapter: robust parsing, provider mapping, env config, and the scripted double."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import httpx2
import openai
import pytest
from openai.types.chat import ChatCompletion

from glassvault.llm import (
    LLM,
    LLMConfigError,
    LLMError,
    LLMResponse,
    OpenAICompatLLM,
    ScriptedLLM,
    ToolCall,
    llm_from_env,
    parse_arguments,
    response_from_completion,
    text_response,
    tool_response,
)

GV_VARS = (
    "GV_LLM_BASE_URL", "GV_LLM_API_KEY", "GV_DRAFTER_MODEL", "GV_VALIDATOR_MODEL",
    "GV_VALIDATOR_BASE_URL", "GV_VALIDATOR_API_KEY", "GV_LLM_TIMEOUT",
    "GV_DRAFTER_EXTRA_BODY", "GV_VALIDATOR_EXTRA_BODY",
)


def completion(message: dict, usage: dict | None = None, choices: bool = True) -> ChatCompletion:
    return ChatCompletion.model_validate({
        "id": "cmpl-1", "object": "chat.completion", "created": 1, "model": "m",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", **message}}] if choices else [],
        "usage": usage,
    })


def fn_call(name: str, arguments: str, call_id: str = "call_1") -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


class FakeCompletions:
    """Stands in for `client.chat.completions`; records kwargs, replays results or raises."""

    def __init__(self, *results):
        self.results = list(results)
        self.kwargs: list[dict] = []

    def create(self, **kwargs):
        self.kwargs.append(kwargs)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def fake_client(*results) -> tuple[SimpleNamespace, FakeCompletions]:
    completions = FakeCompletions(*results)
    return SimpleNamespace(chat=SimpleNamespace(completions=completions)), completions


def bad_request(message: str = "tool_choice 'required' is not supported") -> openai.BadRequestError:
    request = httpx2.Request("POST", "https://llm.example/v1/chat/completions")
    return openai.BadRequestError(message, response=httpx2.Response(400, request=request), body=None)


# --- parse_arguments ------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ('{"steps": [1, 2]}', {"steps": [1, 2]}),
        ({"a": 1}, {"a": 1}),
        ('```json\n{"a": 1}\n```', {"a": 1}),
        ('```\n{"a": 1}\n```', {"a": 1}),
        ('"{\\"a\\": 1}"', {"a": 1}),  # a JSON string holding JSON
        ('Here is the plan: {"a": {"b": 2}} Hope that helps!', {"a": {"b": 2}}),
        ('{"a": [1, 2,], "b": 3,}', {"a": [1, 2], "b": 3}),  # trailing commas
        ('{"text": "line one\nline two"}', {"text": "line one\nline two"}),  # raw control character
        ("", {}),
        ("   ", {}),
        (None, {}),
    ],
)
def test_parse_arguments_repairs_common_model_slips(raw, expected):
    assert parse_arguments(raw) == (expected, None)


@pytest.mark.parametrize(
    "raw, fragment",
    [
        ('{"steps": [', "not valid JSON"),
        ("definitely not json", "not valid JSON"),
        ("[1, 2]", "must be a JSON object"),
        ("42", "must be a JSON object"),
    ],
)
def test_parse_arguments_reports_what_it_cannot_repair(raw, fragment):
    arguments, error = parse_arguments(raw)
    assert arguments == {} and fragment in error


def test_parse_error_quotes_at_most_a_snippet():
    _, error = parse_arguments("{" + "x" * 500)
    assert len(error) < 250


# --- response_from_completion ---------------------------------------------------------


def test_maps_tool_calls_text_and_usage():
    response = response_from_completion(completion(
        {"content": "Drafting now.", "tool_calls": [
            fn_call("submit_plan", '{"steps": []}', "call_a"),
            fn_call("ask_clarification", '{"reason": "other"', "call_b"),
        ]},
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    ))
    assert response.text == "Drafting now."
    first, second = response.tool_calls
    assert first == ToolCall("submit_plan", {"steps": []}, id="call_a")
    assert second.name == "ask_clarification" and second.arguments == {} and "not valid JSON" in second.parse_error
    assert response.usage == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}


def test_text_only_and_empty_responses():
    assert response_from_completion(completion({"content": "Hello"})) == LLMResponse("Hello", (), {})
    assert response_from_completion(completion({"content": ""})).text is None
    assert response_from_completion(completion({"content": None}, choices=False)) == LLMResponse(None)


def test_legacy_function_call_is_treated_as_a_tool_call():
    response = response_from_completion(completion(
        {"content": None, "function_call": {"name": "submit_plan", "arguments": '{"a": 1}'}}
    ))
    assert response.tool_calls == (ToolCall("submit_plan", {"a": 1}),)


# --- OpenAICompatLLM ---------------------------------------------------------------------


def test_sends_an_openai_compatible_request():
    client, calls = fake_client(completion({"content": None, "tool_calls": [fn_call("t", "{}")]}))
    llm = OpenAICompatLLM(model="hunyuan-turbos-latest", base_url="https://api.hunyuan.cloud.tencent.com/v1",
                          extra_body={"enable_enhancement": False}, client=client)
    tools = [{"type": "function", "function": {"name": "t", "parameters": {"type": "object"}}}]
    messages = [{"role": "user", "content": "hi"}]

    response = llm.chat(messages, tools=tools, tool_choice="required", temperature=0.0)

    assert response.tool_calls[0].name == "t"
    assert calls.kwargs == [{
        "model": "hunyuan-turbos-latest", "messages": messages, "temperature": 0.0,
        "tools": tools, "tool_choice": "required", "extra_body": {"enable_enhancement": False},
    }]
    assert llm.name == "hunyuan-turbos-latest@api.hunyuan.cloud.tencent.com"
    assert isinstance(llm, LLM)


def test_no_tools_means_no_tool_choice():
    client, calls = fake_client(completion({"content": "ok"}))
    OpenAICompatLLM(model="m", client=client).chat([{"role": "user", "content": "x"}], tool_choice="required")
    assert "tools" not in calls.kwargs[0] and "tool_choice" not in calls.kwargs[0]


def test_falls_back_to_auto_when_required_is_rejected_and_remembers():
    ok = completion({"content": "ok"})
    client, calls = fake_client(bad_request(), ok, ok)
    llm = OpenAICompatLLM(model="m", client=client)
    tools = [{"type": "function", "function": {"name": "t", "parameters": {"type": "object"}}}]

    llm.chat([], tools=tools, tool_choice="required")
    llm.chat([], tools=tools, tool_choice="required")

    assert [k["tool_choice"] for k in calls.kwargs] == ["required", "auto", "auto"]


def test_an_unrelated_bad_request_does_not_downgrade_tool_choice():
    ok = completion({"content": "ok"})
    client, calls = fake_client(bad_request("context too long"), bad_request("context too long"), ok)
    llm = OpenAICompatLLM(model="m", client=client)
    tools = [{"type": "function", "function": {"name": "t", "parameters": {"type": "object"}}}]

    with pytest.raises(LLMError, match="context too long"):
        llm.chat([], tools=tools, tool_choice="required")
    llm.chat([], tools=tools, tool_choice="required")

    assert [k["tool_choice"] for k in calls.kwargs] == ["required", "auto", "required"]


def test_other_bad_requests_and_transport_errors_become_llm_errors():
    client, _ = fake_client(bad_request("context too long"))
    with pytest.raises(LLMError, match="context too long"):
        OpenAICompatLLM(model="m", client=client).chat([], tools=[{"type": "function"}], tool_choice="auto")

    request = httpx2.Request("POST", "https://llm.example/v1/chat/completions")
    client, _ = fake_client(openai.APIConnectionError(request=request))
    with pytest.raises(LLMError, match="APIConnectionError"):
        OpenAICompatLLM(model="m", client=client).chat([])


# --- llm_from_env -----------------------------------------------------------------------------

BASE_ENV = {
    "GV_LLM_BASE_URL": "https://api.hunyuan.cloud.tencent.com/v1",
    "GV_LLM_API_KEY": "sk-test-drafter",
    "GV_DRAFTER_MODEL": "hunyuan-turbos-latest",
    "GV_VALIDATOR_MODEL": "deepseek-chat",
}


def test_drafter_and_validator_models_from_env():
    drafter = llm_from_env("drafter", environ=BASE_ENV)
    validator = llm_from_env("validator", environ=BASE_ENV)
    assert isinstance(drafter, OpenAICompatLLM) and drafter.model == "hunyuan-turbos-latest"
    assert validator.model == "deepseek-chat"
    # Without overrides the validator shares the drafter's endpoint.
    assert str(validator._client.base_url).startswith("https://api.hunyuan.cloud.tencent.com/v1")


def test_validator_can_use_its_own_endpoint_and_key():
    env = {**BASE_ENV, "GV_VALIDATOR_BASE_URL": "https://api.deepseek.com", "GV_VALIDATOR_API_KEY": "sk-test-val"}
    validator = llm_from_env("validator", environ=env)
    assert str(validator._client.base_url).startswith("https://api.deepseek.com")
    assert validator._client.api_key == "sk-test-val"
    assert validator.name == "deepseek-chat@api.deepseek.com"
    assert llm_from_env("drafter", environ=env)._client.api_key == "sk-test-drafter"


def test_empty_override_falls_back_to_shared_settings():
    env = {**BASE_ENV, "GV_VALIDATOR_BASE_URL": "", "GV_VALIDATOR_API_KEY": "  "}
    assert llm_from_env("validator", environ=env)._client.api_key == "sk-test-drafter"


def test_drafter_key_is_never_sent_to_the_validators_other_provider():
    env = {**BASE_ENV, "GV_VALIDATOR_BASE_URL": "https://api.deepseek.com"}
    with pytest.raises(LLMConfigError, match="set GV_VALIDATOR_API_KEY"):
        llm_from_env("validator", environ=env)
    assert llm_from_env("drafter", environ=env).model == "hunyuan-turbos-latest"


def test_env_example_lists_every_setting():
    from pathlib import Path

    example = (Path(__file__).parents[1] / ".env.example").read_text()
    for var in GV_VARS:
        assert var in example, var
    assert "sk-" not in example  # no real keys


def test_optional_settings():
    env = {**BASE_ENV, "GV_LLM_TIMEOUT": "12.5", "GV_DRAFTER_EXTRA_BODY": '{"enable_enhancement": false}'}
    drafter = llm_from_env("drafter", environ=env)
    assert drafter._extra_body == {"enable_enhancement": False}
    assert drafter._client.timeout == 12.5
    assert llm_from_env("validator", environ=env)._extra_body is None


@pytest.mark.parametrize(
    "env, fragment",
    [
        ({}, "GV_DRAFTER_MODEL, GV_LLM_BASE_URL, GV_LLM_API_KEY"),
        ({**BASE_ENV, "GV_DRAFTER_MODEL": ""}, "GV_DRAFTER_MODEL"),
        ({**BASE_ENV, "GV_DRAFTER_EXTRA_BODY": "{nope"}, "not valid JSON"),
        ({**BASE_ENV, "GV_DRAFTER_EXTRA_BODY": "[1]"}, "must be a JSON object"),
        ({**BASE_ENV, "GV_LLM_TIMEOUT": "soon"}, "GV_LLM_TIMEOUT"),
    ],
)
def test_bad_config_fails_clearly(env, fragment):
    with pytest.raises(LLMConfigError, match=fragment.replace("[", r"\[")):
        llm_from_env("drafter", environ=env)


def test_unknown_role_is_rejected():
    with pytest.raises(ValueError):
        llm_from_env("executor", environ=BASE_ENV)  # type: ignore[arg-type]


def test_reads_dotenv_but_real_environment_wins(tmp_path, monkeypatch):
    for var in GV_VARS:
        monkeypatch.delenv(var, raising=False)
    (tmp_path / ".env").write_text(
        "GV_LLM_BASE_URL=https://dotenv.example/v1\nGV_LLM_API_KEY=sk-from-file\nGV_DRAFTER_MODEL=file-model\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GV_DRAFTER_MODEL", "env-model")

    llm = llm_from_env("drafter")

    assert llm.model == "env-model"
    assert llm._client.api_key == "sk-from-file"
    assert "GV_LLM_API_KEY" not in __import__("os").environ  # the file is read, not exported


# --- ScriptedLLM ---------------------------------------------------------------------------------


def test_scripted_llm_replays_in_order_and_records_calls():
    llm = ScriptedLLM([text_response("one"), tool_response("submit_plan", {"steps": []}, call_id="c1")])
    messages = [{"role": "user", "content": "hi"}]

    assert llm.chat(messages).text == "one"
    messages.append({"role": "assistant", "content": "later edit"})
    second = llm.chat(messages, tools=[{"x": 1}], tool_choice="required", temperature=0.3)

    assert second.tool_calls == (ToolCall("submit_plan", {"steps": []}, id="c1"),)
    assert llm.calls[0].messages == [{"role": "user", "content": "hi"}]  # snapshot, not a live reference
    assert llm.calls[1].tools == [{"x": 1}] and llm.calls[1].tool_choice == "required"
    assert llm.calls[1].temperature == 0.3
    assert llm.remaining == 0 and isinstance(llm, LLM)


def test_scripted_llm_callables_see_the_messages():
    llm = ScriptedLLM([lambda messages: text_response(f"{len(messages)} messages")])
    assert llm.chat([{"role": "user", "content": "a"}, {"role": "user", "content": "b"}]).text == "2 messages"


def test_scripted_llm_raises_when_the_script_runs_out():
    llm = ScriptedLLM([text_response("only")], name="drafter-script")
    llm.chat([])
    with pytest.raises(LLMError, match="drafter-script: no scripted response left"):
        llm.chat([])
    llm.queue(text_response("more"))
    assert llm.chat([]).text == "more"


def test_scripted_llm_is_safe_across_threads():
    llm = ScriptedLLM([text_response(str(i)) for i in range(50)])
    threads = [threading.Thread(target=llm.chat, args=([],)) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert llm.remaining == 0 and len(llm.calls) == 50


def test_tool_response_parses_raw_argument_strings():
    response = tool_response("submit_plan", '{"steps": [')
    assert response.tool_calls[0].arguments == {} and response.tool_calls[0].parse_error
