"""The gateway zone as an HTTP service: the only process that holds the Bank.

The agent zone is assumed compromised, so this service gives it nothing that
moves money. The agent can read a snapshot (``/view``), fetch share quotes and
execution history, ask for a challenge over a plan, and pass the customer's
passkey assertion on to ``/execute``. Every ledger write still goes through
``Gateway.execute``, which checks the signed challenge.

Choices worth knowing:

- One service lock wraps every call. ``Gateway`` locks its own methods, but
  FastAPI runs sync endpoints on a thread pool, and two things sit outside
  the gateway's lock: ``/view`` copying balances while a plan is being
  applied, and passkey enrolment changing the verifier the gateway reads.
- Adding a passkey needs a one-time enrolment code shown on the gateway's
  console. It stands in for the bank's onboarding channel (Singpass, a branch).
  Without it, a compromised agent could enrol its own software authenticator
  and then approve its own plans.
- The device is shown the gateway's own validated copy of the plan
  (``Gateway.pending_plan``), never the agent's, and that copy is what executes.
- Assertions are cut down to the WebAuthn fields before they reach the gateway,
  so the audit log stores what was verified, not extra JSON an agent attached.
- Every error, from the gateway or from this layer, is ``{code, status,
  message}``. Requests this layer refuses are audited as well.
- ``GatewayClient`` (bottom of the module) is the HTTP ``pipeline.GatewayPort``
  for the agent service. It rebuilds a ``BankView`` whose market fetches quotes
  from here, so an equity step's ``quote_id`` is one this gateway issued.

Run it with ``uv run python -m glassvault.services.gateway_api`` and open
http://localhost:8002/dev for the passkey dev harness.
"""

from __future__ import annotations

import argparse
import logging
import os
import secrets
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any, Literal, NoReturn

import httpx
import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from ..audit import GENESIS, AuditLog
from ..bank import Account, Bank, BankView, Biller, PastPayment, Payee, Quote, seed_bank
from ..crypto import challenge_digest
from ..evaluator import EvaluationError, resolve
from ..gateway import STATUS, Execution, Gateway, GatewayError, Receipt
from ..schema import BillPayment, Challenge, FixedAmount, OwnTransfer, Plan, ResolvedPlan, ResolvedStep
from .webauthn_verifier import EnrolmentError, WebAuthnVerifier, approval_options

DEFAULT_RP_ID = "localhost"
DEFAULT_PORT = 8002
DEFAULT_ORIGIN = f"http://localhost:{DEFAULT_PORT}"
DEV_PAGE = Path(__file__).parent / "static" / "dev.html"

log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


# --- Wire models ------------------------------------------------------------------


