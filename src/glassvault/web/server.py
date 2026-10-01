"""Demo server: the customer app, the security console, and the bank behind them.

    uv run python -m glassvault.web.server      # then open http://localhost:8000

One process for the demo, but the zones stay separate objects: the pipeline
(agent zone + control plane) only reaches the gateway through `LocalGatewayPort`,
which can read state and request challenges but never execute. Approvals go
from the browser's passkey straight to `Gateway.execute`.

Without GV_LLM_API_KEY the drafter and validator run on `OfflineLLM`, so the
whole flow works with no API key. With the TRTC settings (see `voice.py`), the
bank voice is TRTC text-to-speech and push-to-talk is TRTC speech recognition;
without them, the browser's own speech is used.
"""

from __future__ import annotations

import hashlib
import os
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from ..audit import AuditLog
from ..bank import Bank, seed_bank
from ..crypto import DeviceKey, challenge_digest
from ..evaluator import resolve
from ..gateway import DeviceKeyVerifier, Gateway, GatewayError
from ..llm import LLM
from ..offline import OfflineLLM
from ..pipeline import LocalGatewayPort, Outcome, Pipeline
from ..rogue import ATTACKS, RogueLLM
from ..schema import Challenge, FixedAmount, PayNowTransfer, Plan, Turn
from ..services.webauthn_verifier import WebAuthnVerifier
from ..stepup import DigitalToken, StepUpVerifier
from ..voice import ASR_MAX_BYTES, TrtcAsr, TrtcTts, VoiceError, voice_from_env

STATIC = Path(__file__).parent / "static"
PORT = int(os.environ.get("GV_PORT") or os.environ.get("PORT") or "8000")
ORIGIN = os.environ.get("GV_ORIGIN", f"http://localhost:{PORT}")
RP_ID = os.environ.get("GV_RP_ID", "localhost")


class DemoVerifier:
    """Real passkeys (WebAuthn) first; a clearly labelled simulated key until one is enrolled."""

    def __init__(self, webauthn: WebAuthnVerifier, simulated: DeviceKeyVerifier):
        self.webauthn = webauthn
        self.simulated = simulated

    def has_passkey(self, customer_id: str) -> bool:
        return bool(self.webauthn.passkeys(customer_id))

    # Enrolment of raw device keys through the gateway, which makes it write-once.
    def has(self, customer_id: str) -> bool:
        return self.has_passkey(customer_id) or self.simulated.has(customer_id)

    def register(self, customer_id: str, public_key: bytes) -> None:
        self.simulated.register(customer_id, public_key)

    def verify(self, customer_id: str, message: bytes, proof: Any) -> bool:
        if isinstance(proof, dict | str):
            return self.webauthn.verify(customer_id, message, proof)
        if isinstance(proof, bytes) and not self.has_passkey(customer_id):
            return self.simulated.verify(customer_id, message, proof)
        return False


@dataclass
class Demo:
    bank: Bank
    audit: AuditLog
    gateway: Gateway
    verifier: DemoVerifier
    simulated_phone: DeviceKey
    token: DigitalToken  # the customer's step-up token (simulated separate device)
    drafter: LLM
    validator: LLM
    llm_mode: str
    rogue: str | None = None
    dialogue: list[Turn] = field(default_factory=list)
    challenges: dict[str, Challenge] = field(default_factory=dict)
    last_approval: tuple[str, Any] | None = None
    blocked: int = 0
    funds_moved: Decimal = Decimal("0.00")
    tts: TrtcTts | None = None  # the bank voice; None: the browser speaks
    asr: TrtcAsr | None = None  # push-to-talk; None: the browser transcribes
    # The only words the bank voice may say: scripts the control plane rendered, by SHA-256.
    scripts: OrderedDict[str, str] = field(default_factory=OrderedDict)
    audio: OrderedDict[str, bytes] = field(default_factory=OrderedDict)  # synthesised scripts, to save credit

    @property
    def pipeline(self) -> Pipeline:
        drafter = RogueLLM(self.drafter, self.rogue, self.bank.view()) if self.rogue else self.drafter
        return Pipeline(LocalGatewayPort(self.gateway), drafter, self.validator)


