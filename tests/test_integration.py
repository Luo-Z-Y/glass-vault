"""Cross-module checks: the modules built in parallel still fit together.

Each module has its own tests against docs/CONTRACTS.md. These tests cover the
seams between them, above all the HTTP split, where the agent zone only sees
the gateway through ``GatewayClient`` and a ``BankView`` rebuilt from JSON.
"""

from __future__ import annotations

import importlib
import pkgutil
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

import glassvault
from glassvault.bank import Bank
from glassvault.context import build_context, market_symbols
from glassvault.crypto import DeviceKey, challenge_digest
from glassvault.drafter import PLAN_TOOL, plan_to_tool_args
from glassvault.gateway import Gateway
from glassvault.llm import ScriptedLLM, tool_response
from glassvault.pipeline import Pipeline
from glassvault.rogue import RogueLLM
from glassvault.schema import Plan, Turn
from glassvault.services.gateway_api import GatewayClient, build_gateway, create_gateway_app
from glassvault.validator import RECORD_TOOL

BRIEF = "Move a grand to my joint account, buy DBS shares with half of it, and pay my SP bill."


@pytest.fixture
def http(gateway: Gateway):
    with TestClient(create_gateway_app(gateway)) as client:
        yield client


@pytest.fixture
def remote(http: TestClient) -> GatewayClient:
    return GatewayClient(http)


def scripted(plan: Plan, tool: str, name: str) -> ScriptedLLM:
    return ScriptedLLM([tool_response(tool, plan_to_tool_args(plan))] * 3, name=name)


# --- every module imports ------------------------------------------------------------


def _modules() -> list[str]:
    return sorted(
        m.name for m in pkgutil.walk_packages(glassvault.__path__, "glassvault.")
        if not m.name.endswith("__main__") and not m.name.startswith("glassvault.web")
    )


@pytest.mark.parametrize("name", _modules())
def test_every_module_imports(name: str) -> None:
    importlib.import_module(name)


# --- gateway app factory -------------------------------------------------------------


def test_passkey_gateway_app_serves_health_and_view(bank: Bank, clock) -> None:
    gateway, _ = build_gateway(bank, clock=clock)
    with TestClient(create_gateway_app(gateway)) as client:
        health = client.get("/health")
        view = client.get("/view")
    assert health.status_code == 200 and health.json()["passkeys"] is True
    assert view.status_code == 200
    assert {x["symbol"] for x in view.json()["listings"]} == {"D05", "O39", "Z74"}


# --- the agent zone's view of the bank over HTTP ------------------------------------------


def test_remote_view_lists_the_same_symbols_as_the_local_view(remote: GatewayClient, bank: Bank) -> None:
    local = build_context(bank.view()).prompt_data["symbols"]
    over_http = build_context(remote.view()).prompt_data["symbols"]
    assert over_http == local
    assert {s["symbol"] for s in over_http} == {"D05", "O39", "Z74"}


def test_remote_view_quarantines_the_poisoned_reference(remote: GatewayClient) -> None:
    context = build_context(remote.view())
    prompt = context.prompt_json()
    assert "8123 4567" not in prompt and "SYSTEM" not in prompt
    assert any("8123 4567" in original for original in context.placeholders.values())


class _PublicMethodMarket:
    def listings(self) -> list[tuple[str, str]]:
        return [("D05", "DBS Group Holdings")]


class _PublicMappingMarket:
    listings = {"O39": ("OCBC Bank", Decimal("16.85"))}


class _PrivateMarket:
    _listings = {"Z74": ("Singtel", Decimal("3.91"))}


@pytest.mark.parametrize(
    ("market", "expected"),
    [(_PublicMethodMarket(), ["D05"]), (_PublicMappingMarket(), ["O39"]), (_PrivateMarket(), ["Z74"]), (object(), [])],
)
def test_market_symbols_reads_any_market_shape(market: object, expected: list[str]) -> None:
    assert market_symbols(market) == expected


# --- the full pipeline over the HTTP split --------------------------------------------------


def test_brief_example_executes_over_http(
    remote: GatewayClient, gateway: Gateway, phone: DeviceKey, brief_plan: Plan
) -> None:
    pipeline = Pipeline(
        remote, scripted(brief_plan, PLAN_TOOL, "drafter"), scripted(brief_plan, RECORD_TOOL, "validator")
    )
    outcome = pipeline.handle([Turn(role="customer", text=BRIEF)])

    assert outcome.kind == "confirm", [(t.stage, t.detail) for t in outcome.trace if not t.ok]
    assert [s.amount for s in outcome.plan.steps] == [Decimal("1000.00"), Decimal("481.44"), Decimal("142.35")]
    assert outcome.challenge.script_hash == outcome.confirmation.script_hash
    assert outcome.challenge.policy_decision_id == outcome.policy.id

    receipts = remote.execute(nonce=outcome.challenge.nonce, signature=phone.sign(challenge_digest(outcome.challenge)))
    assert [r.step_id for r in receipts] == ["t1", "t2", "t3"]
    balances = {a.id: a.balance for a in remote.view().accounts.values()}
    assert balances == {"SAV": Decimal("11257.65"), "JNT": Decimal("3718.56")}
    assert remote.audit()["chain_intact"]


def test_rogue_drafter_over_http_moves_nothing(remote: GatewayClient, gateway: Gateway, brief_plan: Plan) -> None:
    view = remote.view()
    drafter = RogueLLM(scripted(brief_plan, PLAN_TOOL, "drafter"), "extra_transfer", view)
    pipeline = Pipeline(remote, drafter, scripted(brief_plan, RECORD_TOOL, "validator"))
    before = {a.id: a.balance for a in view.accounts.values()}

    outcome = pipeline.handle([Turn(role="customer", text=BRIEF)])

    assert outcome.kind == "frozen"
    assert outcome.challenge is None
    assert {a.id: a.balance for a in remote.view().accounts.values()} == before
    assert gateway.history() == ()