class _Wire(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChallengeRequest(_Wire):
    plan: ResolvedPlan
    script_hash: str | None = Field(default=None, max_length=128)
    policy_decision_id: str | None = Field(default=None, max_length=128)


B64Url = Annotated[str, Field(max_length=8192, pattern=r"^[A-Za-z0-9_-]*={0,2}$")]


class AssertionResponseJSON(BaseModel):
    model_config = ConfigDict(extra="ignore")

    clientDataJSON: B64Url
    authenticatorData: B64Url
    signature: B64Url
    userHandle: B64Url | None = None


class AssertionJSON(BaseModel):
    """The browser's AuthenticationResponseJSON, cut down to what verification reads."""

    model_config = ConfigDict(extra="ignore")

    id: B64Url
    rawId: B64Url
    type: Literal["public-key"]
    response: AssertionResponseJSON


class ExecuteRequest(_Wire):
    # Optional: the gateway executes its own copy; a plan sent here must hash to what was approved.
    plan: ResolvedPlan | None = None
    nonce: str | None = Field(default=None, max_length=128)
    assertion: AssertionJSON | None = None
    signature: str | None = Field(
        default=None,
        max_length=512,
        pattern=r"^(?:[0-9a-f]{2})+$",
        description="hex Ed25519 signature: only for a gateway built with DeviceKeyVerifier (tests, CLI)",
    )

    @model_validator(mode="after")
    def _one_proof(self) -> ExecuteRequest:
        if self.assertion is not None and self.signature is not None:
            raise ValueError("send a passkey assertion or a device signature, not both")
        return self

    def proof(self) -> Any:
        if self.assertion is not None:
            return self.assertion.model_dump(exclude_none=True)
        if self.signature is not None:
            return bytes.fromhex(self.signature)
        return None


class NonceRequest(_Wire):
    nonce: str = Field(min_length=1, max_length=128)


class QuoteRequest(_Wire):
    symbol: str = Field(min_length=1, max_length=16)


class EnrolmentRequest(_Wire):
    enrolment_code: str = Field(max_length=64)
    customer_id: str | None = Field(default=None, max_length=64)


class EnrolmentVerifyRequest(EnrolmentRequest):
    credential: dict[str, Any]


# --- Snapshot wire format -------------------------------------------------------------


def snapshot_json(view: BankView) -> dict[str, Any]:
    """The ``/view`` body.

    Untrusted text (payee nicknames, biller references) goes out raw: the agent
    side quarantines it before anything reaches an LLM, and it needs the
    original to do that.
    """
    # Market has no public way to list symbols yet.
    listings: Mapping[str, tuple[str, Decimal]] = view.market._listings
    return {
        "customer_id": view.customer_id,
        "customer_name": view.customer_name,
        "kyc_tier": view.kyc_tier,
        "kyc_status": view.kyc_status,
        "investment_eligible": view.investment_eligible,
        "accounts": [
            {"id": a.id, "name": a.name, "balance": str(a.balance)} for a in view.accounts.values()
        ],
        "payees": [
            {
                "id": p.id,
                "legal_name": p.legal_name,
                "nickname": p.nickname,
                "paynow_proxy": p.paynow_proxy,
                "added_on": p.added_on.isoformat(),
                "history": [{"on": h.on.isoformat(), "amount": str(h.amount)} for h in p.history],
            }
            for p in view.payees.values()
        ],
        "billers": [
            {"id": b.id, "name": b.name, "reference": b.reference, "amount_due": str(b.amount_due)}
            for b in view.billers.values()
        ],
        "listings": [
            {"symbol": symbol, "name": name, "price": str(price)}
            for symbol, (name, price) in sorted(listings.items())
        ],
    }


def view_from_snapshot(data: Mapping[str, Any], market: Any) -> BankView:
    """Rebuild a read-only ``BankView`` from a ``/view`` body; ``market`` supplies quotes."""
    accounts = {a["id"]: Account(a["id"], a["name"], Decimal(a["balance"])) for a in data["accounts"]}
    payees = {
        p["id"]: Payee(
            p["id"],
            p["legal_name"],
            p["nickname"],
            p["paynow_proxy"],
            date.fromisoformat(p["added_on"]),
            tuple(PastPayment(date.fromisoformat(h["on"]), Decimal(h["amount"])) for h in p["history"]),
        )
        for p in data["payees"]
    }
    billers = {
        b["id"]: Biller(b["id"], b["name"], b["reference"], Decimal(b["amount_due"]))
        for b in data["billers"]
    }
    return BankView(
        customer_id=data["customer_id"],
        customer_name=data["customer_name"],
        kyc_tier=data["kyc_tier"],
        kyc_status=data["kyc_status"],
        investment_eligible=data["investment_eligible"],
        accounts=MappingProxyType(accounts),
        payees=MappingProxyType(payees),
        billers=MappingProxyType(billers),
        market=market,
    )


def quote_json(quote: Quote) -> dict[str, Any]:
    return {
        "quote_id": quote.quote_id,
        "symbol": quote.symbol,
        "name": quote.name,
        "price": str(quote.price),
        "issued_at": quote.issued_at.isoformat(),
        "expires_at": quote.expires_at.isoformat(),
    }


# --- Service ------------------------------------------------------------------------


class ApiError(Exception):
    """A refusal by this HTTP layer; the gateway's own refusals are ``GatewayError``."""

    def __init__(self, code: str, status: int, message: str):
        super().__init__(f"{status} {code}: {message}")
        self.code = code
        self.status = status
        self.message = message


class EnrolmentCode:
    """One-time code that authorises adding a passkey.

    Stands in for the bank's onboarding channel (Singpass, a branch, a letter):
    it is shown on the gateway's console and never passes through the agent
    zone. Each code works for one enrolment; then a new one is issued.
    """

    def __init__(self, code: str | None = None):
        self._code = code or self._new()

    @staticmethod
    def _new() -> str:
        raw = secrets.token_hex(6)
        return "-".join(raw[i : i + 4] for i in range(0, len(raw), 4))

    @property
    def current(self) -> str:
        return self._code

    def matches(self, candidate: str) -> bool:
        return secrets.compare_digest(candidate.strip().encode(), self._code.encode())

    def rotate(self) -> str:
        self._code = self._new()
        return self._code


# Dev harness only: a fixed plan so /dev works before the agent service exists.
# It is resolved by the deterministic evaluator and still needs the customer's passkey.
SAMPLE_PLAN = Plan(steps=[
    OwnTransfer(id="t1", from_account="SAV", to_account="JNT", amount=FixedAmount(value=Decimal("50"))),
    BillPayment(id="t2", from_account="SAV", biller_id="SINGTEL", amount=FixedAmount(value=Decimal("58.90"))),
])


def build_gateway(
    bank: Bank,
    *,
    rp_id: str = DEFAULT_RP_ID,
    origin: str | Sequence[str] = DEFAULT_ORIGIN,
    audit: AuditLog | None = None,
    clock: Callable[[], datetime] = _utcnow,
) -> tuple[Gateway, WebAuthnVerifier]:
    """A gateway that approves with passkeys; both share one audit log and clock."""
    audit = audit if audit is not None else AuditLog()
    verifier = WebAuthnVerifier(rp_id=rp_id, origin=origin, audit=audit, clock=clock)
    return Gateway(bank, audit, verifier=verifier, clock=clock), verifier


def passkey_verifier(gateway: Gateway) -> WebAuthnVerifier | None:
    """The gateway's ``WebAuthnVerifier``, if it approves with passkeys.

    Enrolment must write to the same instance the gateway reads from, and
    ``Gateway`` keeps its verifier private, hence the attribute lookup.
    """
    found = getattr(gateway, "verifier", None) or getattr(gateway, "_verifier", None)
    return found if isinstance(found, WebAuthnVerifier) else None


def _error(code: str, status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"code": code, "status": status, "message": message})


