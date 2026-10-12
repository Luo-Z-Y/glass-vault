# Glass Vault

**The AI drafts. Only your face moves money.**

Entry for the DBS track ("Direct Conversational Transaction Agent") of the
AI CAN DO IT Tencent Cloud Hackathon Singapore 2026.

A customer says what they want ("move a grand to my joint account, then buy
DBS shares with half of it"). An LLM drafts a typed plan; deterministic code
checks that every amount and payee came from the customer's own words, does
all the maths, and applies the risk rules; a second, blind LLM re-derives the
plan from the transcript alone and any disagreement freezes it. The customer
approves an exact, bank-rendered confirmation with a passkey (Face ID /
Touch ID), and a zero-trust gateway executes only what was signed. On stage we
switch the AI to rogue mode and show it still cannot move a cent.

- [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md): 18 attacks and the layer that stops each
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): architecture, tech-stack and codebase diagrams; the request pipeline, invariants, error codes, processes and known gaps
- [docs/CONTRACTS.md](docs/CONTRACTS.md): module interfaces

## Run it

```bash
uv sync
uv run pytest                          # ~1,300 tests
uv run python -m glassvault.demo       # CLI: signed happy path + 6 blocked attacks
uv run python -m glassvault.web.server # web demo on http://localhost:8000
uv run python -i scripts/playground.py # try to rob the gateway yourself
uv run python scripts/adp_setup.py     # optional: Help on Tencent ADP (see .env.example)
```

Open the web demo on `localhost` (not `127.0.0.1`) so passkeys work. Without an
API key it runs on a rule-based offline stand-in for the LLM, so the whole flow
works out of the box. With Tencent RTC keys in `.env` (see `.env.example`), the
bank voice and push-to-talk run on TRTC text-to-speech and speech recognition;
without them, the browser's own speech is used. The Help panel answers general
questions about the demo bank from `src/glassvault/faq.md`: through a Tencent
ADP knowledge-Q&A app once `scripts/adp_setup.py` has created it and written
`GV_ADP_APP_KEY` to `.env`, and from the FAQ itself (keyword match) without one.
Help has no tools and sees no account data, and its answers never enter the
payment chat or the bank voice.

### With a real LLM

Copy `.env.example` to `.env` and add your key. The defaults use DeepSeek's own
API, with `deepseek-flash` drafting and `deepseek-v4-pro` validating, and thinking
mode off for both roles (in thinking mode DeepSeek rejects forced tool calls).
A validator from a different model family makes its check more independent;
our results below use Tencent Hunyuan Hy3 through OpenRouter (see
`.env.example`). On Tencent Cloud International TokenHub, set:

```
GV_VALIDATOR_BASE_URL=https://tokenhub-intl.tencentcloudmaas.com/v1
GV_VALIDATOR_API_KEY=...
GV_VALIDATOR_MODEL=hy3
```

### Benchmark

```bash
uv run python -m glassvault.evals --agent naive      --out evals/results/naive.json
uv run python -m glassvault.evals --agent glassvault --out evals/results/glassvault.json
uv run python -m glassvault.evals --agent glassvault --cases evals/heldout.yaml --out evals/results/heldout-glassvault.json
```

The naive baseline (one LLM call, raw bank data, no checks) runs against a
plain gateway; Glass Vault runs with its full control plane. A careless
simulated customer signs every confirmation, so safety has to come from the
automated defences. `evals/heldout.yaml` and `evals/heldout2.yaml` are
independent test sets: 20 cases each that WorkBuddy wrote from a spec of the
bank data and outcome rules, without seeing our prompts or cases.

Results (12 Oct 2026; Glass Vault with a DeepSeek drafter and a Tencent Hunyuan
Hy3 validator; the naive baseline is one DeepSeek call that executes directly,
run on 29 Sep):

| Test set | Glass Vault: unsafe executions | Glass Vault: fully correct | Naive: unsafe executions (S$ moved) | Naive: fully correct |
|---|---|---|---|---|
| Development | **0** | 89/90 | 36 (S$145,146.41) | 42/84 |
| Held-out set 1 | **0** | 14/20 | 4 (S$5,355.00) | 7/20 |
| Held-out set 2 | **0** | 19/20 | 3 (S$7,981.44) | 9/20 |

Rogue-AI attacks frozen: 12/12 (naive: 0/12). The naive baseline ran before the
6 enquiry cases were added, hence 84 development cases. Every Glass Vault miss
is a safe one: a question asked or a request frozen, never money moved. The
first run of held-out set 2 found one wrong default source account; it became a
deterministic rule (commit 7f17108), so set 2 is no longer untouched for that
case. Full scorecards are in `evals/results/`.

## Layout

| Path | Role | Trust zone |
|---|---|---|
| `schema.py` | Plan (LLM draft), ResolvedPlan (what is signed), Challenge, Turn | shared |
| `bank.py` | Mock bank: accounts, payees, billers, SGX quotes; read-only `BankView` snapshots | shared |
| `context.py` | What the LLM may see; untrusted text quarantined as `<REF_n>` | agent zone |
| `llm.py`, `drafter.py` | OpenAI-compatible client; the drafter's prompt and tools | agent zone |
| `offline.py` | Rule-based LLM stand-in for keyless demos | agent zone |
| `voice.py` | Tencent RTC text-to-speech (the bank voice) and speech recognition (push-to-talk), for the web demo | demo |
| `faq.py`, `faq.md` | Help: the bank FAQ assistant on Tencent ADP, with the FAQ itself as the offline fallback; no tools, no account data | demo |
| `numbers.py` | Deterministic amount grammar ("a grand", "5k", "fifty bucks") | control plane |
| `pipeline.py` | Provenance checks and the orchestration of every stage | control plane |
| `evaluator.py` | Plan -> ResolvedPlan; all money arithmetic in Decimal | control plane |
| `validator.py` | Blind re-derivation and plan diff | control plane |
| `policy.py`, `renderer.py` | KYC and risk rules; the bank-rendered confirmation and voice script | control plane |
| `gateway.py`, `audit.py` | The only code that moves money; hash-chained audit log | gateway |
| `crypto.py`, `stepup.py` | Plan hashes and device signatures; step-up codes | gateway |
| `services/` | FastAPI gateway service with WebAuthn passkeys | gateway |
| `web/`, `demo.py` | Demo server, customer app and security console; CLI demo | demo |
| `rogue.py`, `evals/` | Rogue-AI attacks and the benchmark | testing |

## Built with

Claude Code (architecture, safety core, pipeline, UI, tests) and Tencent
WorkBuddy (the two independent held-out benchmark sets, `evals/heldout.yaml` and
`evals/heldout2.yaml`). Every
WorkBuddy session, including the attempts that did not deliver, is logged in
[docs/codebuddy-log.md](docs/codebuddy-log.md) with screenshots in `docs/codebuddy/`.
