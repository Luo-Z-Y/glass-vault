# Submission: form fields

Paste each block into the matching field of the Banking Track section.

## Project Title

Glass Vault

## Short Blurb (under 10 words, max 80 characters)

The AI drafts. Only your face moves money.

## Project Link

https://glassvault.luo-zy.com

Paste the URL only. It is the live demo, which asks for a sign-in so a public link cannot run up the API bill: user `judge`, password `QgSQnHE8CRdvy7Cm`. The last line of the Project Description repeats both, with the GitHub link to the source code that this field used to hold.

## Project Description

```
Track: Banking, Direct Conversational Transaction Agent (DCTA), DBS. Scenario 1: voice-enabled payments, with KYC and risk controls.
Result: 0 unsafe executions across 130 benchmark cases (a naive single-LLM agent on the same sets: 43 unsafe, S$158,482.85 that should not have moved). 12 of 12 rogue-AI attacks frozen.
Tencent Cloud: Hunyuan Hy3 (the blind validation agent), TRTC FlowTTS and speech recognition (the bank voice and push-to-talk), ADP (the Help assistant), WorkBuddy (independent QA that wrote our held-out test sets).

1. PROJECT OVERVIEW

Glass Vault lets a customer bank in one sentence, by voice or text: "Move a grand to my joint account, then buy DBS shares with half of it." An AI drafts the transactions. Deterministic bank code traces every amount, recipient and share to the customer's words, computes every dollar and applies the risk rules. The customer approves a confirmation drawn by bank code with a passkey (Face ID / Touch ID), and a zero-trust gateway executes only that signed plan.

Users: customers who find multi-screen menus slow, including older customers and anyone who prefers to speak; and the bank's fraud and compliance teams, who need every AI-assisted payment to be provable.

Value: the ease of a personal banker with the guarantees of a signed form. The AI can be wrong, tricked or rogue and still cannot move money the customer did not see and sign.

2. REAL-WORLD SCENARIO INSIGHTS

- Singapore lost about S$913.1 million to scams in 2025 across 37,308 cases; 81.8% of reported scams involved self-effected transfers, where the victim was manipulated into paying (SPF Annual Scam and Cybercrime Brief 2025). The MAS-IMDA Shared Responsibility Framework covers unauthorised phishing transactions, not transfers customers make themselves.
- A conversational agent adds new failure modes: a hallucinated amount, the wrong "John", a scam message read aloud, an instruction hidden in a bill reference or a sender's PayNow note. An agent that can call the payment API is one bug or one injected sentence from draining an account.
- A chat log does not prove what the customer agreed to.

Core problem: let AI understand messy, multi-step speech while making it technically impossible for the AI to move money on its own.

3. COMPREHENSIVE SOLUTION DESIGN

Four trust zones: the customer's device (trusted: chat, push-to-talk, confirmation, passkey); the agent zone (assumed compromised: an LLM that can only fill in a typed form); the control plane (deterministic code, plus one validator AI that can only stop things); the gateway (the only code that changes balances).

DBS capabilities:
- Multi-intent parsing: the DeepSeek drafter returns a typed JSON plan through a forced tool call. "Half of it" stays a reference that code computes in Decimal.
- Ambiguity: deterministic checks force a short question from bank records: two payees called John, "fifteen or fifty" judged against the payee's history, an amount over 10x the largest past payment to that payee.
- Validation agent: Tencent Hunyuan Hy3, a different model family, reads the same transcript and quarantined bank data in parallel but never sees the draft (a model shown a draft tends to agree with it). Code compares both readings; any difference in payee, amount, source account or asset freezes the request.
- Biometric HITL: a confirmation drawn by code from templates and bank records, read aloud by a TRTC FlowTTS bank voice that can only speak bank-rendered scripts, then the passkey prompt with user verification. The signature covers the plan hash, a one-time nonce, a 2-minute expiry, and the hashes of the spoken script and the conversation.
- Zero-trust gateway: re-derives each recipient's name from bank records, re-simulates against live balances, re-runs the risk rules, executes its own stored copy once, and refuses anything unsigned, replayed, expired or changed by one cent.
- Injection shielding: the customer's words are scanned for instructions aimed at the AI and for hidden or look-alike characters. Bill references reach the model only as placeholders; payee names are sanitised, and replaced too if they read like instructions. Third-party statement text never reaches an LLM or the bank voice.
- KYC, risk, velocity: payments to others and share buys are refused until KYC is verified; limits follow the KYC tier. New payees: 12-hour cooling-off and S$1,000 for the first 7 days. 5+ payments to others in 10 minutes are paused. S$5,000+ leaving the accounts, or over 10x the largest past payment to that payee, needs an out-of-band token code for that one approval (simulated; 3 wrong codes void it).
- Help, kept out of the money path: a bank FAQ assistant on Tencent ADP (knowledge-base Q&A over the bank's FAQ, with the FAQ itself as a no-model fallback). It has no tools and no account data; its answers appear in their own panel labelled as AI-written and never enter the payment dialogue, the audit log or the bank voice.
- Audit: a SHA-256 hash-chained log of every turn (transcript with voice/text flag, the drafter's output, the validator's reading once a draft passes provenance, the diff, every check), challenge, signature and execution. A conversation-bound approval cannot execute until that exact conversation, plan and spoken script are on file. The demo stores transcripts, not audio.

How prompts drive the AI: prompts produce structure, through tool schemas (submit_plan, ask_clarification, answer_enquiry), forced tool calls for the drafter, temperature 0 and one repair round. The drafter must quote the customer's words as evidence for each field they stated; a provenance check finds each quote in the transcript or freezes the plan. The validator has its own audit-procedure prompt. Confirmations, answers, outcome messages and all speech come from code templates; the only model wording a customer can see is a short clarifying question, on screen only.

Stack: Python 3.12, FastAPI, Pydantic v2, WebAuthn; DeepSeek drafter; Tencent Hunyuan Hy3 validator (via OpenRouter, pinned to Tencent's endpoint); TRTC FlowTTS and sentence recognition; Tencent ADP (Help). Built with Claude Code and WorkBuddy, which acted as an independent QA tester and wrote our two held-out test sets without seeing our prompts or code.

4. BUSINESS VALUE

A careless simulated customer approves every confirmation, so safety must come from the system.
- 90 development cases: 0 unsafe executions, 12 of 12 rogue attacks frozen, 89 of 90 fully correct (the miss: a false freeze, safe but friction), 21 of 21 needed questions asked and none unneeded, about 2.1 s per turn. Naive agent on the 84 non-enquiry cases: 36 unsafe, S$145,146.41.
- 40 held-out cases by WorkBuddy, each set run once untuned, then its gaps became general rules. First runs: 0 and 1 unsafe (a S$481.44 share buy from the wrong default account). Current build: 0 unsafe, 33 of 40 fully correct, every miss safe: a clarifying question or a freeze (naive: 7 unsafe, S$13,336.44).
- 1,307 automated tests; 18 attacks mapped to the layer that stops each.

Impact: a transfer then a share buy takes one sentence and one approval (42 of 43 routine requests in one touch). Against fraud: cooling-off, holds, step-up and refusing scripts aimed at the AI; a customer being actively coached remains the hardest case.

Responsible AI: the AI never decides who is suspicious; the same rules apply to everyone. Live demo: switch the drafter to rogue mode and watch each attack freeze on the bank's security console.

Feasibility: standard passkeys (FIDO2 / WebAuthn); the gateway also runs as its own HTTP service (our integration tests drive the pipeline through it) and could front an existing payment API; models are swappable. With no keys the demo uses browser speech and a rule-based stand-in; a model failure fails closed. Simulated: ledger, PayNow, billers, share quotes, KYC data, token.

5. WHAT SETS IT APART

- The validator is blind: it never sees the draft, so it cannot simply agree with it. Code, not a model, compares the two readings.
- Provenance: every amount, payee and account must trace to the customer's own words, or the plan freezes.
- Real passkeys, not a simulated button: the customer's signature covers the exact plan, the spoken script and the conversation.
- Safety is measured: a naive-agent baseline, two held-out test sets we did not write, and a live rogue-AI mode.
- Runs end to end with no keys (offline stand-ins), so judges can run the whole flow themselves.

Live demo: https://glassvault.luo-zy.com (user judge / password QgSQnHE8CRdvy7Cm). Source code: https://github.com/Luo-Z-Y/glass-vault
```

## WorkBuddy/CodeBuddy/Miora Conversation History (upload)

Upload these three, in order (from `docs/codebuddy/`):
1. `2026-09-29-11-workbuddy-heldout2-prompt.jpg`: the brief to WorkBuddy as an independent QA tester (bank test data only, no prompts or code).
2. `2026-09-29-12-workbuddy-heldout2-drafting.jpg`: WorkBuddy reasoning about tricky cases (two payees named John).
3. `2026-09-29-10-workbuddy-heldout-cases-delivered.jpg`: the delivered held-out test cases ("Completed 12m20s").

`docs/codebuddy-log.md` is the written development log of every WorkBuddy session.

## Demo Video (upload)

`video/out/glass-vault-demo.mp4` (7:05, 1080p, about 27 MB; built from PR #26, 14 October 2026). Subtitles: `video/out/glass-vault-demo.srt`. If the form asks for a link instead of a file, upload it as an unlisted video and paste that link here.

## Cover Image (upload)

`docs/submission/cover-380x216.png` (or the sharper `cover-1520x864.png`, also 16:9). Source: `cover.html`.