def _where(error: Mapping[str, Any]) -> str:
    # Locations can include attacker-chosen keys; keep them short.
    return ".".join(str(part) for part in error.get("loc", ()))[:80]


def create_gateway_app(
    gateway: Gateway,
    *,
    rp_id: str = DEFAULT_RP_ID,
    origin: str | Sequence[str] = DEFAULT_ORIGIN,
    enrolment_code: str | None = None,
    dev_tools: bool = False,
) -> FastAPI:
    """HTTP front for ``gateway``.

    Passkey approval needs a gateway built with a ``WebAuthnVerifier`` (see
    ``build_gateway``) whose RP ID and origins match these. With any other
    verifier, ``/execute`` takes a hex ``signature`` instead and the passkey
    endpoints answer 501. ``enrolment_code`` fixes the first enrolment code
    (default: random). ``dev_tools`` adds ``POST /dev/sample-challenge``.
    """
    origins = (origin,) if isinstance(origin, str) else tuple(origin)
    verifier = passkey_verifier(gateway)
    if verifier is not None and (verifier.rp_id, set(verifier.origins)) != (rp_id, set(origins)):
        # Caught here rather than as a mysterious SIGNATURE_INVALID on demo day.
        raise ValueError(
            f"gateway verifier expects rp_id={verifier.rp_id!r} origins={verifier.origins}, "
            f"app was given rp_id={rp_id!r} origins={origins}"
        )
    bank = gateway.bank
    lock = threading.RLock()
    enrolment = EnrolmentCode(enrolment_code)
    # The gateway keeps the plan under each nonce but not the Challenge, which
    # the device needs to recompute what it signs.
    issued: dict[str, Challenge] = {}

    app = FastAPI(
        title="Glass Vault gateway",
        description="The only service that can move money. Executes only customer-signed plans.",
        version="0.1.0",
    )
    app.state.gateway = gateway
    app.state.verifier = verifier
    app.state.enrolment = enrolment
    app.state.ledger_lock = lock
    app.add_middleware(
        CORSMiddleware, allow_origins=list(origins), allow_methods=["GET", "POST"], allow_headers=["content-type"]
    )

    def audit(kind: str, data: dict[str, Any]) -> None:
        with lock:
            gateway.audit.append(kind, data, at=gateway.now())

    def refuse(code: str, status: int, message: str, **data: Any) -> NoReturn:
        audit("REQUEST_REFUSED", {"code": code, **data})
        raise ApiError(code, status, message)

    def approval_body(challenge: Challenge) -> dict[str, Any]:
        digest = challenge_digest(challenge)
        if verifier is not None:
            options = verifier.authentication_options(challenge.customer_id, digest)
        else:
            options = approval_options(rp_id, digest)
        return {"challenge": challenge.model_dump(mode="json"), "webauthn_options": options}

    def issue(plan: ResolvedPlan, **binding: str | None) -> dict[str, Any]:
        with lock:
            challenge = gateway.issue_challenge(plan, **binding)
            issued[challenge.nonce] = challenge
            return approval_body(challenge)

    def require_passkeys() -> WebAuthnVerifier:
        if verifier is None:
            raise ApiError("PASSKEYS_NOT_CONFIGURED", 501, "this gateway approves with raw device keys")
        return verifier

    def enrolling_customer(req: EnrolmentRequest) -> str:
        customer_id = req.customer_id or bank.customer_id
        if customer_id != bank.customer_id:
            refuse("UNKNOWN_CUSTOMER", 404, "no such customer", path="enrolment", customer_id=customer_id)
        if not enrolment.matches(req.enrolment_code):
            refuse("ENROLMENT_CODE_INVALID", 403, "enrolment code is wrong or already used",
                   customer_id=customer_id)
        return customer_id

    @app.exception_handler(GatewayError)
    def _gateway_error(request: Request, exc: GatewayError) -> JSONResponse:
        return _error(exc.code, exc.status, exc.message)

    @app.exception_handler(ApiError)
    def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return _error(exc.code, exc.status, exc.message)

    @app.exception_handler(RequestValidationError)
    def _invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = list(exc.errors())
        audit("REQUEST_REFUSED", {
            "code": "INVALID_REQUEST",
            "path": request.url.path,
            "errors": [_where(e) for e in errors[:5]],
        })
        message = "; ".join(f"{_where(e)}: {e.get('msg', 'invalid')}" for e in errors[:3])
        return _error("INVALID_REQUEST", 422, message or "invalid request")

    # --- Read side (what the agent zone may see) ---

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "service": "gateway",
            "customer_id": bank.customer_id,
            "rp_id": rp_id,
            "origins": list(origins),
            "passkeys": verifier is not None,
            "dev_tools": dev_tools,
        }

    @app.get("/now")
    def now() -> dict[str, Any]:
        """The gateway's clock: quotes, challenges and velocity windows all use it."""
        return {"now": gateway.now().isoformat()}

    @app.get("/view")
    def view() -> dict[str, Any]:
        with lock:
            return {**snapshot_json(bank.view()), "as_of": gateway.now().isoformat()}

    @app.post("/quotes")
    def quote(req: QuoteRequest) -> dict[str, Any]:
        with lock:
            issued_quote = bank.market.quote(req.symbol)  # stamped by the gateway's clock
        if issued_quote is None:
            raise ApiError("UNKNOWN_SYMBOL", 404, f"no listing {req.symbol!r}")
        return quote_json(issued_quote)

    @app.get("/history")
    def history(since: AwareDatetime | None = None) -> dict[str, Any]:
        with lock:
            executions = gateway.history(since)
        return {
            "executions": [
                {"at": e.at.isoformat(), "step": e.step.model_dump(mode="json")} for e in executions
            ]
        }

    @app.get("/audit")
    def audit_log(since: int = Query(default=0, ge=0)) -> dict[str, Any]:
        with lock:
            entries = gateway.audit.entries
            broken = gateway.audit.first_broken()
        return {
            "chain_intact": broken is None,
            "first_broken": broken,
            "length": len(entries),
            "head": entries[-1].hash if entries else GENESIS,
            "entries": [asdict(e) for e in entries[since:]],
        }

    # --- Approval and execution ---

    @app.post("/challenges")
    def challenges(req: ChallengeRequest) -> dict[str, Any]:
        # The gateway validates the plan (labels, live balances) and audits refusals.
        return issue(req.plan, script_hash=req.script_hash, policy_decision_id=req.policy_decision_id)

    @app.post("/webauthn/approve/options")
    def approve_options(req: NonceRequest) -> dict[str, Any]:
        """What the device needs to approve a pending challenge: the plan and the passkey prompt."""
        with lock:
            challenge = issued.get(req.nonce)
            plan = gateway.pending_plan(req.nonce)  # the gateway's own copy, never the agent's
            if challenge is None or plan is None:
                issued.pop(req.nonce, None)
                raise GatewayError("UNKNOWN_CHALLENGE", "no such approval request")
            if gateway.now() > challenge.expires_at:
                # Don't ask for Face ID on an approval that can no longer execute.
                raise GatewayError("CHALLENGE_EXPIRED", "approval request expired")
            return {**approval_body(challenge), "plan": plan.model_dump(mode="json")}

    @app.post("/execute")
    def execute(req: ExecuteRequest) -> dict[str, Any]:
        with lock:
            receipts = gateway.execute(req.plan, nonce=req.nonce, signature=req.proof())
            issued.pop(req.nonce or "", None)
        return {"receipts": [asdict(r) for r in receipts]}

    # --- Passkey enrolment ---

    @app.post("/webauthn/register/options")
    def register_options(req: EnrolmentRequest) -> dict[str, Any]:
        passkeys = require_passkeys()
        customer_id = enrolling_customer(req)
        with lock:
            options = passkeys.registration_options(customer_id, user_name=bank.customer_name)
        return {"customer_id": customer_id, "webauthn_options": options}

    @app.post("/webauthn/register/verify")
    def register_verify(req: EnrolmentVerifyRequest) -> dict[str, Any]:
        passkeys = require_passkeys()
        customer_id = enrolling_customer(req)
        with lock:
            try:
                passkey = passkeys.verify_registration(customer_id, req.credential)
            except EnrolmentError as e:
                raise ApiError("ENROLMENT_REJECTED", 400, str(e)) from e
            next_code = enrolment.rotate()
        log.warning("Passkey enrolled for %s. Next enrolment code: %s", customer_id, next_code)
        return {"passkey": passkey.summary()}

    @app.get("/webauthn/passkeys")
    def list_passkeys() -> dict[str, Any]:
        passkeys = require_passkeys()
        with lock:
            return {"passkeys": [p.summary() for p in passkeys.passkeys(bank.customer_id)]}

    # --- Dev harness ---

    @app.get("/dev", include_in_schema=False)
    def dev_page() -> FileResponse:
        return FileResponse(DEV_PAGE, media_type="text/html")

    if dev_tools:

        @app.post("/dev/sample-challenge")
        def sample_challenge() -> dict[str, Any]:
            with lock:
                try:
                    plan = resolve(SAMPLE_PLAN, bank.view(), now=gateway.now())
                except EvaluationError as e:
                    raise ApiError("SAMPLE_UNAVAILABLE", 409, str(e)) from e
                return {**issue(plan), "plan": plan.model_dump(mode="json")}

    return app