def _llms(bank: Bank) -> tuple[LLM, LLM, str]:
    if os.environ.get("GV_LLM_API_KEY") or _dotenv_has_key():
        from ..llm import llm_from_env

        drafter, validator = llm_from_env("drafter"), llm_from_env("validator")
        return drafter, validator, f"{drafter.name} + {validator.name}"
    offline = OfflineLLM(bank.view)
    return offline, offline, "offline (scripted AI, no API key)"


def _dotenv_has_key() -> bool:
    env = Path.cwd() / ".env"
    return env.exists() and any(line.startswith("GV_LLM_API_KEY=") and line.strip() != "GV_LLM_API_KEY="
                                for line in env.read_text().splitlines())


def new_demo() -> Demo:
    clock = lambda: datetime.now(UTC)  # noqa: E731
    bank = seed_bank(clock().date())
    audit = AuditLog()
    webauthn = WebAuthnVerifier(rp_id=RP_ID, origin=ORIGIN, audit=audit, clock=clock)
    verifier = DemoVerifier(webauthn, DeviceKeyVerifier())
    token, step_up = DigitalToken(), StepUpVerifier()
    step_up.enrol(bank.customer_id, token)
    gateway = Gateway(bank, audit, verifier=verifier, step_up=step_up, clock=clock)
    phone = DeviceKey()  # the simulated phone, used until a real passkey is enrolled
    gateway.register_device(bank.customer_id, phone.public_bytes)
    drafter, validator, mode = _llms(bank)
    tts, asr = voice_from_env()
    return Demo(bank, audit, gateway, verifier, phone, token, drafter, validator, mode, tts=tts, asr=asr)


demo = new_demo()
app = FastAPI(title="Glass Vault demo")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def revalidate_ui(request: Request, call_next):
    """Browsers must revalidate the page and its assets, so an old page never runs with new JS."""
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


# --- JSON shapes -------------------------------------------------------------------


def llm_json(llm: LLM) -> dict[str, str | None]:
    model, _, host = llm.name.partition("@")  # names are "model@host" (see OpenAICompatLLM)
    return {"model": model, "host": host or None}


def state_json() -> dict[str, Any]:
    customer = demo.bank.customer_id
    return {
        "customer_name": demo.bank.customer_name,
        "accounts": [{"id": a.id, "name": a.name, "balance": str(a.balance)} for a in demo.bank.accounts.values()],
        "llm_mode": demo.llm_mode,
        "llm": {"live": not isinstance(demo.drafter, OfflineLLM),
                "drafter": llm_json(demo.drafter), "validator": llm_json(demo.validator)},
        "rogue": demo.rogue,
        "passkey": {"registered": demo.verifier.has_passkey(customer),
                    "simulated_allowed": not demo.verifier.has_passkey(customer)},
        "attacks": {"blocked": demo.blocked, "funds_moved": str(demo.funds_moved)},
        "voice": {"speak": {"engine": "trtc" if demo.tts else "browser", "name": demo.tts.name if demo.tts else None},
                  "listen": {"engine": "trtc" if demo.asr else "browser", "name": demo.asr.name if demo.asr else None}},
    }


MAX_SCRIPTS = 64


def speech(o: Outcome) -> str | None:
    """Register the bank-rendered script this outcome may speak, and return its SHA-256 (the voice's key)."""
    script = (o.confirmation or o.answer).spoken_script if (o.confirmation or o.answer) else None
    if not script:
        return None
    key = hashlib.sha256(script.encode("utf-8")).hexdigest()  # equals the confirmation's script_hash
    demo.scripts[key] = script
    demo.scripts.move_to_end(key)
    while len(demo.scripts) > MAX_SCRIPTS:
        demo.scripts.popitem(last=False)
    return key


