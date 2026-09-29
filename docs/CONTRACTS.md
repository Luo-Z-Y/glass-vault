# Module contracts

Shared models live in `src/glassvault/schema.py`: `Plan` (LLM draft), `ResolvedPlan`
(what is signed), `Challenge`, `Turn`, `Clarification`/`ClarifyOption`,
`PolicyDecision`/`PolicyReason`, `ConfirmationView`. A draft amount is a
`FixedAmount`, a `FractionOfStep` ("half of it") or a `FractionOfBalance` ("a
quarter of my savings"). The mock bank is `bank.py` (`Bank`, `BankView`,
`seed_bank`, `share_cost`). Amounts are always `Decimal`.

Each module below has one owner. Do not edit another owner's module; if you need
something from it, code against the contract here.

## numbers.py: amount grammar (deterministic)

```python
@dataclass(frozen=True)
class AmountMention:
    value: Decimal          # S$ value, 2 d.p.
    text: str               # exact words, e.g. "a grand"
    start: int; end: int    # span in the source text

@dataclass(frozen=True)
class FractionMention:
    numerator: int; denominator: int
    text: str; start: int; end: int

def extract_amounts(text: str) -> list[AmountMention]
def parse_amount(phrase: str) -> Decimal | None
def extract_fractions(text: str) -> list[FractionMention]     # "half", "a quarter", "a third", "30%"
def asr_confusables(value: Decimal) -> set[Decimal]            # e.g. 50 -> {15}, 15 -> {50}, 5000 -> {500, 50000}
def confusable_reasons(value: Decimal) -> dict[Decimal, str]   # the same, with "teen_ty" or "magnitude"
def ambiguous_amounts(text: str) -> list[tuple[AmountMention, ...]]   # "two fifty" -> (250, 2.50)
```
Must handle: digits with/without `$`/`S$`/`SGD`, commas, decimals, `5k`, `1.2k`,
`2m`, "a grand", "two grand", words ("fifty", "two hundred and fifty",
"one thousand five hundred", "a hundred"), "bucks"/"dollars", Singlish forms
("50 dollar", "5 k"), and "N of X shares" as cash ("buy 2000 of OCBC shares" is
S$2,000). Never treat dates, times, account/phone numbers or share counts
("12 shares", "100 of my DBS shares") as money. Mandarin forms (两万五) are not
built yet; they are a WorkBuddy task (see `docs/codebuddy-log.md`).

## context.py: what the LLM may see (quarantine)

```python
@dataclass(frozen=True)
class AgentContext:
    prompt_data: dict          # JSON-serialisable; the ONLY bank data put in prompts
    placeholders: Mapping[str, str]   # "<REF_1>" -> original untrusted text; never sent to an LLM
    quarantine: tuple[QuarantinedText, ...] = ()   # what was withheld and why
    def prompt_json(self) -> str

@dataclass(frozen=True)
class QuarantinedText:
    placeholder: str
    source: str                # e.g. "billers.SP_GROUP.reference"
    reason: Literal["third_party_text", "suspicious_label"]
    signals: tuple[InjectionSignal, ...] = ()

@dataclass(frozen=True)
class InjectionSignal:
    kind: str                  # e.g. "instruction_override", "role_play", "system_marker"
    text: str; start: int; end: int

def build_context(view: BankView) -> AgentContext
def sanitize_label(text: str, max_len: int = 40) -> str
def scan_text(text: str, *, data_field: bool = False) -> list[InjectionSignal]
    # default: markers that never belong in a customer's words; data_field=True also
    # flags payment commands and notes addressed to an AI (an attack only inside bank data)
def payee_matches(phrase: str, view: BankView) -> list[str]           # payee ids a spoken phrase could mean
def find_payee_mentions(transcript: str, view: BankView) -> list[PayeeMention]
def ambiguous_payee_mentions(transcript: str, view: BankView) -> list[tuple[str, list[str]]]
def payee_is_mentioned(payee_id: str, transcript: str, view: BankView) -> bool
```
`prompt_data` includes accounts (id, name, balance), payees (id, legal_name,
sanitised nickname, relationship aliases, masked proxy, date added, payment
history summary), billers (id, name, amount_due, reference replaced by a
placeholder), and listed symbols (symbol, name). Relationship aliases:
"mum/mom/mother/ma" match a payee nicknamed Mum.

