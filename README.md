# Glass Vault

**The AI drafts. Only your face moves money.**

Entry for the DBS track ("Direct Conversational Transaction Agent") of the
AI CAN DO IT Tencent Cloud Hackathon Singapore 2026.

A customer says what they want ("move a grand to my joint account, buy DBS
shares with half of it, and pay my SP bill"). An LLM drafts a typed plan,
deterministic code does all the maths, the customer approves with a passkey,
and a zero-trust gateway executes only what was signed. Then we switch the AI
to rogue mode on stage and show it still cannot move a cent.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for trust zones and invariants.

## Run it

```bash
uv sync
uv run pytest
uv run python -m glassvault.demo
```

## Layout

| Module | Role | Trust zone |
|---|---|---|
| `schema.py` | Plan (LLM output), ResolvedPlan (what gets signed), Challenge | shared |
| `bank.py` | Mock accounts, payees, billers, SGX quotes | bank |
| `evaluator.py` | Plan -> ResolvedPlan; all money arithmetic | control plane |
| `crypto.py` | Canonical JSON, plan hashes, device signatures | shared |
| `gateway.py` | The only code that moves money | gateway |
| `audit.py` | Hash-chained audit log | gateway |
| `demo.py` | Happy path + four rogue-agent attacks | - |

## Roadmap

- **Week 1 (to 5 Oct):** text-only spine: schema, evaluator, mock bank, gateway, audit, tests. *Done:* core. *Next:* LLM drafter with tool calling, FastAPI split into agent / gateway services, WebAuthn passkey in the browser.
- **Week 2 (6-12 Oct):** untrusted-text quarantine, clarification loop, blind validator, number grammar, risk rules (KYC tier, velocity, new-payee cooling, step-up), rogue mode, ASR in / TTS readback.
- **Week 3 (13-15 Oct):** 150-200 case benchmark vs a naive agent, demo video, write-up. Submit 15 Oct.

## Built with

Claude Code (architecture, safety core, evals) and Tencent CodeBuddy (see
`docs/codebuddy-log.md` for what CodeBuddy built, with screenshots).
