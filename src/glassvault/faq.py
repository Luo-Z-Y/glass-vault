"""Help: the bank FAQ assistant. It answers general questions about the demo bank, and nothing else.

With GV_ADP_APP_KEY set, a question goes to a published Tencent ADP agent app
(Agent Development Platform, knowledge-base Q&A over ``faq.md``) through ADP's
HTTP SSE chat API. Without it, or when ADP fails, the answer is the best-matching
entry of ``faq.md`` itself, picked by keyword overlap: fixed text, no model.

Help is kept out of the money path by construction:

- It has no tools and no account data. The ADP app knows only the FAQ, and this
  module never reads the bank.
- Its words never enter the payment dialogue, so they can never count as
  something the customer said or the agent asked, and they never reach the
  bank voice, which reads only scripts the control plane rendered.
- An ADP answer is model-written, so the page shows it in its own panel,
  labelled as AI-written. Here it is reduced to plain text, capped in length, and
  withheld if it carries instruction-like text (``context.scan_text``).
"""

from __future__ import annotations

import json
import math
import re
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx

from .context import scan_text

FAQ_PATH = Path(__file__).with_name("faq.md")
ADP_CHAT_URL = "https://wss.lke.tencentcloud.com/adp/v2/chat"  # international site; China: wss.lke.cloud.tencent.com
MAX_QUESTION_CHARS = 300
MAX_ANSWER_CHARS = 1200
MAX_STREAM_CHARS = 256_000  # a whole ADP answer stream; a short FAQ answer is a few kilobytes
MAX_REFERENCES = 5

NO_MATCH = (
    "I can only answer general questions about how this demo bank works, such as limits, approvals, "
    "new payees, share buys or what the assistant can do. For your own accounts and payments, use the main chat."
)
WITHHELD = (
    "I can't show that answer here. Please rephrase your question, or ask the main assistant about "
    "your own accounts."
)


