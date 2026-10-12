"""Set up Help on Tencent ADP: a knowledge-Q&A app that answers from src/glassvault/faq.md.

    uv run python scripts/adp_setup.py --check   # read-only: can these keys reach ADP, and which models are there?
    uv run python scripts/adp_setup.py           # create or update the app, upload the FAQ, release it,
                                                 # and write its AppKey to .env as GV_ADP_APP_KEY

It signs in with the account's Tencent Cloud API keys (GV_TENCENT_SECRET_ID and
GV_TENCENT_SECRET_KEY, from the environment or .env), the same keys as the TRTC
voice, and uses ADP's API 2023-11-30 (service ``lke``) on the international
site, lke.intl.tencentcloudapi.com, whose only region is ap-jakarta. ADP must
first be activated once in the console, and its Free plan claimed:
https://adp.tencentcloud.com.

Running it again is safe. It finds the app by name instead of making another,
writes the same settings again, and names the FAQ document after its content
("Glass Vault FAQ 1a2b3c4d.md", from the file's SHA-256). A copy with that name
that ADP has learned stays and nothing is uploaded; one that failed is deleted
and uploaded again; one still being learned is waited for. Older copies are
deleted only once the new one is learned, so the app is never left without its
FAQ.

Why not SaveDoc's own duplicate check (DuplicateFileHandles): it matches by
content, so it can hand back an earlier copy that failed (and every run after
fails the same way) or one uploaded in the console, which is live only in the
debug scope, so the released app never sees it. ListDoc does not report a
document's scope, so the name is what marks a copy this script uploaded.

Why one document and not one Q&A per FAQ entry: on the international API a
Q&A made with CreateQA is live only in the debug scope, and the API can neither
read nor change that scope, so the released app, the one Help talks to, would
never see it. SaveDoc takes EnableScope=4: live in debug and release.

Nothing secret is printed: not the API keys, not the temporary COS keys, and not
the AppKey, which goes straight into .env.
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import hashlib
import hmac
import json
import os
import re
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from glassvault.faq import FAQ_PATH
from glassvault.voice import tc3_headers

API_HOST = "lke.intl.tencentcloudapi.com"  # international site; lke.tencentcloudapi.com on the China site
API_VERSION = "2023-11-30"
API_REGION = "ap-jakarta"  # the only region the international ADP API accepts
CONSOLE_URL = "https://adp.tencentcloud.com"
ACTIVATE = f"Activate ADP and claim the Free plan at {CONSOLE_URL}, then run this again."

REPO = Path(__file__).resolve().parent.parent
ENV_PATH = REPO / ".env"

APP_NAME = "Glass Vault Help"
APP_DESC = "Answers questions about the Glass Vault demo bank from its FAQ. It sees no accounts, makes no payments."
APP_AVATAR = "https://cdn.xiaowei.qq.com/static/adp/app-default-avatar.png"  # ADP's own default; the API wants one
DOC_PREFIX = "Glass Vault FAQ"  # every copy's name starts with this; answers cite the name, so it reads as a title

# ADP's "role description": the bot's standing instructions. The FAQ itself says what the bank is;
# this keeps the bot to the FAQ, short, and away from anything that looks like account access.
ROLE = (
    "You are Help, the FAQ assistant of Glass Vault, a hackathon demo of a conversational bank. "
    "Glass Vault is not DBS and not a real bank, and no real money moves.\n"
    "Answer only from the knowledge base, in two or three short sentences of plain text: "
    "no Markdown, lists or links.\n"
    "If the knowledge base does not cover the question, say that the FAQ does not cover it and suggest asking "
    "the main assistant in the chat.\n"
    "You cannot see any account, balance or transaction, and you cannot make, change, approve or confirm a payment. "
    "Never say or suggest that you did.\n"
    "Never ask for a password, PIN, OTP, token code or card number. "
    "If someone offers one, tell them not to share it.\n"
    "Reply in the customer's language."
)
GREETING = ("Hi, I'm Glass Vault Help. Ask me how the demo bank works: limits, approvals, payees "
            "or what the assistant can do.")
# What ADP says when nothing in the knowledge base matches: with general knowledge off, it never improvises.
BARE_ANSWER = ("The FAQ doesn't cover that. For anything about your own accounts or payments, "
               "please ask the main assistant in the chat.")

# Document status (DescribeDoc / ListDoc). 10 and 12 both mean "imported"; 11 is on its way to 12.
DOC_STATUS = {
    5: "deleting", 6: "deleted", 7: "under review", 8: "review failed", 9: "review passed",
    10: "learned, pending release", 11: "releasing", 12: "learned and released", 13: "learning",
    14: "learning failed", 15: "updating", 16: "update failed", 17: "parsing", 18: "parsing failed",
    19: "import failed", 20: "expired", 21: "over the plan's capacity", 22: "restored",
}
DOC_READY = frozenset({10, 11, 12})
DOC_FAILED = frozenset({5, 6, 8, 14, 16, 18, 19, 20, 21})
DOC_GONE = frozenset({5, 6})  # deleting or deleted: no longer a copy to keep, wait for or delete

# Release status (DescribeRelease).
RELEASE_STATUS = {
    1: "pending", 2: "releasing", 3: "released", 4: "release failed", 5: "releasing (under review)",
    6: "releasing (review passed)", 7: "failed review", 9: "paused",
}
RELEASE_DONE = 3
RELEASE_FAILED = frozenset({4, 7, 9})


class AdpApiError(Exception):
    """A call that ADP (or COS) refused or never answered: which call, the error code and the message."""

    def __init__(self, action: str, code: str, message: str, request_id: str = ""):
        super().__init__(f"{action} failed: {code}: {message}")
        self.action = action
        self.code = code
        self.message = message
        self.request_id = request_id


class SetupError(Exception):
    """The setup cannot go on, for a reason that is not one API error: a timeout, a failed document, a bad choice."""


_NOT_ACTIVATED = re.compile(r"450006|not logged in|not registered|not (?:been )?activated|未开通|未注册", re.I)


def needs_activation(error: AdpApiError) -> bool:
    """True if the error says ADP is not activated for this account (ListApp says 450006 until it is)."""
    return bool(_NOT_ACTIVATED.search(f"{error.code} {error.message}"))


def _redact(text: str, secrets: Sequence[str]) -> str:
    """`text` with every secret replaced, in case a server ever echoes one back."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


