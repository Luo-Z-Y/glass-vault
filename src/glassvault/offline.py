"""Rule-based stand-in for the LLM, so the demo runs without an API key.

It speaks the same tool protocol as a real model (a plan tool and a clarify
tool) and understands the demo's phrasings: transfers between own accounts,
PayNow to saved payees, bills, and SGX share buys, including "half of it".
It is deliberately simple; the real drafter is an LLM. Everything downstream
(provenance, validator diff, policy, gateway) treats its output exactly like
an LLM's.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from typing import Any

from .bank import BankView
from .context import payee_matches
from .llm import LLMResponse, tool_response
from .numbers import extract_amounts, extract_fractions

UNSUPPORTED = re.compile(r"\b(loan|borrow|crypto|bitcoin|btc|usd|us dollars?|overseas|remit|wire|invest in funds?)\b", re.I)
VERB = r"(?:pay|send|move|transfer|put|give|buy|top\s+up|invest)"
CLAUSE_SPLIT = re.compile(rf"(?:[,;.]|\bthen\b|\balso\b|\band\b(?=\s+(?:then\s+)?{VERB}\b))", re.I)
SHARES = re.compile(r"\b(shares?|stocks?|invest)\b", re.I)
BILL = re.compile(r"\bbill\b", re.I)
STOPWORDS = {"send", "pay", "give", "transfer", "move", "to", "my", "the", "a", "for", "please", "dollars",
             "dollar", "bucks", "sgd", "s", "and", "some", "money", "via", "paynow", "on", "of", "it"}


class OfflineLLM:
    def __init__(self, view: Callable[[], BankView], *, name: str = "offline"):
        self._view = view
        self.name = name

    def chat(self, messages: list[dict], *, tools: list[dict] | None = None,
             tool_choice: str | dict | None = None, temperature: float = 0.0) -> LLMResponse:
        names = [t["function"]["name"] for t in tools or []]
        plan_tool, clarify_tool = names[0], names[1]
        customer, agent = _turns(messages)
        result = interpret(customer, agent, self._view())
        if "steps" in result:
            return tool_response(plan_tool, result)
        return tool_response(clarify_tool, result)


def _turns(messages: Sequence[dict]) -> tuple[list[str], list[str]]:
    """Customer and agent turns from either the drafter's chat or the validator's transcript."""
    customer: list[str] = []
    agent: list[str] = []
    for m in messages:
        content = m.get("content") or ""
        if m["role"] == "user" and "<transcript>" in content:
            body = content.split("<transcript>", 1)[1].split("</transcript>", 1)[0]
            for line in body.strip().splitlines():
                try:
                    turn = json.loads(line)
                except json.JSONDecodeError:
                    continue
                (customer if turn.get("speaker") == "CUSTOMER" else agent).append(turn.get("text", ""))
        elif m["role"] == "user" and not content.startswith("("):
            customer.append(content)
        elif m["role"] == "assistant" and content and not m.get("tool_calls"):
            agent.append(content)
    return customer, agent


def interpret(customer: Sequence[str], agent: Sequence[str], view: BankView) -> dict[str, Any]:
    if not customer:
        return _ask("missing_info", "What would you like to do?")
    request = customer[0]
    later = " ".join(customer[1:])
    if UNSUPPORTED.search(request):
        return _ask("unsupported", "In this demo I can move money between your accounts, send PayNow, "
                                   "pay bills and buy SGX shares. What would you like to do?")

    steps: list[dict[str, Any]] = []
    for clause in (c.strip() for c in CLAUSE_SPLIT.split(request)):
        if not clause:
            continue
        step_id = f"t{len(steps) + 1}"
        amount, evidence = _amount(clause, steps)
        if SHARES.search(clause):
            symbol, said = _symbol(clause, view)
            if symbol is None:
                return _ask("missing_info", "Which shares would you like to buy?")
            if amount is None:
                return _ask("ambiguous_amount", f"How much would you like to invest in {view.market.name(symbol)}?")
            source = steps[-1]["to_account"] if steps and steps[-1]["type"] == "own_transfer" else "SAV"
            steps.append({"id": step_id, "type": "equity_buy", "from_account": source, "symbol": symbol,
                          "amount": amount, "evidence": {**evidence, "symbol": said}})
        elif BILL.search(clause):
            biller, said = _biller(clause, view)
            if biller is None:
                return _ask("missing_info", "Which bill would you like to pay?")
            steps.append({"id": step_id, "type": "bill_payment", "from_account": "SAV", "biller_id": biller,
                          "amount": amount or {"full_amount_due": True},
                          "evidence": {**evidence, "biller_id": said}})
        elif (target := _own_account(clause, view)) is not None:
            to_account, said = target
            if amount is None:
                return _ask("ambiguous_amount", f"How much would you like to move to your {view.accounts[to_account].name}?")
            source = next(a for a in view.accounts if a != to_account)
            steps.append({"id": step_id, "type": "own_transfer", "from_account": source, "to_account": to_account,
                          "amount": amount, "evidence": {**evidence, "to_account": said}})
        elif re.search(rf"\b{VERB}\b", clause, re.I):
            phrase, candidates = _payee(clause, view)
            if not candidates:
                continue
            if len(candidates) > 1:
                resolved = [p for p in candidates if later and _mentions(later, p, view)]
                if len(resolved) != 1:
                    return _ask("ambiguous_payee", f"Which {phrase} do you mean?",
                                [{"label": view.payees[p].legal_name, "value": p} for p in candidates])
                candidates = resolved
            if amount is None:
                return _ask("ambiguous_amount", f"How much would you like to send to {view.payees[candidates[0]].legal_name}?")
            steps.append({"id": step_id, "type": "paynow", "from_account": "SAV", "payee_id": candidates[0],
                          "amount": amount, "evidence": {**evidence, "payee_id": phrase}})
    if not steps:
        return _ask("missing_info", "Sorry, I didn't catch that. You can move money, send PayNow, pay a bill or buy shares.")
    return {"steps": steps}


def _ask(reason: str, question: str, options: list[dict[str, str]] | None = None) -> dict[str, Any]:
    return {"reason": reason, "question": question, **({"options": options} if options else {})}


def _amount(clause: str, steps: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, dict[str, str]]:
    fractions = extract_fractions(clause)
    if fractions and steps:
        f = fractions[0]
        return ({"fraction_of_step": {"step": steps[-1]["id"], "numerator": f.numerator, "denominator": f.denominator}},
                {"amount": f.text})
    amounts = extract_amounts(clause)
    if amounts:
        return {"value": str(amounts[0].value)}, {"amount": amounts[0].text}
    return None, {}


def _symbol(clause: str, view: BankView) -> tuple[str | None, str]:
    for symbol in ("D05", "O39", "Z74"):
        name = view.market.name(symbol)
        if not name:
            continue
        word = name.split()[0]
        if m := re.search(rf"\b{re.escape(word)}\b", clause, re.I):
            return symbol, m.group(0)
    return None, ""


def _biller(clause: str, view: BankView) -> tuple[str | None, str]:
    for biller in view.billers.values():
        word = biller.name.split()[0]
        if m := re.search(rf"\b{re.escape(word)}\b", clause, re.I):
            return biller.id, m.group(0)
    return None, ""


def _own_account(clause: str, view: BankView) -> tuple[str, str] | None:
    for account in view.accounts.values():
        word = account.name.split()[0]
        if m := re.search(rf"\b(?:to|into)\s+(?:my\s+)?({re.escape(word)})\b", clause, re.I):
            return account.id, m.group(1)
    return None


def _payee(clause: str, view: BankView) -> tuple[str, list[str]]:
    """The shortest-candidate-list phrase in the clause that names saved payees."""
    tokens = re.findall(r"\S+", clause)
    best: tuple[str, list[str]] = ("", [])
    for size in (1, 2, 3):  # shortest phrase wins ties, so we quote as little as possible
        for i in range(len(tokens) - size + 1):
            gram = [t.strip(".,;:!?") for t in tokens[i:i + size]]
            if any(not w.isalpha() for w in gram) or gram[0].lower() in STOPWORDS or gram[-1].lower() in STOPWORDS:
                continue
            phrase = " ".join(gram)  # contiguous words, so it is a verbatim quote
            found = payee_matches(phrase, view)
            if found and (not best[1] or len(found) < len(best[1])):
                best = (phrase, found)
    return best


def _mentions(text: str, payee_id: str, view: BankView) -> bool:
    return view.payees[payee_id].legal_name.lower() in text.lower() or payee_matches(text, view) == [payee_id]