# --- Client (used by the agent service) ------------------------------------------------


class GatewayRequestError(Exception):
    """A refusal that is not a ``GatewayError`` (bad request, enrolment, unknown symbol...)."""

    def __init__(self, code: str, status: int, message: str):
        super().__init__(f"{status} {code}: {message}")
        self.code = code
        self.status = status
        self.message = message


def _raise_for_error(response: Any) -> None:
    if response.status_code < 400:
        return
    try:
        body = response.json()
        code, message = str(body["code"]), str(body["message"])
    except (ValueError, KeyError, TypeError):
        raise GatewayRequestError("HTTP_ERROR", response.status_code, response.text[:200]) from None
    if code in STATUS:
        raise GatewayError(code, message)
    raise GatewayRequestError(code, response.status_code, message)


@dataclass(frozen=True)
class IssuedChallenge:
    challenge: Challenge
    webauthn_options: dict[str, Any]


class RemoteMarket:
    """Stands in for ``bank.Market`` on the agent side.

    Names come from the snapshot. Quotes come from the gateway, so the quote id
    in a signed plan is one the gateway issued and will honour. ``now`` is
    ignored: the gateway's clock sets quote expiry.
    """

    def __init__(self, client: GatewayClient, listings: Mapping[str, tuple[str, Decimal]]):
        self._client = client
        self.listings: Mapping[str, tuple[str, Decimal]] = MappingProxyType(dict(listings))
        self._issued: dict[str, Quote] = {}

    def name(self, symbol: str) -> str | None:
        listing = self.listings.get(symbol)
        return listing[0] if listing else None

    def quote(self, symbol: str, now: datetime | None = None) -> Quote | None:
        quote = self._client.quote(symbol)
        if quote is not None:
            self._issued[quote.quote_id] = quote
        return quote

    def issued(self, quote_id: str) -> Quote | None:
        return self._issued.get(quote_id)