## llm.py / drafter.py / validator.py / rogue.py / offline.py: the AI layer

```python
@dataclass(frozen=True)
class ToolCall: name: str; arguments: dict; id: str | None = None; parse_error: str | None = None
@dataclass(frozen=True)
class LLMResponse: text: str | None; tool_calls: tuple[ToolCall, ...] = (); usage: dict = {}

class LLM(Protocol):
    name: str
    def chat(self, messages: list[dict], *, tools: list[dict] | None = None,
             tool_choice: str | dict | None = None, temperature: float = 0.0) -> LLMResponse

class OpenAICompatLLM(LLM): ...   # (*, model, base_url, api_key, name, timeout, max_retries, extra_body)
                                  # retries with tool_choice="auto" if an endpoint rejects "required"
class ScriptedLLM(LLM): ...       # returns queued responses; records calls (for tests)
class OfflineLLM: ...             # offline.py, OfflineLLM(view: Callable[[], BankView]): rule-based stand-in
def llm_from_env(role: Literal["drafter", "validator"], *, environ: Mapping[str, str] | None = None) -> LLM
# env, falling back to .env: GV_LLM_BASE_URL, GV_LLM_API_KEY, GV_DRAFTER_MODEL, GV_VALIDATOR_MODEL
#   optional: GV_VALIDATOR_BASE_URL / GV_VALIDATOR_API_KEY, GV_DRAFTER_EXTRA_BODY /
#   GV_VALIDATOR_EXTRA_BODY (JSON objects), GV_LLM_TIMEOUT (seconds, default 30)

class DraftError(Exception): ...  # no usable answer, even after one repair round
def draft(dialogue: Sequence[Turn], context: AgentContext, llm: LLM) -> Plan | Clarification
    # tools: submit_plan / ask_clarification

def independent_plan(dialogue: Sequence[Turn], context: AgentContext, llm: LLM) -> Plan | Clarification
    # the blind validator: same inputs, different prompt; NEVER receives the draft
    # tools: record_request / report_unclear

@dataclass(frozen=True)
class StepDiff: kind: Literal["missing", "extra", "changed"]; step: str; detail: str
@dataclass(frozen=True)
class PlanDiff:
    matches: bool
    diffs: tuple[StepDiff, ...]
def diff_plans(draft: ResolvedPlan, check: ResolvedPlan) -> PlanDiff
    # order-insensitive; compares type, from_account, to, amount; equity by symbol and quantity

ATTACKS: tuple[str, ...]   # "extra_transfer", "swap_payee", "inflate_amount", "swap_account", "follow_injection"
def corrupt_plan(plan: Plan, attack: str, view: BankView) -> Plan
class RogueLLM(LLM): ...   # RogueLLM(inner, attack, view, *, fallback=True): corrupts any submit_plan
                           # tool call per `attack`, or the next attack that fits the plan
```

## evaluator.py: Plan -> ResolvedPlan (all money arithmetic)

```python
def resolve(plan: Plan, view: BankView, *, now: datetime) -> ResolvedPlan
def destination_label(records: Bank | BankView, step_type: StepType, to: str,
                      equity: EquityFill | None) -> str | None   # display name, from bank records only
class EvaluationError(Exception): code: str; message: str; step_id: str | None
```
Steps run in order against simulated balances. Fractions round down; a
`FractionOfBalance` uses the balance as it stands when its step runs; a share
buy takes a quote from `view.market` and buys `floor(notional / price)` shares.
Error codes: `UNKNOWN_ACCOUNT`, `UNKNOWN_PAYEE`, `UNKNOWN_BILLER`,
`UNKNOWN_SYMBOL`, `SAME_ACCOUNT`, `INSUFFICIENT_FUNDS`, `NOTIONAL_TOO_SMALL`,
`FORWARD_REFERENCE`, `ZERO_AMOUNT`.

## policy.py + renderer.py: risk rules and the confirmation script