class HelpError(Exception):
    """No usable answer from Help. `code` is ADP's error code, or ours."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class FaqEntry:
    question: str
    answer: str
    also: tuple[str, ...] = ()  # other ways customers ask it ("Also asked as")


@dataclass(frozen=True)
class HelpAnswer:
    text: str
    source: Literal["adp", "offline"]
    matched: str | None = None  # offline: the FAQ question answered; None if nothing matched
    references: tuple[str, ...] = ()  # ADP: names of the knowledge it cited
    note: str | None = None  # e.g. why the built-in FAQ answered instead of ADP


# --- The knowledge base -------------------------------------------------------------


def load_faq(text: str | None = None) -> tuple[FaqEntry, ...]:
    """The entries of faq.md: each "## question" heading, its "Also asked as" line, and its answer."""
    text = FAQ_PATH.read_text(encoding="utf-8") if text is None else text
    entries = []
    for block in re.split(r"^## ", text, flags=re.M)[1:]:
        heading, _, body = block.partition("\n")
        lines = body.strip().splitlines()
        also: tuple[str, ...] = ()
        if lines and lines[0].startswith("Also asked as:"):
            also = tuple(p.strip() for p in lines[0].removeprefix("Also asked as:").split(",") if p.strip())
            lines = lines[1:]
        paragraphs = re.split(r"\n\s*\n", "\n".join(lines))
        answer = "\n\n".join(" ".join(p.split()) for p in paragraphs if p.strip())
        if heading.strip() and answer:
            entries.append(FaqEntry(heading.strip(), answer, also))
    return tuple(entries)


# --- Offline answers: keyword overlap over the FAQ ------------------------------------

_STOPWORDS = frozenset("""
a about after again all am an and any are as at be been before but by can could did does doing for
from get got had has have how i if in into is it its just me more most my no not of on or our out so some
such than that the their them then there these they this to too up us was we were what when where which
while who why will with would you your yours
""".split())
# Different words for one idea, mapped to the word the FAQ uses.
_SYNONYMS = {
    "otp": "token", "code": "token", "pin": "token", "2fa": "token",
    "mom": "mum", "mother": "mum", "ma": "mum",
    "maximum": "limit", "max": "limit", "cap": "limit", "capped": "limit",
    "stock": "share", "stocks": "share", "equity": "share", "invest": "share", "investing": "share",
    "faceid": "passkey", "biometric": "passkey", "fingerprint": "passkey",
    "stuck": "held", "blocked": "held", "paused": "held", "hold": "held",
    "transfer": "send", "transfers": "send", "pay": "send", "paying": "send",
    "safe": "safety", "scammer": "scam", "fraud": "scam",
}


def _stem(word: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


_CANNOT = re.compile(r"\b(?:can['’]?t|cannot|can not|unable to|not supported|unsupported)\b")


def _tokens(text: str) -> set[str]:
    text = _CANNOT.sub(" unsupported ", text.lower().replace("face id", "faceid"))  # keep "can't" apart from "can"
    words = re.findall(r"[a-z0-9]+", text)
    return {_stem(_SYNONYMS.get(w, w)) for w in words if w not in _STOPWORDS and len(w) > 1}


@dataclass(frozen=True)
class _Index:
    entries: tuple[FaqEntry, ...]
    asked: tuple[frozenset[str], ...]  # question + "also asked as" words, per entry
    told: tuple[frozenset[str], ...]  # answer words, per entry
    idf: Mapping[str, float]

    @classmethod
    def build(cls, entries: Sequence[FaqEntry]) -> _Index:
        asked = tuple(frozenset(_tokens(" ".join([e.question, *e.also]))) for e in entries)
        told = tuple(frozenset(_tokens(e.answer)) for e in entries)
        counts: dict[str, int] = {}
        for a, t in zip(asked, told):
            for word in a | t:
                counts[word] = counts.get(word, 0) + 1
        idf = {w: math.log(1 + len(entries) / n) for w, n in counts.items()}
        return cls(tuple(entries), asked, told, idf)

    def best(self, question: str) -> tuple[FaqEntry | None, float]:
        words = _tokens(question)
        best, best_score = None, 0.0
        for entry, asked, told in zip(self.entries, self.asked, self.told):
            if not words & asked:
                continue  # words that only occur in an answer are too weak a sign it is the right one
            score = sum(self.idf.get(w, 0.0) * (3.0 * (w in asked) + 1.0 * (w in told)) for w in words)
            if score > best_score:
                best, best_score = entry, score
        return best, best_score


_INDEX: _Index | None = None
MIN_SCORE = 4.0  # below this, no entry is a credible match: about one distinctive question word


def answer_offline(question: str, entries: Sequence[FaqEntry] | None = None) -> HelpAnswer:
    """The FAQ entry that best matches `question`, word for word; a fixed reply if none does."""
    global _INDEX
    if entries is not None:
        index = _Index.build(entries)
    else:
        _INDEX = _INDEX or _Index.build(load_faq())
        index = _INDEX
    entry, score = index.best(question)
    if entry is None or score < MIN_SCORE:
        return HelpAnswer(NO_MATCH, "offline")
    return HelpAnswer(entry.answer, "offline", matched=entry.question)


# --- Questions and answers on the way in and out ----------------------------------------


def check_question(text: str) -> str:
    """The question as Help sends it: trimmed, capped, and valid text. HelpError if there is nothing to ask."""
    question = " ".join(str(text).split())[:MAX_QUESTION_CHARS]
    if not question:
        raise HelpError("EMPTY_QUESTION", "ask a question")
    try:
        question.encode("utf-8")
    except UnicodeEncodeError as e:
        raise HelpError("BAD_TEXT", "the question is not valid text") from e
    return question


def clean_answer(text: str) -> str | None:
    """A model-written answer as plain text for the Help panel, or None if it must not be shown."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return None
    text = re.sub(r"\*\*|__|`", "", text.replace("\r", ""))  # Markdown emphasis: the panel shows plain text
    text = re.sub(r"^[ \t]*#{1,6}[ \t]*", "", text, flags=re.M)  # Markdown headings
    lines = [" ".join(line.split()) for line in text.split("\n")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    if len(text) > MAX_ANSWER_CHARS:
        cut = text[:MAX_ANSWER_CHARS]
        end = max(cut.rfind(". "), cut.rfind(".\n"))
        text = (cut[: end + 1] if end > MAX_ANSWER_CHARS // 2 else cut.rstrip()) + " …"
    if not text or scan_text(text):
        return None
    return text


# --- Tencent ADP --------------------------------------------------------------------


@dataclass
class AdpHelp:
    """A published ADP knowledge-Q&A app, asked through the HTTP SSE chat API (v2).

    The AppKey travels in the JSON body; there is no signature. ADP answers HTTP
    200 even when a request fails, with an ``error`` event in the stream, so
    errors are read from the events, not the status code.
    """

    app_key: str
    url: str = ADP_CHAT_URL
    visitor_id: str = "glassvault-demo"
    timeout: float = 20.0  # for the whole answer
    http: Any = None  # an httpx.Client; tests pass one with a mock transport

    @property
    def name(self) -> str:
        return "Tencent ADP · knowledge Q&A"

    @property
    def host(self) -> str:
        return urlsplit(self.url).hostname or self.url

    def ask(self, question: str, *, conversation_id: str) -> HelpAnswer:
        """ADP's answer to `question`, as plain text. HelpError if ADP gives no usable answer."""
        body = {
            "RequestId": uuid.uuid4().hex,
            "ConversationId": conversation_id,  # 32-64 of [A-Za-z0-9_-]; one per demo session
            "AppKey": self.app_key,
            "VisitorId": self.visitor_id,
            "Contents": [{"Type": "text", "Text": check_question(question)}],
            "Incremental": True,
            "Stream": "enable",
        }
        payload = json.dumps(body, ensure_ascii=False).encode()
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        give_up = time.monotonic() + self.timeout
        try:
            if self.http is not None:
                with self.http.stream("POST", self.url, content=payload, headers=headers,
                                      timeout=self.timeout) as response:
                    reply = _read_stream(response, give_up)
            else:
                with httpx.Client(timeout=self.timeout) as client, \
                        client.stream("POST", self.url, content=payload, headers=headers) as response:
                    reply = _read_stream(response, give_up)
        except httpx.HTTPError as e:
            raise HelpError("UNREACHABLE", f"{type(e).__name__}: {e}"[:200]) from e
        text = clean_answer(reply.text)
        if text is None:
            return HelpAnswer(WITHHELD, "adp", references=reply.references,
                              note="ADP's answer was withheld: it was not plain, safe text")
        return HelpAnswer(text, "adp", references=reply.references)


@dataclass
class _Reply:
    text: str
    references: tuple[str, ...] = ()
    reply_method: int | None = None


@dataclass
class _Stream:
    kinds: dict[str, str] = field(default_factory=dict)  # MessageId -> Message.Type ("reply", "thought", ...)
    deltas: dict[str, list[str]] = field(default_factory=dict)  # reply MessageId -> text so far
    done: dict[str, str] = field(default_factory=dict)  # reply MessageId -> final text
    order: list[str] = field(default_factory=list)  # reply MessageIds, first seen first
    references: list[str] = field(default_factory=list)
    reply_method: int | None = None

    def reply(self) -> _Reply:
        parts = [self.done.get(m) if m in self.done else "".join(self.deltas.get(m, [])) for m in self.order]
        return _Reply("\n\n".join(p.strip() for p in parts if p and p.strip()), tuple(self.references),
                      self.reply_method)

    def take(self, event: Mapping[str, Any]) -> None:
        kind = event.get("Type")
        if kind == "error":
            error = event.get("Error") if isinstance(event.get("Error"), dict) else {}
            # The code ends up in a note on the page, so only a plain code gets through, never ADP's text.
            code = str(error.get("Code", ""))
            raise HelpError(code if _ERROR_CODE.fullmatch(code) else "error", str(error.get("Message", ""))[:200])
        if kind == "message.added":
            message = event.get("Message") if isinstance(event.get("Message"), dict) else {}
            message_id = str(message.get("MessageId", ""))
            self.kinds[message_id] = str(message.get("Type", ""))
            if self.kinds[message_id] == "reply" and message_id not in self.order:
                self.order.append(message_id)
        elif kind in ("text.delta", "text.replace"):
            message_id = str(event.get("MessageId", ""))
            if self.kinds.get(message_id) == "reply":  # never thought, tool or other messages
                text = str(event.get("Text", ""))
                if kind == "text.delta":
                    self.deltas.setdefault(message_id, []).append(text)
                else:
                    self.deltas[message_id] = [text]
        elif kind == "message.done":
            message = event.get("Message") if isinstance(event.get("Message"), dict) else {}
            message_id = str(message.get("MessageId") or event.get("MessageId") or "")
            if message.get("Type") == "reply":
                contents = message.get("Contents") if isinstance(message.get("Contents"), list) else []
                self.done[message_id] = "".join(str(c.get("Text", "")) for c in contents
                                                if isinstance(c, dict) and c.get("Type") == "text")
                if message_id not in self.order:
                    self.order.append(message_id)
        elif kind == "reference.added":
            reference = event.get("Reference") if isinstance(event.get("Reference"), dict) else {}
            name = " ".join(str(reference.get("Name", "")).split())[:80]
            if name and name not in self.references and len(self.references) < MAX_REFERENCES:
                self.references.append(name)
        elif kind == "response.completed":
            response = event.get("Response") if isinstance(event.get("Response"), dict) else {}
            extra = response.get("ExtraInfo") if isinstance(response.get("ExtraInfo"), dict) else {}
            if isinstance(extra.get("ReplyMethod"), int):
                self.reply_method = extra["ReplyMethod"]


_ERROR_CODE = re.compile(r"[A-Za-z0-9_.-]{1,40}")


def _read_stream(response: Any, give_up: float) -> _Reply:
    """Read ADP's SSE events up to ``[DONE]`` or the end of the stream; an error event raises HelpError.

    The stream is read in chunks, not lines, and the deadline and size are checked on
    every chunk: a server that trickles bytes without a newline, or sends one endless
    line, must not hold a worker thread past the timeout.
    """
    if response.status_code != 200:
        raise HelpError(f"HTTP_{response.status_code}", "ADP refused the request")  # the body is not read
    stream = _Stream()
    received, pending = 0, ""

    def take_line(line: str) -> bool:
        """Apply one SSE line; True at ``[DONE]``."""
        if time.monotonic() > give_up:
            raise HelpError("TIMEOUT", "no complete answer in time")
        if not line.startswith("data:"):
            return False  # "event:" names repeat the Type inside the data
        data = line[5:].strip()
        if data == "[DONE]":
            return True
        try:
            event = json.loads(data)
        except (ValueError, RecursionError):  # deeply nested JSON raises RecursionError
            return False
        if isinstance(event, dict):
            stream.take(event)
        return False

    finished = False
    for chunk in response.iter_text():
        if time.monotonic() > give_up:
            raise HelpError("TIMEOUT", "no complete answer in time")
        received += len(chunk)
        if received > MAX_STREAM_CHARS:
            raise HelpError("TOO_LONG", "ADP's answer stream is too long")
        *lines, pending = (pending + chunk).split("\n")
        if any(take_line(line.rstrip("\r")) for line in lines):
            finished = True
            break
    if not finished and pending:
        take_line(pending.rstrip("\r"))
    reply = stream.reply()
    if not reply.text:
        raise HelpError("EMPTY_ANSWER", "ADP sent no reply text")
    return reply


# --- Answering ------------------------------------------------------------------------


def answer(question: str, adp: AdpHelp | None, *, conversation_id: str) -> HelpAnswer:
    """Help's answer: from ADP when it is set up, and from the built-in FAQ otherwise or if ADP fails."""
    question = check_question(question)
    if adp is None:
        return answer_offline(question)
    try:
        return adp.ask(question, conversation_id=conversation_id)
    except HelpError as e:
        code = e.code
    except Exception:  # whatever ADP's stream did, the customer still gets an answer
        code = "ERROR"
    return replace(answer_offline(question), note=f"Tencent ADP did not answer ({code}), so the built-in FAQ did")


def new_conversation_id() -> str:
    """A fresh ADP ConversationId: 32 hex characters, within ADP's 32-64 of [A-Za-z0-9_-]."""
    return uuid.uuid4().hex


# --- Configuration --------------------------------------------------------------------


def faq_from_env(environ: Mapping[str, str] | None = None) -> AdpHelp | None:
    """The ADP Help app the settings point to, from GV_* variables or .env; None if it is not set up.

    Needs GV_ADP_APP_KEY (the published app's AppKey). Optional: GV_ADP_CHAT_URL
    (an https URL; default: the international site).
    """
    if environ is None:
        from .llm import _env_with_dotenv

        environ = _env_with_dotenv()

    def get(key: str) -> str:
        return (environ.get(key) or "").strip()

    app_key = get("GV_ADP_APP_KEY")
    if not app_key:
        return None
    url = get("GV_ADP_CHAT_URL")
    if not (url.startswith("https://") and urlsplit(url).hostname):
        url = ADP_CHAT_URL
    return AdpHelp(app_key, url=url)