class GatewayClient:
    """The agent service's only route to the gateway. It holds nothing that moves money.

    Implements ``pipeline.GatewayPort`` (``now``, ``view``, ``history``,
    ``issue_challenge``) over HTTP, plus ``execute`` for relaying an approval.
    """

    def __init__(self, http: Any):
        # An httpx.Client with base_url set, or Starlette's TestClient.
        self._http = http

    @classmethod
    def connect(cls, base_url: str = DEFAULT_ORIGIN, *, timeout: float = 10.0) -> GatewayClient:
        return cls(httpx.Client(base_url=base_url, timeout=timeout))

    def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._http.request(method, path, **kwargs)
        _raise_for_error(response)
        return response.json()

    def now(self) -> datetime:
        return datetime.fromisoformat(self._call("GET", "/now")["now"])

    def view(self) -> BankView:
        data = self._call("GET", "/view")
        listings = {x["symbol"]: (x["name"], Decimal(x["price"])) for x in data["listings"]}
        return view_from_snapshot(data, RemoteMarket(self, listings))

    def quote(self, symbol: str) -> Quote | None:
        try:
            data = self._call("POST", "/quotes", json={"symbol": symbol})
        except GatewayRequestError as e:
            if e.code == "UNKNOWN_SYMBOL":
                return None
            raise
        return Quote(
            data["quote_id"], data["symbol"], data["name"], Decimal(data["price"]),
            datetime.fromisoformat(data["issued_at"]), datetime.fromisoformat(data["expires_at"]),
        )

    def history(self, since: datetime | None = None) -> tuple[Execution, ...]:
        params = {"since": since.isoformat()} if since is not None else None
        data = self._call("GET", "/history", params=params)
        return tuple(
            Execution(datetime.fromisoformat(e["at"]), ResolvedStep.model_validate(e["step"]))
            for e in data["executions"]
        )

    def request_challenge(
        self, plan: ResolvedPlan, *, script_hash: str | None = None, policy_decision_id: str | None = None
    ) -> IssuedChallenge:
        body = {
            "plan": plan.model_dump(mode="json"),
            "script_hash": script_hash,
            "policy_decision_id": policy_decision_id,
        }
        data = self._call("POST", "/challenges", json=body)
        return IssuedChallenge(Challenge.model_validate(data["challenge"]), data["webauthn_options"])

    def issue_challenge(
        self, plan: ResolvedPlan, *, script_hash: str | None = None, policy_decision_id: str | None = None
    ) -> Challenge:
        """``GatewayPort``; the device fetches its passkey prompt by nonce (/webauthn/approve/options)."""
        return self.request_challenge(
            plan, script_hash=script_hash, policy_decision_id=policy_decision_id
        ).challenge

    def execute(
        self,
        plan: ResolvedPlan | None = None,
        *,
        nonce: str | None,
        assertion: Mapping[str, Any] | None = None,
        signature: bytes | None = None,
    ) -> list[Receipt]:
        body: dict[str, Any] = {"nonce": nonce}
        if plan is not None:
            body["plan"] = plan.model_dump(mode="json")
        if assertion is not None:
            body["assertion"] = dict(assertion)
        if signature is not None:
            body["signature"] = signature.hex()
        data = self._call("POST", "/execute", json=body)
        return [Receipt(**r) for r in data["receipts"]]

    def audit(self, since: int = 0) -> dict[str, Any]:
        return self._call("GET", "/audit", params={"since": since})


