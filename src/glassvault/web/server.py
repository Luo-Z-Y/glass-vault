"""Demo server: the customer app, the security console, and the bank behind them.

    uv run python -m glassvault.web.server      # then open http://localhost:8000

One process for the demo, but the zones stay separate objects: the pipeline
(agent zone + control plane) only reaches the gateway through `LocalGatewayPort`,
which can read state and request challenges but never execute. Approvals go
from the browser's passkey straight to `Gateway.execute`.

Without GV_LLM_API_KEY the drafter and validator run on `OfflineLLM`, so the
whole flow works with no API key. With the TRTC settings (see `voice.py`), the
bank voice is TRTC text-to-speech and push-to-talk is TRTC speech recognition;
without them, the browser's own speech is used. With GV_ADP_APP_KEY (see `faq.py`),
Help, the bank FAQ assistant, is a Tencent ADP knowledge-Q&A app; without it, Help
answers from the built-in FAQ. Help stands apart from the payment conversation: its
words never enter the dialogue, the audit log or the bank voice.

Hosted for others (deploy/README.md), it can ask for a password, give each visitor a
demo of their own and cap its paid calls per hour: see "Hosting" below.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import threading
import time
from base64 import b64decode
from collections import OrderedDict, deque
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError
from starlette.middleware.body_limit import RequestBodyLimitMiddleware

from .. import faq
from ..audit import AuditLog
from ..bank import Bank, seed_bank
from ..crypto import DeviceKey, challenge_digest
from ..evaluator import resolve
from ..faq import AdpHelp, new_conversation_id
from ..gateway import DeviceKeyVerifier, Gateway, GatewayError
from ..llm import LLM
from ..offline import OfflineLLM
from ..pipeline import LocalGatewayPort, Outcome, Pipeline
from ..rogue import ATTACKS, RogueLLM
from ..schema import Challenge, FixedAmount, PayNowTransfer, Plan, Turn
from ..services.webauthn_verifier import WebAuthnVerifier
from ..stepup import DigitalToken, StepUpVerifier
from ..voice import ASR_MAX_BYTES, TTS_VOICE, TTS_VOICE_LABEL, TrtcAsr, TrtcTts, VoiceError, voice_from_env

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
    # The only words the bank voice may say: scripts the control plane rendered, by SHA-256, with their
    # audio once synthesised (so "Listen again" costs no credit). Evicted together, oldest first.
    scripts: OrderedDict[str, str] = field(default_factory=OrderedDict)
    audio: dict[str, bytes] = field(default_factory=dict)
    voice_lock: threading.Lock = field(default_factory=threading.Lock)
    synthesising: dict[str, threading.Lock] = field(default_factory=dict)  # per script: one TRTC call at a time
    faq: AdpHelp | None = None  # Help's Tencent ADP app; None: the built-in FAQ answers
    # Help's ADP conversation, so a follow-up question has its context. A reset starts a new one.
    faq_conversation: str = field(default_factory=new_conversation_id)

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
    return Demo(bank, audit, gateway, verifier, phone, token, drafter, validator, mode, tts=tts, asr=asr,
                faq=faq.faq_from_env())


demo = new_demo()  # the one shared demo, unless GV_SESSIONS gives each visitor their own
app = FastAPI(title="Glass Vault demo")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def revalidate_ui(request: Request, call_next):
    """Browsers must revalidate the page and its assets, so an old page never runs with new JS."""
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


# --- Hosting: who may visit, whose demo, and how much it may spend -------------------
#
# All off unless set, so a local run behaves as before. A public deployment sets them
# (deploy/README.md): every call to a model, Tencent RTC or ADP spends the account's credit.
#
#   GV_ACCESS_PASSWORD      every request needs it (HTTP basic auth, user GV_ACCESS_USER or "judge")
#   GV_SESSIONS=1           each visitor (a cookie) gets a demo of their own, so one visitor's
#                           rogue mode, passkey or payments never show up in another's
#   GV_PAID_CALLS_PER_HOUR  at most this many calls to a model, Tencent RTC or ADP, all visitors
#                           together; a request that would spend nothing is not counted
#
# Always on: request bodies over 4 MB are refused (a recording is at most 2 MB), and a write to
# /api/ from another site is refused (see _cross_site).

ACCESS_USER = os.environ.get("GV_ACCESS_USER") or "judge"
ACCESS_PASSWORD = os.environ.get("GV_ACCESS_PASSWORD") or ""
SESSIONS = os.environ.get("GV_SESSIONS") == "1"
MAX_SESSIONS = int(os.environ.get("GV_MAX_SESSIONS") or "16")  # the least recently used demo goes first
PAID_CALLS_PER_HOUR = int(os.environ.get("GV_PAID_CALLS_PER_HOUR") or "0")  # 0: no cap
MAX_BODY = 4 * 1024 * 1024
MAX_AUDIT = 2000  # a demo whose audit log is this long takes no more requests until it is reset
# Over HTTPS the cookie is a __Host- cookie, which only this host can set: a sibling subdomain (or,
# on an sslip.io name, anyone's, since sslip.io is not a public suffix) could otherwise plant one.
VISITOR_COOKIE = "__Host-gv_visitor" if ORIGIN.startswith("https://") else "gv_visitor"
VISITOR_ID = re.compile(r"[A-Za-z0-9_-]{22,64}")

_visitor: ContextVar[str | None] = ContextVar("visitor", default=None)
_fresh: ContextVar[bool] = ContextVar("fresh", default=False)  # this request came without a visitor id
_sessions: OrderedDict[str, Demo] = OrderedDict()
_sessions_lock = threading.Lock()
_paid_calls: deque[float] = deque()
_paid_lock = threading.Lock()

app.add_middleware(RequestBodyLimitMiddleware, max_body_size=MAX_BODY)


def current() -> Demo:
    """The demo this request acts on: the visitor's own with GV_SESSIONS, else the shared one.

    A request without a visitor id gets a throwaway demo, so cookieless requests (scripts, or a
    browser that refuses the cookie) can never push a judge's demo out of the store.
    """
    visitor = _visitor.get()
    if visitor is None:
        return demo
    if _fresh.get():
        return new_demo()
    with _sessions_lock:
        d = _sessions.get(visitor)
        if d is None:
            d = _sessions[visitor] = new_demo()
            while len(_sessions) > MAX_SESSIONS:
                _sessions.popitem(last=False)
        _sessions.move_to_end(visitor)
        return d


def replace_demo() -> Demo:
    """A fresh demo in place of this request's one (the Reset button)."""
    global demo
    visitor = _visitor.get()
    if visitor is None:
        demo = new_demo()
        return demo
    if _fresh.get():
        return new_demo()
    with _sessions_lock:
        d = _sessions[visitor] = new_demo()
        _sessions.move_to_end(visitor)
        while len(_sessions) > MAX_SESSIONS:
            _sessions.popitem(last=False)
        return d