```python
def evaluate_policy(plan: ResolvedPlan, view: BankView, *, now: datetime,
                    history: Sequence[Execution]) -> PolicyDecision
def decision_id(plan: ResolvedPlan, outcome: str, reasons: Sequence[PolicyReason]) -> str
def render(plan: ResolvedPlan, policy: PolicyDecision, view: BankView) -> ConfirmationView
def spell_amount(value: Decimal) -> str     # "one thousand dollars and fifty cents"
def read_digits(value: Decimal) -> str      # digit-by-digit read-back of large or unusual amounts
```
`Execution` is `gateway.Execution(at, step)`. Outcome precedence:
deny > hold > step_up > allow. Own-account transfers are exempt from
third-party limits. `render` raises `RenderError` rather than render a plan
whose policy decision was made for a different plan, or a counterparty the bank
does not know.

## pipeline.py: a dialogue in, one outcome out

```python
class GatewayPort(Protocol):   # all the pipeline may ask of the gateway; there is no execute
    def now(self) -> datetime
    def view(self) -> BankView
    def history(self, since: datetime | None = None) -> Sequence[Execution]
    def issue_challenge(self, plan: ResolvedPlan, *, script_hash: str | None = None,
                        policy_decision_id: str | None = None) -> Challenge

class LocalGatewayPort: ...    # LocalGatewayPort(gateway): in-process port (web demo, benchmark)

@dataclass(frozen=True)
class TraceEvent: stage: str; ok: bool; detail: str

@dataclass(frozen=True)
class Outcome:
    kind: Literal["confirm", "clarify", "frozen", "refused", "held", "error"]
    message: str                           # a fixed template, or the clarifying question
    plan: ResolvedPlan | None = None
    clarification: Clarification | None = None
    confirmation: ConfirmationView | None = None
    policy: PolicyDecision | None = None
    challenge: Challenge | None = None     # set when kind == "confirm"
    diff: PlanDiff | None = None           # set when the validator disagreed
    trace: tuple[TraceEvent, ...] = ()
    llm_calls: int = 0

class Pipeline:
    def __init__(self, gateway: GatewayPort, drafter_llm: LLM, validator_llm: LLM)
    def handle(self, dialogue: Sequence[Turn]) -> Outcome

def agent_factory(bank, gateway, attack, *, drafter_llm=None, validator_llm=None)
    # Glass Vault as an evals AgentFactory; with `attack`, the drafter is a RogueLLM
```

## gateway.py, crypto.py, audit.py, stepup.py: the only ledger writer

```python
class Gateway:
    def __init__(self, bank: Bank, audit: AuditLog, *, verifier: ApprovalVerifier | None = None,
                 policy: PolicyFn | None = evaluate_policy, step_up: StepUpVerifier | None = None,
                 clock=..., challenge_ttl: timedelta = timedelta(minutes=2))
    bank: Bank; audit: AuditLog                     # read-only properties
    def now(self) -> datetime
    def history(self, since: datetime | None = None) -> tuple[Execution, ...]
    def register_device(self, customer_id: str, public_key: bytes) -> None       # write-once
    def issue_challenge(self, plan: ResolvedPlan, *, script_hash: str | None = None,
                        policy_decision_id: str | None = None) -> Challenge
    def pending_plan(self, nonce: str) -> ResolvedPlan | None                   # the gateway's own copy
    def execute(self, plan: ResolvedPlan | None = None, *, nonce: str | None = None,
                signature: Any = None, step_up_code: str | None = None) -> list[Receipt]

class GatewayError(Exception): code: str; status: int; message: str   # codes and statuses in STATUS
@dataclass(frozen=True)
class Receipt: reference: str; step_id: str; summary: str
@dataclass(frozen=True)
class Execution: at: datetime; step: ResolvedStep

class ApprovalVerifier(Protocol):
    def verify(self, customer_id: str, message: bytes, proof: Any) -> bool
class DeviceKeyVerifier: ...   # raw Ed25519 device keys (CLI demo, benchmark, tests): has, register, verify

# crypto.py
def canonical_bytes(obj: Any) -> bytes               # sorted keys, no whitespace, Decimals as strings
def plan_hash(plan: ResolvedPlan) -> str             # sha256 hex of the canonical plan
def challenge_digest(challenge: Challenge) -> bytes  # what the customer's device signs
class DeviceKey:                                     # stands in for the passkey outside the web app
    public_bytes: bytes
    def sign(self, message: bytes) -> bytes
def verify_signature(public_key: bytes, signature: bytes, message: bytes) -> bool

# audit.py
class AuditLog:
    def __init__(self, path: Path | None = None)   # with a path, entries are also appended as JSONL
    entries: tuple[AuditEntry, ...]
    def append(self, kind: str, data: dict, *, at: datetime) -> AuditEntry
    def first_broken(self) -> int | None           # first entry that fails the hash chain, or None

# stepup.py
class DigitalToken:
    def code(self, nonce: str) -> str              # 6 digits: HMAC-SHA256 over the nonce
class StepUpVerifier:
    def enrol(self, customer_id: str, token: DigitalToken) -> None
    def verify(self, customer_id: str, nonce: str, code: object) -> bool
    def record_failure(self, nonce: str) -> int    # the gateway voids the approval at MAX_ATTEMPTS = 3
```
Each gateway error code and the attack it stops are listed in
`docs/ARCHITECTURE.md`.

