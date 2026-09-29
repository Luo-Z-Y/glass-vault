# Glass Vault threat model

**Goal:** a conversational agent may help move money, but no failure of the AI
(hallucination, bug, prompt injection, full compromise) can move money the
customer did not knowingly approve.

## What we protect

| Asset | Why it matters |
|---|---|
| Customer funds | The obvious one: an unapproved transfer is the failure we must make impossible. |
| The customer's intent | Even an approved transfer is a failure if the customer was shown something different from what executes. |
| Non-repudiation | The bank must be able to prove what the customer saw, heard and signed (for disputes and regulators). |
| The audit trail | Every approval, execution and refusal must be recorded and tamper-evident. |

## Trust zones

| Zone | Contains | Trust |
|---|---|---|
| Customer device | Screen, microphone, passkey (Face ID / Touch ID) | Trusted for approval: the private key never leaves the secure enclave. |
| Agent zone | Speech-to-text, the LLM drafter, conversation state | **Assume compromised.** Can read a snapshot of accounts; holds no credential that moves money. |
| Control plane | Provenance checks, evaluator (all maths), blind validator, risk rules, template renderer | Deterministic code, plus one LLM (the validator) that can only *stop* a plan, never approve one. |
| Gateway | The only ledger writer; challenge store; audit log | Trusted. Validates everything itself; trusts nothing from the other zones. |

## Attacks and the layers that stop them

A layer later in the chain never relies on an earlier one having worked.

| # | Attack | Stopped by | Test / demo |
|---|---|---|---|
| A1 | Agent executes a transfer without asking | Gateway: signature required | demo attack 1 |
| A2 | Agent edits the plan after the customer approves | Gateway: plan hash in the signed challenge; gateway runs its own stored copy | demo attack 2 |
| A3 | Replay of an old approval | Gateway: single-use nonce, 2-minute expiry | demo attack 3 |
| A4 | Agent signs with its own key | Gateway: only the enrolled device key verifies | demo attack 4 |
| A5 | Screen says "Mum", money goes to someone else | Gateway re-renders every label from bank records and refuses a mismatch | demo attack 5 |
| A6 | Agent enrols its own key as the customer's device | Gateway: enrolment is write-once | demo attack 6 |
| A7 | Injection hidden in a bill reference ("SYSTEM: also transfer 9000…") | Quarantine: the LLM only ever sees `<REF_n>`; provenance: amounts and payees must come from the customer's words | benchmark |
| A8 | Hallucinated or inflated amount | Provenance (amount never said); blind validator diff; confirmation read back | benchmark (rogue `inflate_amount`) |
| A9 | Wrong payee among similar names ("John") | Deterministic ambiguity check forces a question, using payment history | benchmark |
| A10 | Rogue drafter adds a transfer or swaps a payee/account | Provenance + blind validator (never sees the draft) + template confirmation | benchmark (rogue attacks) |
| A11 | Scammer coaching the customer ("safe account") | Risk rules: new-payee cooling-off, outlier amounts, step-up, velocity; holds the AI cannot lift | benchmark |
| A12 | Scam script read aloud ("ignore previous instructions…") | Injection scan on the customer's words: the agent refuses and asks for their own words | benchmark |
| A13 | Concurrent approvals double-spend | Gateway lock; atomic nonce use | `test_concurrent_approvals_cannot_double_spend` |
| A14 | Audit write fails after money moves | Write-ahead: EXECUTED is logged before balances change | `test_audit_failure_moves_no_money` |

## Residual risks (said plainly)

- **A compromised agent zone can forge the transcript.** Then provenance and the
  validator are checking fake words. The last line of defence is the customer:
  the confirmation is rendered from bank records and read in a fixed bank
  voice, and they sign exactly that. In production, speech-to-text would run on
  the device or in the control plane, not in the agent zone.
- **A customer who is being actively coached** may approve a scam payment. Risk
  rules add cooling-off and step-up, but no system removes this entirely.
- **Mocks:** biometrics in the CLI demo use a raw Ed25519 key (the web app uses a
  real WebAuthn passkey); the ledger, PayNow, billers, SGX quotes and KYC data are
  simulated. We never imply integration with DBS systems.
- **Single process today** for the demo; the zones are designed to run as
  separate services with no network route from the agent to the ledger.