def _authorised(request: Request) -> bool:
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        user, _, password = b64decode(value, validate=True).decode("utf-8").partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    # Both compared in constant time, and both always compared.
    return secrets.compare_digest(user.encode(), ACCESS_USER.encode()) & \
        secrets.compare_digest(password.encode(), ACCESS_PASSWORD.encode())


def spend() -> JSONResponse | None:
    """Count one call to a paid service, just before it is made; a refusal if the hour's budget is spent."""
    if not PAID_CALLS_PER_HOUR:
        return None
    now = time.monotonic()
    with _paid_lock:
        while _paid_calls and now - _paid_calls[0] > 3600:
            _paid_calls.popleft()
        if len(_paid_calls) >= PAID_CALLS_PER_HOUR:
            return _refusal("BUSY", 429, "The demo has reached its hourly limit. Please try again later.")
        _paid_calls.append(now)
    return None


def full(d: Demo) -> JSONResponse | None:
    """A refusal once a demo has grown too big to keep (Reset starts it again), else None."""
    if len(d.audit.entries) > MAX_AUDIT:
        return _refusal("DEMO_FULL", 429, "This demo is full: press Reset to start again.")
    return None


@app.middleware("http")
async def hosting(request: Request, call_next):
    """Access, cross-site writes, then the visitor's demo (see above). /healthz is open."""
    if request.url.path == "/healthz":
        return JSONResponse({"status": "ok"})
    if ACCESS_PASSWORD and not _authorised(request):
        return Response("Glass Vault demo: sign in with the user name and password you were given.", 401,
                        {"WWW-Authenticate": 'Basic realm="Glass Vault demo", charset="UTF-8"'}, "text/plain")
    if request.method != "GET" and request.url.path.startswith("/api/") and \
            (refused := _cross_site(request, "this demo")) is not None:
        return refused
    if not SESSIONS:
        return await call_next(request)
    visitor = request.cookies.get(VISITOR_COOKIE, "")
    fresh = not VISITOR_ID.fullmatch(visitor)
    if fresh:
        visitor = secrets.token_urlsafe(24)
    tokens = _visitor.set(visitor), _fresh.set(fresh)
    try:
        response = await call_next(request)
    finally:
        _visitor.reset(tokens[0])
        _fresh.reset(tokens[1])
    if fresh:
        response.set_cookie(VISITOR_COOKIE, visitor, max_age=30 * 86400, httponly=True, samesite="lax",
                            secure=ORIGIN.startswith("https://"))
    return response