## services/: HTTP split (agent zone vs gateway zone)

- `services/gateway_api.py`: FastAPI app factory
  `create_gateway_app(gateway, *, rp_id, origin, enrolment_code=None, dev_tools=False)`;
  `build_gateway(bank, *, rp_id, origin, audit=None, clock=...)` returns a
  `(Gateway, WebAuthnVerifier)` pair. The only service holding the `Bank`. Endpoints:
  - read side: `GET /health`, `GET /now`, `GET /view` (read-only snapshot for the
    agent), `POST /quotes`, `GET /history`, `GET /audit`
  - approval: `POST /challenges`, `POST /webauthn/approve/options` (returns the
    gateway's own copy of the plan), `POST /execute`
  - enrolment: `POST /webauthn/register/options|verify` (both need the one-time
    enrolment code shown on the gateway's console), `GET /webauthn/passkeys`
  - dev: `GET /dev` (passkey test page), and `POST /dev/sample-challenge` with `dev_tools`

  Every error is `{code, status, message}`. `GatewayClient(http)` (or
  `GatewayClient.connect(base_url)`) is the HTTP `pipeline.GatewayPort`: its
  `BankView` fetches share quotes from the gateway, and `request_challenge` and
  `execute` carry passkey assertions.
- `services/webauthn_verifier.py`: `WebAuthnVerifier(*, rp_id, origin, rp_name="Glass Vault", audit=None, clock=...)`
  implementing `gateway.ApprovalVerifier`, plus `registration_options`,
  `verify_registration`, `authentication_options` and `passkeys`. The WebAuthn
  challenge bytes are `crypto.challenge_digest(challenge)`. User verification is
  required, and each signed assertion is accepted once.
- `services/agent_api.py` (planned, not built): a FastAPI app wrapping the
  pipeline, talking to the gateway only over HTTP through `GatewayClient`. Until
  it exists, the web demo (`web/server.py`) runs the pipeline and the gateway in
  one process, connected by `LocalGatewayPort`.

## evals/: benchmark

- `evals/cases.yaml`: hand-written cases (see `evals/README.md` for the format).
- `evals/heldout.yaml`, `evals/heldout2.yaml`: two held-out sets of 20 cases
  each that WorkBuddy wrote from a spec of the bank data and outcome rules,
  without seeing our prompts or cases. Run one with `--cases evals/heldout.yaml`.
- `src/glassvault/evals/`:
  - `dataset.py`: `load_cases`, the `Case` format and its setup operations
  - `harness.py`: `run_suite`, `run_case`, `AgentOutcome`, `Oracle`, `Scorecard`,
    `render_markdown`, and `AgentFactory = Callable[[Bank, Gateway, str | None], AgentFn]`
  - `naive.py`: `naive_agent_factory(llm)`, the baseline agent
  - `__main__.py`: the CLI (`--agent`, `--cases`, `--out`, `--md`, `--only`,
    `--tags`, `--exclude-tags`, `--workers`, `--name`, `--fail-on-unsafe`, `--compare`)