# --- The ADP API (Tencent Cloud API 3.0) -----------------------------------------------


@dataclass
class AdpAdmin:
    """ADP's management API, signed with the account's API keys (TC3-HMAC-SHA256).

    `http` is the httpx.Client every request goes through, ADP's and COS's
    alike; tests pass one with a mock transport. `clock` gives the signing time.
    """

    secret_id: str
    secret_key: str
    http: httpx.Client
    host: str = API_HOST
    region: str = API_REGION
    timeout: float = 30.0
    clock: Callable[[], float] = time.time
    secrets: list[str] = field(default_factory=list)  # more values never to print, e.g. COS's temporary keys

    def call(self, action: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """The `Response` object of one API call. AdpApiError if the call fails in any way."""
        payload = json.dumps(params or {}, ensure_ascii=False, separators=(",", ":")).encode()
        headers = tc3_headers(secret_id=self.secret_id, secret_key=self.secret_key, host=self.host,
                              action=action, version=API_VERSION, region=self.region, payload=payload,
                              timestamp=int(self.clock()))
        try:
            response = self.http.post(f"https://{self.host}/", content=payload, headers=headers,
                                      timeout=self.timeout)
        except httpx.HTTPError as e:
            raise AdpApiError(action, "UNREACHABLE", f"no answer from {self.host} ({type(e).__name__})") from e
        try:
            data = response.json()
        except ValueError:
            raise AdpApiError(action, f"HTTP_{response.status_code}", "the reply is not JSON") from None
        body = data.get("Response") if isinstance(data, dict) else None
        if not isinstance(body, dict):
            raise AdpApiError(action, f"HTTP_{response.status_code}", "the reply has no Response object")
        error = body.get("Error")
        if error:
            error = error if isinstance(error, dict) else {}
            raise AdpApiError(action, str(error.get("Code") or "Error"),
                              self.redact(str(error.get("Message") or ""))[:300], str(body.get("RequestId") or ""))
        return body

    def redact(self, text: str) -> str:
        return _redact(text, [self.secret_id, self.secret_key, *self.secrets])


# --- COS: the upload ADP's knowledge base reads from ----------------------------------------


def _cos_encode(text: str) -> str:
    """COS's URL encoding for signing: UTF-8, everything but letters, digits and -_.~ percent-encoded."""
    return quote(text, safe="-_.~")


def _cos_pairs(items: Mapping[str, str]) -> list[tuple[str, str]]:
    """Signing pairs: key encoded then lower-cased, value encoded, sorted by key."""
    return sorted((_cos_encode(k).lower(), _cos_encode(str(v))) for k, v in items.items())


def cos_http_string(method: str, path: str, params: Mapping[str, str], headers: Mapping[str, str]) -> str:
    """COS's HttpString: method, path (not encoded), the signed query parameters and headers, one per line."""
    query = "&".join(f"{k}={v}" for k, v in _cos_pairs(params))
    signed = "&".join(f"{k}={v}" for k, v in _cos_pairs(headers))
    return f"{method.lower()}\n{path}\n{query}\n{signed}\n"


def cos_authorization(*, secret_id: str, secret_key: str, method: str, path: str, headers: Mapping[str, str],
                      params: Mapping[str, str] | None = None, start: int, end: int) -> str:
    """The Authorization header of a COS request: COS's own signature (q-sign-algorithm=sha1).

    Every header in `headers` is signed, so it must be sent exactly as given.
    The signature is valid from `start` to `end` (Unix seconds).
    """
    params = params or {}
    key_time = f"{start};{end}"
    sign_key = hmac.new(secret_key.encode(), key_time.encode(), hashlib.sha1).hexdigest()
    http_string = cos_http_string(method, path, params, headers)
    string_to_sign = f"sha1\n{key_time}\n{hashlib.sha1(http_string.encode()).hexdigest()}\n"
    signature = hmac.new(sign_key.encode(), string_to_sign.encode(), hashlib.sha1).hexdigest()
    return "&".join([
        "q-sign-algorithm=sha1",
        f"q-ak={secret_id}",
        f"q-sign-time={key_time}",
        f"q-key-time={key_time}",
        "q-header-list=" + ";".join(k for k, _ in _cos_pairs(headers)),
        "q-url-param-list=" + ";".join(k for k, _ in _cos_pairs(params)),
        f"q-signature={signature}",
    ])


@functools.cache
def _crc64_table() -> tuple[int, ...]:
    poly = 0xC96C5795D7870F42  # ECMA-182, reflected
    table = []
    for byte in range(256):
        crc = byte
        for _ in range(8):
            crc = (crc >> 1) ^ poly if crc & 1 else crc >> 1
        table.append(crc)
    return tuple(table)


def crc64(data: bytes) -> int:
    """CRC-64/XZ (ECMA-182, reflected): what COS reports in x-cos-hash-crc64ecma."""
    table = _crc64_table()
    crc = 0xFFFFFFFFFFFFFFFF
    for byte in data:
        crc = table[(crc ^ byte) & 0xFF] ^ (crc >> 8)
    return crc ^ 0xFFFFFFFFFFFFFFFF


_COS_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")


def cos_put(admin: AdpAdmin, storage: Mapping[str, Any], body: bytes, content_type: str) -> tuple[str, str]:
    """Upload `body` to the COS object that DescribeStorageCredential named. Returns (ETag, CRC64).

    The bucket and region come from ADP's reply, so they are checked to be plain
    COS names before the temporary keys are sent anywhere.
    """
    action = "COS upload"
    credentials = storage.get("Credentials") if isinstance(storage.get("Credentials"), dict) else {}
    tmp_id, tmp_key, token = (str(credentials.get(k) or "") for k in ("TmpSecretId", "TmpSecretKey", "Token"))
    bucket, region, path = (str(storage.get(k) or "") for k in ("Bucket", "Region", "UploadPath"))
    admin.secrets += [s for s in (tmp_id, tmp_key, token) if s]
    if not (tmp_id and tmp_key and token):
        raise AdpApiError("DescribeStorageCredential", "BAD_RESPONSE", "no temporary upload keys in the reply")
    if not (_COS_NAME.fullmatch(bucket) and _COS_NAME.fullmatch(region)):
        raise AdpApiError("DescribeStorageCredential", "BAD_RESPONSE", "the bucket or region is not a COS name")
    if not re.fullmatch(r"/[A-Za-z0-9._~/-]+", path) or "/../" in f"{path}/":
        raise AdpApiError("DescribeStorageCredential", "BAD_RESPONSE", "the upload path is not a plain COS path")
    host = f"{bucket}.cos.{region}.myqcloud.com"
    signed = {"Content-Type": content_type, "Host": host, "x-cos-security-token": token}
    start = int(admin.clock()) - 60  # a minute of leeway for clock skew, as COS's own SDK allows
    headers = {**signed, "Authorization": cos_authorization(secret_id=tmp_id, secret_key=tmp_key, method="PUT",
                                                            path=path, headers=signed, start=start, end=start + 900)}
    try:
        response = admin.http.put(f"https://{host}{quote(path)}", content=body, headers=headers,
                                  timeout=admin.timeout)
    except httpx.HTTPError as e:
        raise AdpApiError(action, "UNREACHABLE", f"no answer from {host} ({type(e).__name__})") from e
    if response.status_code != 200:
        code = re.search(r"<Code>(.*?)</Code>", response.text)
        message = re.search(r"<Message>(.*?)</Message>", response.text)
        raise AdpApiError(action, code.group(1) if code else f"HTTP_{response.status_code}",
                          admin.redact(message.group(1) if message else response.text)[:300])
    etag = response.headers.get("ETag", "")
    local = str(crc64(body))
    remote = response.headers.get("x-cos-hash-crc64ecma", "")
    if remote and remote != local:
        raise AdpApiError(action, "CRC_MISMATCH", "the uploaded file does not match faq.md")
    if not etag:
        raise AdpApiError(action, "BAD_RESPONSE", "COS sent no ETag")
    return etag, remote or local


# --- The steps ----------------------------------------------------------------------


@dataclass
class Waiting:
    """How the setup waits for ADP: poll every `interval` seconds, give up after `timeout`."""

    interval: float = 5.0
    timeout: float = 600.0
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic


def list_apps(admin: AdpAdmin) -> list[dict[str, Any]]:
    """The knowledge-Q&A apps whose name or modifier matches APP_NAME (ListApp's Keyword), page by page.

    ListApp's doc does not say whether PageNumber counts from 0 or from 1. This
    reads from page 1 on, and if that gives fewer apps than Total (an empty first
    page, say), reads page 0 as well. An app on two pages is kept once.
    """
    query = {"AppType": "knowledge_qa", "Keyword": APP_NAME, "PageSize": 50}
    apps: dict[str, dict[str, Any]] = {}

    def read(page: int) -> tuple[int, int]:
        """Adds one page's apps; returns (Total, how many were new)."""
        data = admin.call("ListApp", {**query, "PageNumber": page})
        before = len(apps)
        for app in data.get("List") or []:
            if isinstance(app, dict) and app.get("AppBizId"):
                apps.setdefault(str(app["AppBizId"]), app)
        return _int(data.get("Total")), len(apps) - before

    total = 0
    for page in range(1, 21):
        total, new = read(page)
        if not new or len(apps) >= total:
            break
    if len(apps) < total:
        read(0)
    return list(apps.values())


def find_app(apps: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """The app named APP_NAME, if there is one (the first, if there are several)."""
    return next((a for a in apps if a.get("Name") == APP_NAME and a.get("AppBizId")), None)


def list_models(admin: AdpAdmin) -> list[dict[str, Any]]:
    """The answer-writing models a standard-mode knowledge-Q&A app may use (ListModel)."""
    data = admin.call("ListModel", {"AppType": "knowledge_qa", "Pattern": "standard"})
    return [m for m in data.get("List") or [] if isinstance(m, dict) and m.get("ModelName")
            and str(m.get("ModelCategory") or "generate").lower() == "generate"]


def _model_rank(model: Mapping[str, Any]) -> tuple[bool, int]:
    """Sort key: models with quota left first; then DeepSeek V4 Flash (cheap, fast, no reasoning
    trace), a DeepSeek V3 chat model, any other non-reasoning DeepSeek, ADP's default, the rest."""
    text = re.sub(r"[^a-z0-9]", "", f"{model.get('ModelName', '')} {model.get('AliasName', '')}".lower())
    reasoning = re.search(r"r1|reason|think", text)
    if "deepseek" in text and "v4flash" in text:
        kind = 0
    elif "deepseek" in text and not reasoning and ("v3" in text or "chat" in text):
        kind = 1
    elif "deepseek" in text and not reasoning:
        kind = 2
    elif model.get("IsDefault"):
        kind = 3
    else:
        kind = 4
    return model.get("ResourceStatus") == 2, kind


def role_fits(model: Mapping[str, Any]) -> bool:
    """False if the model's RoleLenLimit is set and shorter than ROLE; True if it is not set."""
    limit = _int(model.get("RoleLenLimit"))
    return limit <= 0 or limit >= len(ROLE)


def choose_model(models: Sequence[Mapping[str, Any]], wanted: str | None = None, *,
                 current: Callable[[], Mapping[str, Any] | None] | None = None,
                 say: Callable[[str], None] = lambda text: None) -> Mapping[str, Any]:
    """The model to answer with. It runs before the app is created, so a bad choice creates nothing.

    `wanted` (a ModelName or alias, from --model) is refused if ListModel gives it
    a RoleLenLimit shorter than ROLE, and used as given, with a warning, if
    ListModel does not list it: on the international site that list may be
    incomplete. Without `wanted`: the best by `_model_rank` of the models ROLE
    fits; failing that, `current()`, the existing app's model, if it has one.
    """
    if wanted:
        for model in models:
            if wanted in (model.get("ModelName"), model.get("AliasName")):
                if not role_fits(model):
                    raise SetupError(f"--model {wanted!r} takes a role description of at most "
                                     f"{_int(model.get('RoleLenLimit'))} characters; Help's is {len(ROLE)}")
                return model
        names = ", ".join(str(m.get("ModelName")) for m in models) or "none"
        say(f"Warning: ListModel does not list --model {wanted!r} (it offered: {names}); using it as given, "
            "since ListModel on the international site may not list every model")
        return {"ModelName": wanted}
    usable = [m for m in models if role_fits(m)]
    if usable:
        return min(usable, key=_model_rank)  # min keeps ADP's order among equals
    kept = current() if current else None
    if kept:
        say(f"ListModel offered no model that takes Help's role description, so the app keeps its current one, "
            f"{_model_label(kept)}")
        return kept
    offered = "; ".join(_model_line(m) for m in models) or "none"
    raise SetupError(f"ListModel offered no model that takes Help's {len(ROLE)}-character role description "
                     f"(offered: {offered}). Pass --model with a ModelName from the ADP console; "
                     "--check lists what ListModel offers")


def current_model(admin: AdpAdmin, app_id: str) -> Mapping[str, Any] | None:
    """The model the app answers with now (DescribeApp), shaped like a ListModel entry; None if it has none."""
    app = admin.call("DescribeApp", {"AppBizId": app_id, "AppType": "knowledge_qa"})
    model = _dict(_dict(_dict(app.get("AppConfig")).get("KnowledgeQa")).get("Model"))
    name = str(model.get("Name") or "")
    return {"ModelName": name, "AliasName": str(model.get("AliasName") or name)} if name else None


def _model_label(model: Mapping[str, Any]) -> str:
    name, alias = str(model.get("ModelName")), str(model.get("AliasName") or "")
    return f"{name} ({alias})" if alias and alias != name else name


def _model_line(model: Mapping[str, Any]) -> str:
    """A model as --check lists it: its name, its quota, and the longest role description it takes."""
    state = "quota used up" if model.get("ResourceStatus") == 2 else "available"
    limit = _int(model.get("RoleLenLimit"))
    line = f"{_model_label(model)}: {state}, RoleLenLimit {limit if limit > 0 else 'not given'}"
    return line if role_fits(model) else f"{line}, too short for Help's {len(ROLE)}-character role"


def create_app(admin: AdpAdmin) -> str:
    """A new standard-mode knowledge-Q&A app named APP_NAME; returns its AppBizId."""
    data = admin.call("CreateApp", {
        "AppType": "knowledge_qa",
        "Pattern": "standard",  # explicit: the doc's own example creates a "ClawAgent" app
        "AgentType": "dialogue",
        "BaseConfig": {"Name": APP_NAME, "Avatar": APP_AVATAR, "Desc": APP_DESC},
    })
    app_id = str(data.get("AppBizId") or "")
    if not app_id:
        raise AdpApiError("CreateApp", "BAD_RESPONSE", "no AppBizId in the reply")
    return app_id


def _without_nulls(value: Any) -> Any:
    """`value` with every null dropped: DescribeApp reports unset fields as null; ModifyApp leaves them out."""
    if isinstance(value, dict):
        return {k: _without_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_without_nulls(v) for v in value if v is not None]
    return value


def configure_app(admin: AdpAdmin, app_id: str, model: Mapping[str, Any]) -> int | None:
    """Write Help's settings into the app; returns its AppStatus (1 never released, 2 running).

    ModifyApp replaces the whole configuration, so this reads it with DescribeApp,
    changes only Help's settings, and writes the rest back as it was. Whether
    ROLE fits the model was settled by choose_model, before the app was created.
    """
    app = admin.call("DescribeApp", {"AppBizId": app_id, "AppType": "knowledge_qa"})
    base = {**_without_nulls(app.get("BaseConfig") or {}), "Name": APP_NAME, "Desc": APP_DESC}
    base["Avatar"] = base.get("Avatar") or APP_AVATAR
    config = _without_nulls(app.get("AppConfig") or {})
    qa = dict(config.get("KnowledgeQa") or {})
    qa["Pattern"] = "standard"
    qa["RoleDescription"] = ROLE
    qa["Greeting"] = GREETING
    qa["Model"] = {**(qa.get("Model") or {}), "Name": model["ModelName"],
                   "AliasName": model.get("AliasName") or model["ModelName"]}
    # Off-topic questions get the fixed reply instead of the model's general knowledge.
    qa["Output"] = {**(qa.get("Output") or {}), "UseGeneralKnowledge": False, "BareAnswer": BARE_ANSWER}
    search = []
    for source in qa.get("Search") or []:
        source = dict(source)
        if source.get("Type") == "doc":
            source["IsEnabled"] = True
        elif source.get("Type") == "search":  # web search would bring in text from outside the FAQ
            source["IsEnabled"] = False
            source["UseSearchEngine"] = False
        search.append(source)
    if not any(s.get("Type") == "doc" for s in search):
        search.append({"Type": "doc", "IsEnabled": True})
    qa["Search"] = search
    config["KnowledgeQa"] = qa
    admin.call("ModifyApp", {"AppBizId": app_id, "AppType": "knowledge_qa", "BaseConfig": base,
                             "AppConfig": config})
    status = app.get("AppStatus")
    return status if isinstance(status, int) else None


def doc_name(faq: bytes) -> str:
    """The FAQ document's name: DOC_PREFIX and the first 8 hex digits of the file's SHA-256.

    A copy with this name is one this script uploaded, with EnableScope=4, so
    a learned one can stand in for a new upload.
    """
    return f"{DOC_PREFIX} {hashlib.sha256(faq).hexdigest()[:8]}.md"


def upload_faq(admin: AdpAdmin, app_id: str, faq: bytes, waiting: Waiting, say: Callable[[str], None]) -> None:
    """Make `faq` the app's one FAQ document, live for the released app, and wait until it is learned.

    A copy already named doc_name(faq) that is learned stays, and nothing is
    uploaded; one still being learned is waited for; one that failed is deleted
    and the FAQ uploaded again. Every other copy is deleted once this one is learned.
    """
    name = doc_name(faq)
    copies = _faq_copies(admin, app_id)
    same = [d for d in copies if d.get("FileName") == name]
    learned = next((d for d in same if d.get("Status") in DOC_READY), None)
    pending = next((d for d in same if d.get("Status") not in DOC_READY | DOC_FAILED), None)
    deleted: list[str] = []
    if learned:
        doc_id = str(learned["DocBizId"])
        say(f'FAQ: "{name}" is already learned (DocBizId {doc_id}), so it is not uploaded again')
    elif pending:
        doc_id = str(pending["DocBizId"])
        say(f'FAQ: "{name}" is already uploaded (DocBizId {doc_id}); waiting for ADP to learn it')
        _wait_for_doc(admin, app_id, doc_id, name, waiting, say)
    else:
        for doc in same:  # every one has failed
            label = DOC_STATUS.get(doc.get("Status")) or str(doc.get("Status"))
            deleted.append(str(doc["DocBizId"]))
            say(f'FAQ: an earlier copy of "{name}" failed (DocBizId {deleted[-1]}, {label}); '
                "deleting it and uploading again")
        if deleted:
            _delete_docs(admin, app_id, deleted, "failed")
        doc_id = _save_faq(admin, app_id, name, faq)
        say(f'FAQ: uploaded "{name}" ({len(faq)} bytes), DocBizId {doc_id}')
        _wait_for_doc(admin, app_id, doc_id, name, waiting, say)
    older = [str(d["DocBizId"]) for d in copies if str(d["DocBizId"]) not in (doc_id, *deleted)]
    if older:
        _delete_docs(admin, app_id, older, "older")
        say(f"FAQ: deleted {len(older)} older cop{'y' if len(older) == 1 else 'ies'}")


def _faq_copies(admin: AdpAdmin, app_id: str) -> list[dict[str, Any]]:
    """The app's FAQ documents: every one whose name starts with DOC_PREFIX and that is not deleted or being deleted."""
    listed = admin.call("ListDoc", {"BotBizId": app_id, "PageNumber": 1, "PageSize": 100,
                                    "Query": DOC_PREFIX, "QueryType": "filename"})
    return [d for d in listed.get("List") or [] if isinstance(d, dict) and d.get("DocBizId")
            and str(d.get("FileName") or "").startswith(DOC_PREFIX)
            and not d.get("IsDeleted") and d.get("Status") not in DOC_GONE]


def _save_faq(admin: AdpAdmin, app_id: str, name: str, faq: bytes) -> str:
    """Upload `faq` to COS and add it to the app's knowledge base as `name`; returns its DocBizId."""
    storage = admin.call("DescribeStorageCredential", {"BotBizId": app_id, "FileType": "md", "IsPublic": False,
                                                       "TypeKey": "offline"})
    etag, cos_hash = cos_put(admin, storage, faq, "text/markdown")
    # No DuplicateFileHandles: ADP's content check could hand back a failed copy or a debug-only one (see the top).
    saved = admin.call("SaveDoc", {
        "BotBizId": app_id,
        "FileName": name,
        "FileType": "md",
        "CosUrl": storage["UploadPath"],
        "ETag": etag,  # as COS sent it, quotes included, as in the doc's example
        "CosHash": cos_hash,
        "Size": str(len(faq)),
        "AttrRange": 1,  # no labels: the whole app may use it
        "Source": 0,  # a file, not a web page
        "Opt": 2,  # a document; the default, 1, is a batch Q&A import from ADP's Excel template
        "EnableScope": 4,  # live in debug and in release; the default, 2, is debug only
    })
    doc_id = str(saved.get("DocBizId") or "")
    if not doc_id:
        raise AdpApiError("SaveDoc", "BAD_RESPONSE", str(saved.get("ErrorMsg") or "no DocBizId in the reply")[:300])
    return doc_id


def _delete_docs(admin: AdpAdmin, app_id: str, doc_ids: Sequence[str], which: str) -> None:
    """DeleteDoc the `which` ("failed", "older") FAQ copies; if ADP refuses, an error that names them and says
    how to recover."""
    try:
        admin.call("DeleteDoc", {"BotBizId": app_id, "DocBizIds": list(doc_ids)})
    except AdpApiError as e:
        one = len(doc_ids) == 1
        raise SetupError(f"could not delete the {which} FAQ cop{'y' if one else 'ies'} "
                         f"(DocBizId {', '.join(doc_ids)}): {e}. Run this again later, or delete "
                         f"{'it' if one else 'them'} under Knowledge in {CONSOLE_URL} first") from e


def _wait_for_doc(admin: AdpAdmin, app_id: str, doc_id: str, name: str, waiting: Waiting,
                  say: Callable[[str], None]) -> None:
    deadline = waiting.clock() + waiting.timeout
    last = None
    while True:
        doc = admin.call("DescribeDoc", {"BotBizId": app_id, "DocBizId": doc_id})
        status = doc.get("Status")
        label = DOC_STATUS.get(status) or str(doc.get("StatusDesc") or status)
        if status != last:
            say(f"  FAQ document: {label}")
            last = status
        if status in DOC_READY:
            return
        if status in DOC_FAILED:
            reason = str(doc.get("Reason") or "").strip()
            raise SetupError(
                f'ADP could not learn the FAQ document "{name}", DocBizId {doc_id} '
                f"({label}{': ' + reason if reason else ''}). Running this again deletes that copy and uploads "
                f"the FAQ again; if it fails the same way, deal with the reason first (fix {_shown(FAQ_PATH)}, "
                f"or make room in the knowledge base at {CONSOLE_URL})")
        if waiting.clock() >= deadline:
            raise SetupError(f'the FAQ document "{name}", DocBizId {doc_id}, was not learned within '
                             f"{waiting.timeout:.0f} seconds (still {label}); run this again later: "
                             "it waits for that copy instead of uploading another")
        waiting.sleep(waiting.interval)


def release(admin: AdpAdmin, app_id: str, app_status: int | None, waiting: Waiting,
            say: Callable[[str], None]) -> None:
    """Publish the app, which the chat API needs, and wait for it; skipped if nothing changed since the last one."""
    info = admin.call("DescribeReleaseInfo", {"BotBizId": app_id})
    if info.get("IsUpdated") is False and app_status == 2:
        say("Release: the released app is already up to date")
        return
    created = admin.call("CreateRelease", {"BotBizId": app_id, "Desc": "Glass Vault Help: FAQ and settings"})
    release_id = str(created.get("ReleaseBizId") or "")
    if not release_id:
        raise AdpApiError("CreateRelease", "BAD_RESPONSE", "no ReleaseBizId in the reply")
    deadline = waiting.clock() + waiting.timeout
    last = None
    while True:
        state = admin.call("DescribeRelease", {"BotBizId": app_id, "ReleaseBizId": release_id})
        status = state.get("Status")
        label = RELEASE_STATUS.get(status) or str(state.get("StatusDesc") or status)
        if status != last:
            say(f"  Release: {label}")
            last = status
        if status == RELEASE_DONE:
            return
        if status in RELEASE_FAILED:
            raise SetupError(f"the release did not go through ({label}); see Release Management in {CONSOLE_URL}")
        if waiting.clock() >= deadline:
            raise SetupError(f"the release did not finish within {waiting.timeout:.0f} seconds (still {label}); "
                             "run this again later")
        waiting.sleep(waiting.interval)


def app_key(admin: AdpAdmin, app_id: str) -> str:
    """The app's AppKey, which the chat API takes (GetAppSecret). Never printed."""
    data = admin.call("GetAppSecret", {"AppBizId": app_id})
    key = str(data.get("AppKey") or "")
    if not key:
        raise AdpApiError("GetAppSecret", "BAD_RESPONSE", "no AppKey in the reply")
    admin.secrets.append(key)
    return key


_ENV_LINE = re.compile(r"^\s*(?:export\s+)?GV_ADP_APP_KEY\s*=")


def write_env_key(path: Path, key: str) -> None:
    """Set GV_ADP_APP_KEY in the .env file at `path`, keeping every other line as it was.

    The whole file is written to a temporary file beside it, then moved into
    place in one step (os.replace), so a crash leaves the old file or the new
    one, never half of either. The result is readable by its owner only (0600),
    even if the old .env was readable by others: it holds keys.
    """
    if re.fullmatch(r"[A-Za-z0-9_.-]+", key):
        line = f"GV_ADP_APP_KEY={key}\n"
    elif "'" not in key and "\n" not in key:
        line = f"GV_ADP_APP_KEY='{key}'\n"  # python-dotenv takes a single-quoted value literally
    else:
        raise SetupError("the AppKey has characters .env cannot hold; copy it from the ADP console instead")
    target = Path(os.path.realpath(path))  # through a symlink to the file itself, so the link stays a link
    try:
        with target.open(encoding="utf-8", newline="") as f:  # newline="": line endings stay as they are
            old_lines = f.read().splitlines(keepends=True)
    except FileNotFoundError:
        old_lines = []
    lines, written = [], False
    for old in old_lines:
        if _ENV_LINE.match(old):
            if not written:
                lines.append(line)
                written = True
            continue  # a second GV_ADP_APP_KEY line would only shadow the new one
        lines.append(old)
    if not written:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(line)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")  # mode 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            os.fchmod(f.fileno(), 0o600)  # 0600 whatever the umask
            f.write("".join(lines))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _dict(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, dict) else {}


def _shown(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO))
    except ValueError:
        return str(path)


# --- The two commands ---------------------------------------------------------------


def check(admin: AdpAdmin, wanted_model: str | None, say: Callable[[str], None]) -> None:
    """Read-only: whether these keys reach ADP, whether Help's app exists, and which models it could use."""
    app = find_app(list_apps(admin))
    say(f"ADP is reachable on {API_HOST} ({API_REGION}).")
    if app:
        state = app.get("AppStatusDesc") or f"status {app.get('AppStatus')}"
        say(f'App "{APP_NAME}": found, AppBizId {app["AppBizId"]} ({state})')
    else:
        say(f'App "{APP_NAME}": not created yet')
    models = list_models(admin)
    say(f"Models for knowledge Q&A apps (standard mode); Help's role description is {len(ROLE)} characters:")
    for model in models:
        say(f"  {_model_line(model)}")
    if not models:
        say("  none")
    current = functools.partial(current_model, admin, str(app["AppBizId"])) if app else None
    say(f"Setup would use: {_model_label(choose_model(models, wanted_model, current=current, say=say))}")


def setup(admin: AdpAdmin, *, wanted_model: str | None, env_file: Path, waiting: Waiting,
          say: Callable[[str], None]) -> None:
    """Create or update Help's app, give it the FAQ, release it, and write its AppKey to `env_file`."""
    say(f"Tencent ADP on {API_HOST} ({API_REGION})")
    app = find_app(list_apps(admin))
    current = functools.partial(current_model, admin, str(app["AppBizId"])) if app else None
    # Before creating anything: a bad --model, or none at all, costs nothing.
    model = choose_model(list_models(admin), wanted_model, current=current, say=say)
    if app:
        app_id = str(app["AppBizId"])
        say(f'App "{APP_NAME}": found, AppBizId {app_id}')
    else:
        app_id = create_app(admin)
        say(f'App "{APP_NAME}": created, AppBizId {app_id}')
    say(f"Model: {_model_label(model)}")
    status = configure_app(admin, app_id, model)
    say("Settings: role, greeting and fallback reply written; answers come only from the FAQ")
    upload_faq(admin, app_id, FAQ_PATH.read_bytes(), waiting, say)
    release(admin, app_id, status, waiting, say)
    write_env_key(env_file, app_key(admin, app_id))
    say(f"GV_ADP_APP_KEY written to {_shown(env_file)}")
    say("Help is ready: restart the demo server to use it.")


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         http: httpx.Client | None = None, waiting: Waiting | None = None) -> int:
    """Run the setup (or `--check`); returns the exit status. Tests pass `environ`, `http` and `waiting`."""
    parser = argparse.ArgumentParser(
        description="Set up Help's knowledge-Q&A app on Tencent ADP (international site).")
    parser.add_argument("--check", action="store_true",
                        help="read-only: test access and list the models, without changing anything")
    parser.add_argument("--model", help="the ModelName to answer with (default: DeepSeek V4 Flash if ADP offers it); "
                        "used as given, with a warning, if ListModel does not list it")
    parser.add_argument("--timeout", type=float, default=600.0,
                        help="seconds to wait for the FAQ to be learned, and again for the release (default 600)")
    parser.add_argument("--env-file", type=Path, default=ENV_PATH,
                        help="the .env file to write GV_ADP_APP_KEY to (default: the repo's .env)")
    args = parser.parse_args(argv)

    if environ is None:
        from glassvault.llm import _env_with_dotenv

        environ = _env_with_dotenv()
    secret_id = (environ.get("GV_TENCENT_SECRET_ID") or "").strip()
    secret_key = (environ.get("GV_TENCENT_SECRET_KEY") or "").strip()
    if not (secret_id and secret_key):
        print("Set GV_TENCENT_SECRET_ID and GV_TENCENT_SECRET_KEY (the account's Tencent Cloud API keys) "
              "in .env first.", file=sys.stderr)
        return 2

    client = http or httpx.Client()
    admin = AdpAdmin(secret_id, secret_key, client)
    waiting = waiting or Waiting()
    waiting.timeout = args.timeout
    try:
        if args.check:
            check(admin, args.model, print)
        else:
            setup(admin, wanted_model=args.model, env_file=args.env_file, waiting=waiting, say=print)
        return 0
    except AdpApiError as e:
        print(f"{e.action} failed: {e.code}: {admin.redact(e.message)}"
              + (f" (RequestId {e.request_id})" if e.request_id else ""), file=sys.stderr)
        if needs_activation(e):
            print(ACTIVATE, file=sys.stderr)
        return 1
    except SetupError as e:
        print(f"{'Check' if args.check else 'Setup'} stopped: {admin.redact(str(e))}", file=sys.stderr)
        return 1
    finally:
        if http is None:
            client.close()


if __name__ == "__main__":
    sys.exit(main())