# --- JSON shapes -------------------------------------------------------------------


def llm_json(llm: LLM) -> dict[str, str | None]:
    model, _, host = llm.name.partition("@")  # names are "model@host" (see OpenAICompatLLM)
    return {"model": model, "host": host or None}


def state_json(d: Demo) -> dict[str, Any]:
    customer = d.bank.customer_id
    return {
        "customer_name": d.bank.customer_name,
        "accounts": [{"id": a.id, "name": a.name, "balance": str(a.balance)} for a in d.bank.accounts.values()],
        "llm_mode": d.llm_mode,
        "llm": {"live": not isinstance(d.drafter, OfflineLLM),
                "drafter": llm_json(d.drafter), "validator": llm_json(d.validator)},
        "rogue": d.rogue,
        "passkey": {"registered": d.verifier.has_passkey(customer),
                    "simulated_allowed": not d.verifier.has_passkey(customer)},
        "attacks": {"blocked": d.blocked, "funds_moved": str(d.funds_moved)},
        "voice": voice_json(d),
        "faq": faq_json(d),
    }


def voice_json(d: Demo) -> dict[str, Any]:
    """Who listens and who speaks for the bank: TRTC where it is set up, otherwise the browser.

    Beyond the engine and name, the details are for display only, so a voice that lacks them
    (a test double, say) still works: the page shows its name instead.
    """
    tts, asr = d.tts, d.asr
    speak: dict[str, Any] = {"engine": "browser", "name": None}
    if tts:
        voice = getattr(tts, "voice", None)
        speak = {"engine": "trtc", "name": tts.name, "host": getattr(tts, "host", None),
                 "region": getattr(tts, "region", None), "voice": TTS_VOICE_LABEL if voice == TTS_VOICE else voice}
    listen: dict[str, Any] = {"engine": "browser", "name": None}
    if asr:
        listen = {"engine": "trtc", "name": asr.name, "host": getattr(asr, "host", None),
                  "model": getattr(asr, "engine", None), "language": getattr(asr, "language", None) or "auto"}
    return {"speak": speak, "listen": listen}


def faq_json(d: Demo) -> dict[str, Any]:
    """Who answers Help: a Tencent ADP app where one is set up, otherwise the built-in FAQ.

    The name and host are for display only, so a stand-in that lacks them (a test double, say)
    still works, as with voice_json().
    """
    if d.faq is None:
        return {"engine": "offline", "name": None, "host": None}
    return {"engine": "adp", "name": getattr(d.faq, "name", None), "host": getattr(d.faq, "host", None)}


MAX_SCRIPTS = 64
MAX_AUDIO = 8  # recent audio kept per demo, about 1.5 MB each


def speech(d: Demo, o: Outcome) -> str | None:
    """Register the bank-rendered script this outcome may speak, and return its SHA-256 (the voice's key)."""
    script = (o.confirmation or o.answer).spoken_script if (o.confirmation or o.answer) else None
    if not script:
        return None
    key = hashlib.sha256(script.encode("utf-8")).hexdigest()  # equals the confirmation's script_hash
    with d.voice_lock:
        d.scripts[key] = script
        d.scripts.move_to_end(key)
        while len(d.scripts) > MAX_SCRIPTS:
            old, _ = d.scripts.popitem(last=False)
            d.audio.pop(old, None)
            d.synthesising.pop(old, None)
    return key


def outcome_json(d: Demo, o: Outcome) -> dict[str, Any]:
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
        if d.verifier.has_passkey(d.bank.customer_id):
            data["webauthn"] = d.verifier.webauthn.authentication_options(
                d.bank.customer_id, challenge_digest(o.challenge))
    return data


def error(e: GatewayError) -> JSONResponse:
    return JSONResponse({"code": e.code, "status": e.status, "message": e.message}, status_code=e.status)


# --- Pages and state -----------------------------------------------------------------


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/state")
def get_state() -> dict[str, Any]:
    return state_json(current())


