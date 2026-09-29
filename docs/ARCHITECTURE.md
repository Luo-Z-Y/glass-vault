# Glass Vault architecture

## Trust zones

```
 customer device (trusted)        agent zone (assume compromised)        control plane (deterministic)        gateway (only ledger writer)
 ┌──────────────────────┐        ┌────────────────────────────┐        ┌─────────────────────────────┐      ┌───────────────────────────┐
 │ push-to-talk / text  │──────▶ │ ASR -> quarantine -> LLM   │──────▶ │ evaluator (Decimal maths)   │────▶ │ verify signature, nonce,  │
 │ confirmation screen  │        │ drafter -> Plan (JSON)     │        │ blind validator + diff      │      │ expiry, plan hash         │
 │ passkey (Face ID)    │◀────── │ clarification questions    │        │ policy rules                │      │ re-simulate, apply atomic │
 └──────────────────────┘        └────────────────────────────┘        │ template renderer (+ TTS)   │      │ hash-chained audit log    │
            │  signs Challenge{plan_hash, nonce, expiry}               └─────────────────────────────┘      └───────────────────────────┘
            └──────────────────────────────────────────────────────────────────────────────────────────────────────────▲
```

The agent zone can read a snapshot of the customer's accounts and can ask the
gateway for a challenge. It has no credential that moves money.

## Invariants

| # | Invariant | Enforced by | Status |
|---|---|---|---|
| I1 | Only the gateway can change balances. | `Gateway` owns the `Bank`; agent side gets a `BankView` snapshot. Week 1: separate services, no network route from agent to ledger. | in code (process split: week 1) |
| I2 | Nothing executes without a signature from the customer's registered device over `Challenge{plan_hash, nonce, customer_id, expires_at}`. | `Gateway._execute` | done |
| I3 | Any change to the plan after approval, down to one cent, is rejected. | plan hash in the signed challenge | done |
| I4 | An approval executes at most once, and only before it expires. | nonce set, challenge TTL | done |
| I5 | All money arithmetic is deterministic code in `Decimal`; the LLM never computes an amount. | `evaluator.py` | done |
| I6 | Execution is all-or-nothing, and re-checked against live balances and quotes. | `Gateway._simulate` | done |
| I7 | What the customer sees and hears is rendered from bank records, never from LLM text. | `evaluator.destination_label`; the gateway re-renders every label and refuses a mismatch before issuing a challenge | done (renderer: week 2) |
| I8 | Untrusted text (bill references, nicknames) never reaches an LLM prompt verbatim. | quarantine step | week 2 |
| I9 | The validator never sees the draft it checks. | blind validator | week 2 |
| I10 | Every approval, execution and rejection is in a tamper-evident log, written before money moves. | `AuditLog` hash chain; write-ahead in `Gateway._execute`; catch-all rejection audit | done |
| I11 | Device enrolment is write-once; the agent cannot swap in its own key. | `Gateway.register_device` | done |
| I12 | The gateway executes its own validated copy of the plan, never the caller's object, and only one execution runs at a time. | `_Pending` store, strict re-parse, gateway lock | done |
| I13 | Time comes from the gateway: quote and approval expiry use the gateway clock. | `Market.bind_clock` | done |

## Gateway error codes

| Code | HTTP | Attack it stops |
|---|---|---|
| `MALFORMED_PLAN` | 400 | sub-cent or NaN amounts, broken text, equity fields on the wrong step |
| `SIGNATURE_REQUIRED` | 401 | agent executes without asking the customer |
| `SIGNATURE_INVALID` | 401 | agent signs with its own key |
| `PAYLOAD_HASH_MISMATCH` | 403 | plan edited after the customer approved it |
| `UNKNOWN_CHALLENGE` | 404 | made-up approval |
| `NONCE_CONSUMED` | 409 | replaying an old approval |
| `DEVICE_ALREADY_REGISTERED` | 409 | agent enrolling its own key as the customer's device |
| `QUOTE_EXPIRED` | 409 | executing a share buy at a stale price |
| `STALE_PLAN` | 409 | displayed destination differs from bank records ("show Mum, pay someone else"), or plan no longer matches live accounts, payees or quotes |
| `CHALLENGE_EXPIRED` | 410 | using an approval after its window |
| `INSUFFICIENT_FUNDS` | 422 | balances changed since approval |
| `INTERNAL_ERROR` | 500 | anything unexpected; audited, and no money moves |

## Adversarial review (29 Sep)

An independent multi-agent review attacked the week-1 core and confirmed these,
all now fixed with regression tests in `tests/test_security_regressions.py`:
display spoofing (show one payee, pay another), device-key replacement,
concurrent double-spend, sub-cent amounts, caller-controlled quote expiry,
money moving before the audit write, and unaudited non-gateway errors.

## Known simplifications

- `DeviceKey` (raw Ed25519) stands in for a WebAuthn passkey until the web app exists.
- Share buys ignore fees and use SGX unit-share lots (quantity 1+).
- Everything is in one process for now; the zones become separate services next.