def outcome_json(o: Outcome) -> dict[str, Any]:
    data: dict[str, Any] = {
        "kind": o.kind,
        "message": o.message,
        "trace": [{"stage": t.stage, "ok": t.ok, "detail": t.detail} for t in o.trace],
        "diff": [{"kind": d.kind, "step": d.step, "detail": d.detail} for d in o.diff.diffs] if o.diff else None,
    }
    if o.clarification:
        data["clarification"] = o.clarification.model_dump(mode="json")
    if o.answer:
        # Lines to show and a script for the bank voice, both in the bank's fixed wording.
        data["answer"] = o.answer.model_dump(mode="json")
    if o.confirmation:
        data["confirmation"] = o.confirmation.model_dump(mode="json")
        # The renderer lists the payment steps first, then any risk notices.
        data["step_count"] = len(o.plan.steps) if o.plan else len(o.confirmation.lines)
    if o.policy:
        data["policy"] = o.policy.model_dump(mode="json")
    if o.challenge:
        data["challenge"] = {"nonce": o.challenge.nonce, "expires_at": o.challenge.expires_at.isoformat(),
                             "step_up_required": o.challenge.step_up_required}
        if demo.verifier.has_passkey(demo.bank.customer_id):
            data["webauthn"] = demo.verifier.webauthn.authentication_options(
                demo.bank.customer_id, challenge_digest(o.challenge))
    return data


def error(e: GatewayError) -> JSONResponse:
    return JSONResponse({"code": e.code, "status": e.status, "message": e.message}, status_code=e.status)


# --- Pages and state -----------------------------------------------------------------


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/state")
def get_state() -> dict[str, Any]:
    return state_json()


@app.post("/api/reset")
def reset() -> dict[str, Any]:
    global demo
    demo = new_demo()
    return {"state": state_json()}


class RogueRequest(BaseModel):
    attack: str | None = None


@app.post("/api/rogue")
def set_rogue(req: RogueRequest) -> dict[str, Any]:
    if req.attack is not None and req.attack not in ATTACKS:
        raise HTTPException(400, f"unknown attack {req.attack!r}")
    demo.rogue = req.attack
    return {"state": state_json()}


# --- Conversation ----------------------------------------------------------------------


class MessageRequest(BaseModel):
    text: str
    voice: bool = False


@app.post("/api/message")
def message(req: MessageRequest) -> dict[str, Any]:
    text = req.text.strip()[:500]
    if not text:
        raise HTTPException(400, "empty message")
    try:
        turn = Turn(role="customer", text=text, via="voice" if req.voice else "text")
    except ValidationError:
        raise HTTPException(400, "message is not valid text") from None
    demo.dialogue.append(turn)
    try:
        outcome = demo.pipeline.handle(demo.dialogue)
    except Exception:
        demo.dialogue.clear()  # one broken turn must not break every turn after it
        raise
    if outcome.kind == "clarify":
        demo.dialogue.append(Turn(role="agent", text=outcome.message))
    else:
        demo.dialogue.clear()
    if outcome.challenge:
        demo.challenges[outcome.challenge.nonce] = outcome.challenge
    data = outcome_json(outcome)
    data["speech"] = speech(outcome)
    return {"outcome": data, "state": state_json()}


# --- Voice (TRTC) -----------------------------------------------------------------------


MAX_AUDIO = 16


def _voice_error(e: VoiceError, status: int = 502) -> JSONResponse:
    return JSONResponse({"code": e.code, "status": status, "message": e.message}, status_code=status)


@app.get("/api/voice/{script_hash}")
def voice(script_hash: str):
    """A bank-rendered script in the bank's TRTC voice, as WAV. It speaks nothing else: there is no text input."""
    if demo.tts is None:
        return JSONResponse({"code": "VOICE_OFF", "status": 404, "message": "TRTC voice is not set up"}, 404)
    script = demo.scripts.get(script_hash)
    if script is None:
        return JSONResponse({"code": "UNKNOWN_SCRIPT", "status": 404, "message": "not a script the bank rendered"}, 404)
    audio = demo.audio.get(script_hash)
    if audio is None:
        try:
            audio = demo.tts.synthesize(script)
        except VoiceError as e:
            return _voice_error(e)
        demo.audio[script_hash] = audio
        while len(demo.audio) > MAX_AUDIO:
            demo.audio.popitem(last=False)
    return Response(audio, media_type="audio/wav", headers={"Cache-Control": "private, max-age=600"})