@app.post("/api/reset")
def reset() -> dict[str, Any]:
    return {"state": state_json(replace_demo())}


class RogueRequest(BaseModel):
    attack: str | None = None


@app.post("/api/rogue")
def set_rogue(req: RogueRequest) -> dict[str, Any]:
    if req.attack is not None and req.attack not in ATTACKS:
        raise HTTPException(400, f"unknown attack {req.attack!r}")
    d = current()
    d.rogue = req.attack
    return {"state": state_json(d)}


# --- Conversation ----------------------------------------------------------------------


class MessageRequest(BaseModel):
    text: str
    voice: bool = False


@app.post("/api/message")
def message(req: MessageRequest):
    text = req.text.strip()[:500]
    if not text:
        raise HTTPException(400, "empty message")
    try:
        turn = Turn(role="customer", text=text, via="voice" if req.voice else "text")
    except ValidationError:
        raise HTTPException(400, "message is not valid text") from None
    d = current()
    if (refused := full(d)) is not None:
        return refused
    if not isinstance(d.drafter, OfflineLLM) and (busy := spend()) is not None:
        return busy
    d.dialogue.append(turn)
    try:
        outcome = d.pipeline.handle(d.dialogue)
    except Exception:
        d.dialogue.clear()  # one broken turn must not break every turn after it
        raise
    if outcome.kind == "clarify":
        d.dialogue.append(Turn(role="agent", text=outcome.message))
    else:
        d.dialogue.clear()
    if outcome.challenge:
        d.challenges[outcome.challenge.nonce] = outcome.challenge
    data = outcome_json(d, outcome)
    data["speech"] = speech(d, outcome)
    return {"outcome": data, "state": state_json(d)}


# --- Voice (TRTC) -----------------------------------------------------------------------


def _refusal(code: str, status: int, message: str) -> JSONResponse:
    return JSONResponse({"code": code, "status": status, "message": message}, status_code=status)


def _cross_site(request: Request, service: str = "the bank's voice") -> JSONResponse | None:
    """Refuse a call from another site: these endpoints spend TRTC or ADP credit."""
    site = request.headers.get("sec-fetch-site")
    origin = request.headers.get("origin")
    # Hosts, not schemes (behind an HTTPS proxy the page's scheme differs from the server's), and the
    # configured public origin as well as the Host header (a proxy may rewrite Host).
    own = {request.headers.get("host", ""), urlsplit(ORIGIN).netloc}
    if site not in (None, "same-origin", "none") or (origin is not None and urlsplit(origin).netloc not in own):
        return _refusal("CROSS_SITE", 403, f"only this page may use {service}")
    return None


@app.get("/api/voice/{script_hash}")
def voice(script_hash: str, request: Request):
    """A bank-rendered script in the bank's TRTC voice, as WAV. It speaks nothing else: there is no text input."""
    if (refused := _cross_site(request)) is not None:
        return refused
    d = current()
    if d.tts is None:
        return _refusal("VOICE_OFF", 404, "TRTC voice is not set up")
    with d.voice_lock:
        script = d.scripts.get(script_hash)
        audio = d.audio.get(script_hash)
        if script is not None and audio is None:
            busy = d.synthesising.setdefault(script_hash, threading.Lock())  # kept as long as the script
    if script is None:
        return _refusal("UNKNOWN_SCRIPT", 404, "not a script the bank rendered")
    if audio is None:
        try:
            with busy:  # a second request for the same script waits for the first one's audio
                with d.voice_lock:
                    audio = d.audio.get(script_hash)
                if audio is None:
                    if (refused := spend()) is not None:
                        return refused
                    audio = d.tts.synthesize(script)
                    with d.voice_lock:
                        if script_hash in d.scripts:  # not evicted meanwhile
                            d.audio[script_hash] = audio
                            while len(d.audio) > MAX_AUDIO:  # the oldest audio goes; it can be made again
                                d.audio.pop(next(iter(d.audio)))
        except VoiceError as e:
            return _refusal(e.code, 502, e.message)
    return Response(audio, media_type="audio/wav", headers={"Cache-Control": "private, max-age=600"})