# --- Entrypoint ---------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m glassvault.services.gateway_api",
        description="Glass Vault gateway: the only service that can move money.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--rp-id", default=DEFAULT_RP_ID, help="WebAuthn relying party id (the site's domain)")
    parser.add_argument("--origin", action="append",
                        help="trusted WebAuthn origin; repeat for several (default http://localhost:PORT)")
    parser.add_argument("--audit-path", type=Path, help="also append audit entries to this JSONL file")
    parser.add_argument("--enrolment-code", default=os.environ.get("GV_ENROLMENT_CODE"),
                        help="first passkey enrolment code (default: random, printed below)")
    parser.add_argument("--no-dev-tools", action="store_true", help="disable POST /dev/sample-challenge")
    args = parser.parse_args(argv)

    origins = args.origin or [f"http://localhost:{args.port}"]
    bank = seed_bank(_utcnow().date())
    gateway, _ = build_gateway(bank, rp_id=args.rp_id, origin=origins, audit=AuditLog(args.audit_path))
    app = create_gateway_app(
        gateway, rp_id=args.rp_id, origin=origins,
        enrolment_code=args.enrolment_code, dev_tools=not args.no_dev_tools,
    )
    print(f"Glass Vault gateway for {bank.customer_name} ({bank.customer_id})")
    print(f"  dev harness:            http://localhost:{args.port}/dev")
    print(f"  passkey enrolment code: {app.state.enrolment.current}")
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