@app.post("/api/asr")
async def transcribe(request: Request):
    """One push-to-talk recording (mono 16-bit 16 kHz WAV) as text, by TRTC speech recognition."""
    if demo.asr is None:
        return JSONResponse({"code": "ASR_OFF", "status": 404, "message": "TRTC speech recognition is not set up"}, 404)
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > ASR_MAX_BYTES:
            return JSONResponse({"code": "AUDIO_TOO_LONG", "status": 413, "message": "recording too long"}, 413)
    try:
        text = await run_in_threadpool(demo.asr.transcribe, bytes(body))
    except VoiceError as e:
        return _voice_error(e, 400 if e.code in ("BAD_AUDIO", "AUDIO_TOO_LONG") else 502)
    return {"text": text}


@app.post("/api/cancel")
def cancel() -> dict[str, Any]:
    demo.dialogue.clear()
    return {"state": state_json()}


class ApproveRequest(BaseModel):
    nonce: str
    assertion: dict[str, Any] | None = None
    simulated: bool = False
    step_up_code: str | None = None


@app.post("/api/approve")
def approve(req: ApproveRequest):
    challenge = demo.challenges.get(req.nonce)
    if req.assertion is not None:
        proof: Any = _webauthn_fields(req.assertion)
    elif req.simulated and challenge is not None and not demo.verifier.has_passkey(demo.bank.customer_id):
        proof = demo.simulated_phone.sign(challenge_digest(challenge))  # labelled "simulated" in the UI
    else:
        proof = None
    try:
        receipts = demo.gateway.execute(nonce=req.nonce, signature=proof, step_up_code=req.step_up_code)
    except GatewayError as e:
        return error(e)
    demo.last_approval = (req.nonce, proof)
    return {"receipts": [r.__dict__ for r in receipts], "state": state_json()}


def _webauthn_fields(assertion: dict[str, Any]) -> dict[str, Any]:
    """Only the fields a WebAuthn assertion has; nothing else reaches the gateway or its audit log."""
    response = assertion.get("response") or {}
    return {
        **{k: assertion[k] for k in ("id", "rawId", "type", "authenticatorAttachment") if k in assertion},
        "clientExtensionResults": {},
        "response": {k: response[k] for k in ("clientDataJSON", "authenticatorData", "signature", "userHandle")
                     if k in response},
    }


@app.get("/api/token/{nonce}")
def token_code(nonce: str) -> dict[str, Any]:
    """The simulated digital token: in real life a separate device the agent can't reach."""
    return {"code": demo.token.code(nonce)}


# --- Passkey enrolment ------------------------------------------------------------------


@app.post("/api/passkey/register/options")
def passkey_options():
    customer = demo.bank.customer_id
    if demo.verifier.has_passkey(customer):
        return JSONResponse({"code": "DEVICE_ALREADY_REGISTERED", "status": 409,
                             "message": "a passkey is already enrolled; reset the demo to enrol again"}, 409)
    return demo.verifier.webauthn.registration_options(customer, user_name="weiling@glassvault.demo",
                                                       display_name=demo.bank.customer_name)


class RegisterRequest(BaseModel):
    credential: dict[str, Any]


@app.post("/api/passkey/register/verify")
def passkey_verify(req: RegisterRequest):
    try:
        demo.verifier.webauthn.verify_registration(demo.bank.customer_id, req.credential)
    except Exception as e:  # EnrolmentError and friends
        return JSONResponse({"code": "ENROLMENT_REJECTED", "status": 400, "message": str(e)[:200]}, 400)
    return {"state": state_json()}