@app.post("/api/asr")
async def transcribe(request: Request):
    """One push-to-talk recording (mono 16-bit 16 kHz WAV) as text, by TRTC speech recognition."""
    if (refused := _cross_site(request)) is not None:
        return refused
    d = current()
    if d.asr is None:
        return _refusal("ASR_OFF", 404, "TRTC speech recognition is not set up")
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "audio/wav":
        # Not a type a form or a no-cors fetch can send, so another site's page cannot post here unasked.
        return _refusal("BAD_AUDIO", 415, "send the recording as audio/wav")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > ASR_MAX_BYTES:
            return _refusal("AUDIO_TOO_LONG", 413, "recording too long")
    if (refused := spend()) is not None:
        return refused
    try:
        text = await run_in_threadpool(d.asr.transcribe, bytes(body))
    except VoiceError as e:
        return _refusal(e.code, 400 if e.code in ("BAD_AUDIO", "AUDIO_TOO_LONG") else 502, e.message)
    return {"text": text}


# --- Help (Tencent ADP) ------------------------------------------------------------------


class FaqRequest(BaseModel):
    text: str


@app.post("/api/faq")
def ask_faq(req: FaqRequest, request: Request):
    """Help's answer to a general question about the bank: from Tencent ADP, or the built-in FAQ.

    Help stands apart from the payment conversation. This reads and writes nothing but Help's
    own ADP conversation: not the dialogue, the bank's scripts or audio, the gateway or the
    audit log. So a question here never counts as something the customer told the agent, and
    an answer, which ADP's model wrote, never reaches the bank voice or moves money.
    """
    if (refused := _cross_site(request, "Help")) is not None:  # an ADP call spends the account's credit
        return refused
    try:
        question = faq.check_question(req.text)
    except faq.HelpError as e:
        return _refusal(e.code, 400, e.message)
    d = current()
    if d.faq is not None and (refused := spend()) is not None:
        return refused
    answer = faq.answer(question, d.faq, conversation_id=d.faq_conversation)
    return {
        "answer": {"text": answer.text, "source": answer.source, "matched": answer.matched,
                   "references": list(answer.references), "note": answer.note},
        "faq": faq_json(d),
    }


# --- Cancelling and approving -------------------------------------------------------------


@app.post("/api/cancel")
def cancel() -> dict[str, Any]:
    d = current()
    d.dialogue.clear()
    return {"state": state_json(d)}


class ApproveRequest(BaseModel):
    nonce: str
    assertion: dict[str, Any] | None = None
    simulated: bool = False
    step_up_code: str | None = None


@app.post("/api/approve")
def approve(req: ApproveRequest):
    d = current()
    if (refused := full(d)) is not None:
        return refused
    challenge = d.challenges.get(req.nonce)
    if req.assertion is not None:
        proof: Any = _webauthn_fields(req.assertion)
    elif req.simulated and challenge is not None and not d.verifier.has_passkey(d.bank.customer_id):
        proof = d.simulated_phone.sign(challenge_digest(challenge))  # labelled "simulated" in the UI
    else:
        proof = None
    try:
        receipts = d.gateway.execute(nonce=req.nonce, signature=proof, step_up_code=req.step_up_code)
    except GatewayError as e:
        return error(e)
    d.last_approval = (req.nonce, proof)
    return {"receipts": [r.__dict__ for r in receipts], "state": state_json(d)}


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
    return {"code": current().token.code(nonce)}


# --- Passkey enrolment ------------------------------------------------------------------


@app.post("/api/passkey/register/options")
def passkey_options():
    d = current()
    if (refused := full(d)) is not None:
        return refused
    customer = d.bank.customer_id
    if d.verifier.has_passkey(customer):
        return JSONResponse({"code": "DEVICE_ALREADY_REGISTERED", "status": 409,
                             "message": "a passkey is already enrolled; reset the demo to enrol again"}, 409)
    return d.verifier.webauthn.registration_options(customer, user_name="weiling@glassvault.demo",
                                                       display_name=d.bank.customer_name)


class RegisterRequest(BaseModel):
    credential: dict[str, Any]


@app.post("/api/passkey/register/verify")
def passkey_verify(req: RegisterRequest):
    d = current()
    try:
        d.verifier.webauthn.verify_registration(d.bank.customer_id, req.credential)
    except Exception as e:  # EnrolmentError and friends
        return JSONResponse({"code": "ENROLMENT_REJECTED", "status": 400, "message": str(e)[:200]}, 400)
    return {"state": state_json(d)}


class ApprovalOptionsRequest(BaseModel):
    nonce: str


