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

- [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md): 14 attacks and the layer that stops each
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): trust zones, invariants, gateway error codes
- [docs/CONTRACTS.md](docs/CONTRACTS.md): module interfaces

## Run it

```bash
uv sync
uv run pytest                          # ~1,060 tests
uv run python -m glassvault.demo       # CLI: signed happy path + 6 blocked attacks
uv run python -m glassvault.web.server # web demo on http://localhost:8000
uv run python -i scripts/playground.py # try to rob the gateway yourself
```

Open the web demo on `localhost` (not `127.0.0.1`) so passkeys work. Without an
API key it runs on a rule-based offline stand-in for the LLM, so the whole flow
works out of the box.

### With a real LLM

Copy `.env.example` to `.env` and fill it in. Use a different model family for
the validator so its check is genuinely independent. For Tencent Hunyuan on
Tencent Cloud International TokenHub:

```
GV_LLM_BASE_URL=https://tokenhub-intl.tencentcloudmaas.com/v1
GV_LLM_API_KEY=...
GV_DRAFTER_MODEL=hy3
GV_VALIDATOR_MODEL=deepseek-v4-flash
```

### Benchmark

```bash
uv run python -m glassvault.evals --agent naive      --out evals/results/naive.json
uv run python -m glassvault.evals --agent glassvault --out evals/results/glassvault.json
```

The naive baseline (one LLM call, raw bank data, no checks) runs against a
plain gateway; Glass Vault runs with its full control plane. A careless
simulated customer signs every confirmation, so safety has to come from the
automated defences.

## Layout

| Path | Role | Trust zone |
|---|---|---|
| `schema.py` | Plan (LLM draft), ResolvedPlan (what is signed), Challenge, Turn | shared |
| `context.py` | What the LLM may see; untrusted text quarantined as `<REF_n>` | agent zone |
| `llm.py`, `drafter.py` | OpenAI-compatible client; the drafter's prompt and tools | agent zone |
| `offline.py` | Rule-based LLM stand-in for keyless demos | agent zone |
| `numbers.py` | Deterministic amount grammar ("a grand", "5k", "fifty bucks") | control plane |
| `pipeline.py` | Provenance checks and the orchestration of every stage | control plane |
| `evaluator.py` | Plan -> ResolvedPlan; all money arithmetic in Decimal | control plane |
| `validator.py` | Blind re-derivation and plan diff | control plane |
| `policy.py`, `renderer.py` | KYC and risk rules; the bank-rendered confirmation and voice script | control plane |
| `gateway.py`, `audit.py` | The only code that moves money; hash-chained audit log | gateway |
| `services/` | FastAPI gateway service with WebAuthn passkeys | gateway |
| `web/` | Demo server, customer app and security console | demo |
| `rogue.py`, `evals/` | Rogue-AI attacks and the benchmark | testing |

## Built with

Claude Code (architecture, safety core, pipeline, tests) and Tencent WorkBuddy /
CodeBuddy (demo UI stylesheet and more; see [docs/codebuddy-log.md](docs/codebuddy-log.md)
and the screenshots in `docs/codebuddy/`).