# --- "Try to rob this bank": direct attacks on the gateway -----------------------------------

ATTACK_BUTTONS = [
    ("no_approval", "Send S$9,000 with no approval"),
    ("forge", "Sign a valid-looking plan with the AI's own key"),
    ("replay", "Replay the last approval"),
    ("spoof", "Show 'Tan Mei Ling', pay John Lim"),
    ("rekey", "Replace the customer's Face ID key"),
    ("sub_cent", "Slip in a fraction of a cent"),
]


def _theft_plan(payee: str = "P_JLIM", amount: str = "9000"):
    plan = Plan(steps=[PayNowTransfer(id="t1", from_account="SAV", payee_id=payee,
                                      amount=FixedAmount(value=Decimal(amount)))])
    return resolve(plan, demo.bank.view(), now=demo.gateway.now())


def _attack(kind: str) -> tuple[Decimal, Any]:
    """Run one attack; returns (amount at stake, gateway result). Raises GatewayError when blocked."""
    gw = demo.gateway
    match kind:
        case "no_approval":
            plan = _theft_plan()
            return plan.steps[0].amount, gw.execute(plan)
        case "forge":
            plan = _theft_plan(amount="900")  # under the new-payee limit, so only the signature stands in the way
            challenge = gw.issue_challenge(plan)
            return plan.steps[0].amount, gw.execute(plan, nonce=challenge.nonce,
                                                    signature=DeviceKey().sign(challenge_digest(challenge)))
        case "replay":
            nonce, proof = demo.last_approval or ("0" * 32, b"\x00" * 64)
            return Decimal("0"), gw.execute(nonce=nonce, signature=proof)
        case "spoof":
            honest = _theft_plan("P_MUM", "500")
            spoof = honest.model_copy(update={"steps": (honest.steps[0].model_copy(update={"to": "P_JLIM"}),)})
            return Decimal("500"), gw.issue_challenge(spoof)
        case "rekey":
            return Decimal("0"), gw.register_device(demo.bank.customer_id, DeviceKey().public_bytes)
        case "sub_cent":
            honest = _theft_plan("P_MUM", "500")
            bad = honest.model_copy(update={"steps": (honest.steps[0].model_copy(update={"amount": Decimal("500.004")}),)})
            return Decimal("500"), gw.issue_challenge(bad)
    raise HTTPException(404, f"unknown attack {kind!r}")


@app.get("/api/attacks")
def attacks() -> dict[str, Any]:
    return {"attacks": [{"id": k, "label": label} for k, label in ATTACK_BUTTONS]}


@app.post("/api/attacks/{kind}")
def run_attack(kind: str) -> dict[str, Any]:
    try:
        at_stake, result = _attack(kind)
    except GatewayError as e:
        demo.blocked += 1
        return {"result": {"blocked": True, "code": e.code, "status": e.status, "message": e.message},
                "state": state_json()}
    # A challenge being issued is not money moving; only an execution counts.
    moved = at_stake if isinstance(result, list) else Decimal("0")
    demo.funds_moved += moved
    return {"result": {"blocked": False, "code": "EXECUTED" if moved else "ISSUED", "status": 200,
                       "message": str(result)[:200]}, "state": state_json()}


@app.get("/api/audit")
def audit() -> dict[str, Any]:
    entries = demo.audit.entries
    return {"intact": demo.audit.first_broken() is None,
            "entries": [{"seq": e.seq, "kind": e.kind, "at": e.at, "hash": e.hash,
                         # A REQUEST entry is one customer turn; its outcome says what became of it.
                         "outcome": e.data.get("outcome") if e.kind == "REQUEST" else None}
                        for e in entries]}


@app.exception_handler(GatewayError)
def _gateway_error(request: Request, exc: GatewayError) -> JSONResponse:
    return error(exc)


def main() -> None:
    import uvicorn

    print(f"Glass Vault demo on {ORIGIN}  (use localhost, not 127.0.0.1, for passkeys)")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