@app.post("/api/passkey/approval-options")
def passkey_approval_options(req: ApprovalOptionsRequest):
    """WebAuthn options for a confirmation shown before the passkey was enrolled.

    The confirmation came without them (there was no passkey to ask), so a device that sets up
    Face ID from the confirmation itself fetches them here. They hold nothing the customer could
    not compute: the challenge digest and the customer's own credential ids."""
    d = current()
    challenge = d.challenges.get(req.nonce)
    if challenge is None:
        return JSONResponse({"code": "UNKNOWN_CHALLENGE", "status": 404,
                             "message": "no pending challenge has this nonce"}, 404)
    if not d.verifier.has_passkey(d.bank.customer_id):
        return JSONResponse({"code": "NO_PASSKEY", "status": 409,
                             "message": "set up Face ID before asking it to sign"}, 409)
    return {"webauthn": d.verifier.webauthn.authentication_options(
        d.bank.customer_id, challenge_digest(challenge))}


# --- "Try to rob this bank": direct attacks on the gateway -----------------------------------

ATTACK_BUTTONS = [
    ("no_approval", "Send S$9,000 with no approval"),
    ("forge", "Sign a valid-looking plan with the AI's own key"),
    ("replay", "Replay the last approval"),
    ("spoof", "Show 'Mum' (Tan Mei Ling), pay John Lim"),
    ("rekey", "Replace the customer's Face ID key"),
    ("sub_cent", "Slip in a fraction of a cent"),
]


def _theft_plan(d: Demo, payee: str = "P_JLIM", amount: str = "9000"):
    plan = Plan(steps=[PayNowTransfer(id="t1", from_account="SAV", payee_id=payee,
                                      amount=FixedAmount(value=Decimal(amount)))])
    return resolve(plan, d.bank.view(), now=d.gateway.now())


def _attack(d: Demo, kind: str) -> tuple[Decimal, Any]:
    """Run one attack; returns (amount at stake, gateway result). Raises GatewayError when blocked."""
    gw = d.gateway
    match kind:
        case "no_approval":
            plan = _theft_plan(d)
            return plan.steps[0].amount, gw.execute(plan)
        case "forge":
            plan = _theft_plan(d, amount="900")  # under the new-payee limit, so only the signature stands in the way
            challenge = gw.issue_challenge(plan)
            return plan.steps[0].amount, gw.execute(plan, nonce=challenge.nonce,
                                                    signature=DeviceKey().sign(challenge_digest(challenge)))
        case "replay":
            nonce, proof = d.last_approval or ("0" * 32, b"\x00" * 64)
            return Decimal("0"), gw.execute(nonce=nonce, signature=proof)
        case "spoof":
            honest = _theft_plan(d, "P_MUM", "500")
            spoof = honest.model_copy(update={"steps": (honest.steps[0].model_copy(update={"to": "P_JLIM"}),)})
            return Decimal("500"), gw.issue_challenge(spoof)
        case "rekey":
            return Decimal("0"), gw.register_device(d.bank.customer_id, DeviceKey().public_bytes)
        case "sub_cent":
            honest = _theft_plan(d, "P_MUM", "500")
            bad = honest.model_copy(update={"steps": (honest.steps[0].model_copy(update={"amount": Decimal("500.004")}),)})
            return Decimal("500"), gw.issue_challenge(bad)
    raise HTTPException(404, f"unknown attack {kind!r}")


@app.get("/api/attacks")
def attacks() -> dict[str, Any]:
    return {"attacks": [{"id": k, "label": label} for k, label in ATTACK_BUTTONS]}


@app.post("/api/attacks/{kind}")
def run_attack(kind: str):
    d = current()
    if (refused := full(d)) is not None:
        return refused
    try:
        at_stake, result = _attack(d, kind)
    except GatewayError as e:
        d.blocked += 1
        return {"result": {"blocked": True, "code": e.code, "status": e.status, "message": e.message},
                "state": state_json(d)}
    # A challenge being issued is not money moving; only an execution counts.
    moved = at_stake if isinstance(result, list) else Decimal("0")
    d.funds_moved += moved
    return {"result": {"blocked": False, "code": "EXECUTED" if moved else "ISSUED", "status": 200,
                       "message": str(result)[:200]}, "state": state_json(d)}


@app.get("/api/audit")
def audit() -> dict[str, Any]:
    d = current()
    entries = d.audit.entries
    return {"intact": d.audit.first_broken() is None,
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
    # GV_HOST=0.0.0.0 inside a container, where the reverse proxy reaches it over the container network.
    uvicorn.run(app, host=os.environ.get("GV_HOST") or "127.0.0.1", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
