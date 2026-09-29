# Module contracts (week 1-2 build)

Shared models live in `src/glassvault/schema.py`: `Plan` (LLM draft), `ResolvedPlan`
(what is signed), `Challenge`, `Turn`, `Clarification`/`ClarifyOption`,
`PolicyDecision`/`PolicyReason`, `ConfirmationView`. The mock bank is
`bank.py` (`Bank`, `BankView`, `seed_bank`). Amounts are always `Decimal`.

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
```
Must handle: digits with/without `$`/`S$`/`SGD`, commas, decimals, `5k`, `1.2k`,
`2m`, "a grand", "two grand", words ("fifty", "two hundred and fifty",
"one thousand five hundred", "a hundred"), "bucks"/"dollars", Singlish forms
("50 dollar", "5 k"). Never treat dates, times, account/phone numbers or
share counts ("12 shares") as money. Mandarin forms (两万五) are a later
extension (CodeBuddy).

## context.py: what the LLM may see (quarantine)

```python
@dataclass(frozen=True)
class AgentContext:
    prompt_data: dict          # JSON-serialisable; the ONLY bank data put in prompts
    placeholders: Mapping[str, str]   # "<REF_1>" -> original untrusted text; never sent to an LLM

@dataclass(frozen=True)
class InjectionSignal:
    kind: str                  # e.g. "instruction_override", "role_play", "system_marker"
    text: str; start: int; end: int

def build_context(view: BankView) -> AgentContext
def sanitize_label(text: str, max_len: int = 40) -> str
def scan_text(text: str) -> list[InjectionSignal]
def payee_matches(phrase: str, view: BankView) -> list[str]           # payee ids a spoken phrase could mean
def ambiguous_payee_mentions(transcript: str, view: BankView) -> list[tuple[str, list[str]]]
def payee_is_mentioned(payee_id: str, transcript: str, view: BankView) -> bool
```
`prompt_data` includes accounts (id, name, balance), payees (id, legal_name,
sanitised nickname, masked proxy, last-paid summary), billers (id, name,
amount_due, reference replaced by a placeholder), and listed symbols (symbol,
name). Relationship aliases: "mum/mom/mother/ma" match a payee nicknamed Mum.

## llm.py / drafter.py / validator.py / rogue.py: the AI layer

```python
@dataclass(frozen=True)
class ToolCall: name: str; arguments: dict
@dataclass(frozen=True)
class LLMResponse: text: str | None; tool_calls: tuple[ToolCall, ...]; usage: dict

class LLM(Protocol):
    name: str
    def chat(self, messages: list[dict], *, tools: list[dict] | None = None,
             tool_choice: str | dict | None = None, temperature: float = 0.0) -> LLMResponse

class OpenAICompatLLM(LLM): ...   # base_url, api_key, model; OpenAI-compatible chat completions
class ScriptedLLM(LLM): ...       # returns queued responses; records calls (for tests)
def llm_from_env(role: Literal["drafter", "validator"]) -> LLM
# env: GV_LLM_BASE_URL, GV_LLM_API_KEY, GV_DRAFTER_MODEL, GV_VALIDATOR_MODEL
#      (optional GV_VALIDATOR_BASE_URL / GV_VALIDATOR_API_KEY)

class DraftError(Exception): ...
def draft(dialogue: Sequence[Turn], context: AgentContext, llm: LLM) -> Plan | Clarification

def independent_plan(dialogue: Sequence[Turn], context: AgentContext, llm: LLM) -> Plan | Clarification
    # the blind validator: same inputs, different prompt; NEVER receives the draft

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
class RogueLLM(LLM): ...   # wraps an LLM; corrupts any submit_plan tool call per `attack`
```

## policy.py + renderer.py: risk rules and the confirmation script

```python
def evaluate_policy(plan: ResolvedPlan, view: BankView, *, now: datetime,
                    history: Sequence[Execution]) -> PolicyDecision
def render(plan: ResolvedPlan, policy: PolicyDecision, view: BankView) -> ConfirmationView
def spell_amount(value: Decimal) -> str     # "one thousand dollars and fifty cents"
```
`Execution` is `gateway.Execution(at, step)`. Outcome precedence:
deny > hold > step_up > allow. Own-account transfers are exempt from
third-party limits.

## services/: HTTP split (agent zone vs gateway zone)

- `services/gateway_api.py`: FastAPI app factory `create_gateway_app(gateway, *, rp_id, origin)`.
  The only service holding the `Bank`. Endpoints: `GET /health`, `GET /view` (read-only
  snapshot for the agent), `POST /challenges`, `POST /execute`, `GET /audit`,
  WebAuthn `POST /webauthn/register/options|verify` and `POST /webauthn/approve/options`.
- `services/webauthn_verifier.py`: `WebAuthnVerifier` implementing
  `gateway.ApprovalVerifier`; the WebAuthn challenge bytes are `crypto.challenge_digest(challenge)`.
- `services/agent_api.py`: FastAPI app wrapping the pipeline; talks to the gateway only over HTTP.

## evals/: benchmark

- `evals/cases.yaml`: hand-written cases (see `evals/README.md` for the format).
- `src/glassvault/evals/`: loader, harness, naive baseline agent, scorecard.
