"""scripts/adp_setup.py against a fake Tencent ADP and COS: signing, the read-only check, a full setup, errors."""

import hashlib
import hmac
import importlib.util
import json
import os
import sys
from pathlib import Path

import httpx
import pytest

from glassvault.faq import FAQ_PATH
from glassvault.voice import tc3_headers

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "adp_setup.py"


def _load_script():
    """scripts/ is not a package, so the script is loaded from its path."""
    spec = importlib.util.spec_from_file_location("adp_setup", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["adp_setup"] = module  # dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


adp = _load_script()

SECRET_ID = "AKIDfakeSecretIdForTests0000"
SECRET_KEY = "fakeSecretKeyForTests0000000000"
ENV = {"GV_TENCENT_SECRET_ID": SECRET_ID, "GV_TENCENT_SECRET_KEY": SECRET_KEY}
TMP_ID, TMP_KEY, TMP_TOKEN = "AKIDtmpCosId", "tmpCosSecretKey", "tmpCosSessionToken"
APP_KEY = "HuLJbSdLZfakeAppKey"
UPLOAD_PATH = "/corp/1737374499879124992/1801166480814637052/doc/QUiGRdWFAFBkDKjhuxbd-1796118032488595456.md"

MODELS = [
    {"ModelName": "youtu-llm", "AliasName": "Youtu", "ResourceStatus": 1, "IsDefault": True},
    {"ModelName": "deepseek-r1-0528", "AliasName": "DeepSeek-R1-0528", "ResourceStatus": 1, "RoleLenLimit": 2000},
    {"ModelName": "deepseek-v3.1", "AliasName": "DeepSeek-V3.1", "ResourceStatus": 1, "RoleLenLimit": 2000},
    {"ModelName": "deepseek-v4-flash", "AliasName": "DeepSeek-V4-Flash", "ResourceStatus": 1, "RoleLenLimit": 4000},
]
FAQ_NAME = adp.doc_name(FAQ_PATH.read_bytes())  # "Glass Vault FAQ <8 hex>.md"
NEW_DOC = "1694921462777761792"  # the DocBizId of the first SaveDoc; later ones count up from it


class FakeAdp:
    """ADP's management API and its COS bucket, in memory. Records every call in order.

    Documents are kept: SaveDoc adds one, DescribeDoc moves it through `doc_statuses`, and DeleteDoc
    removes it, so a second run sees what the first one left. ListApp pages from `page_base` (0 or 1).
    """

    def __init__(self, *, apps=(), docs=(), doc_statuses=(17, 13, 10), release_statuses=(2, 3), errors=None,
                 models=MODELS, page_base=1):
        self.apps = list(apps)
        self.docs = [dict(d) for d in docs]
        self.doc_statuses = list(doc_statuses)
        self.release_statuses = list(release_statuses)
        self.errors = dict(errors or {})  # action -> (code, message)
        self.models = models
        self.page_base = page_base
        self.saved = 0
        self.calls: list[tuple[str, dict]] = []
        self.requests: list[httpx.Request] = []
        self.uploaded: bytes | None = None

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def actions(self) -> list[str]:
        return [action for action, _ in self.calls]

    def params(self, action: str) -> dict:
        return next(p for a, p in self.calls if a == action)

    def all_params(self, action: str) -> list[dict]:
        return [p for a, p in self.calls if a == action]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "lke.intl.tencentcloudapi.com":
            action = request.headers["X-TC-Action"]
            params = json.loads(request.content)
            self.calls.append((action, params))
            if action in self.errors:
                code, message = self.errors[action]
                return httpx.Response(200, json={"Response": {"Error": {"Code": code, "Message": message},
                                                              "RequestId": "req-err"}})
            return httpx.Response(200, json={"Response": {**getattr(self, action)(params), "RequestId": "req"}})
        if request.url.host.endswith(".myqcloud.com") and request.method == "PUT":
            self.calls.append(("COS PUT", {"path": request.url.path}))
            self.uploaded = request.content
            return httpx.Response(200, headers={"ETag": '"58e88ad7665f11c4f66eba0eada383a5"',
                                                "x-cos-hash-crc64ecma": str(adp.crc64(request.content))})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    def ListApp(self, params):
        apps = [a for a in self.apps if params.get("Keyword", "") in a["Name"]]  # Keyword: a part of the name
        start = (params["PageNumber"] - self.page_base) * params["PageSize"]
        return {"Total": str(len(apps)), "List": apps[start:start + params["PageSize"]] if start >= 0 else []}

    def ListModel(self, params):
        return {"List": self.models}

    def CreateApp(self, params):
        return {"AppBizId": "1801166480814637052", "IsCustomList": False}

    def DescribeApp(self, params):
        return {
            "AppBizId": params["AppBizId"], "AppType": "knowledge_qa", "AppStatus": 1, "AppKey": APP_KEY,
            "BaseConfig": {"Name": "Glass Vault Help", "Avatar": "https://example.com/a.png", "Desc": "old"},
            "AppConfig": {"KnowledgeQa": {
                "Greeting": "", "RoleDescription": "", "Pattern": "standard",
                "Model": {"Name": "youtu-llm", "AliasName": "Youtu", "HistoryLimit": 5, "IsUseContext": True},
                "Output": {"Method": 1, "UseGeneralKnowledge": True, "BareAnswer": "", "UseRecommended": False},
                "Search": [{"Type": "qa", "IsEnabled": True, "QaTopN": 3, "Confidence": 0.7},
                           {"Type": "doc", "IsEnabled": False, "DocTopN": 5, "Confidence": 0.2},
                           {"Type": "search", "IsEnabled": True, "UseSearchEngine": True}],
                "Workflow": None,
            }, "Summary": None},
        }

    def ModifyApp(self, params):
        return {}

    def ListDoc(self, params):
        docs = [d for d in self.docs if params["Query"] in d["FileName"]]  # a filename query matches a part
        return {"Total": str(len(docs)), "List": docs}

    def DescribeStorageCredential(self, params):
        return {"Credentials": {"TmpSecretId": TMP_ID, "TmpSecretKey": TMP_KEY, "Token": TMP_TOKEN},
                "Bucket": "qidian-qbot-1251316161", "Region": "ap-jakarta", "Type": "cos",
                "UploadPath": UPLOAD_PATH, "StartTime": 1717062883, "ExpiredTime": 1717063483}

    def SaveDoc(self, params):
        doc_id = str(int(NEW_DOC) + self.saved)
        self.saved += 1
        self.docs.append({"DocBizId": doc_id, "FileName": params["FileName"], "Status": 7})
        return {"DocBizId": doc_id, "ErrorMsg": ""}

    def DescribeDoc(self, params):
        status = self.doc_statuses.pop(0) if len(self.doc_statuses) > 1 else self.doc_statuses[0]
        for doc in self.docs:
            if doc["DocBizId"] == params["DocBizId"]:
                doc["Status"] = status
        return {"DocBizId": params["DocBizId"], "Status": status, "StatusDesc": "", "Reason": "bad file"}

    def DeleteDoc(self, params):
        self.docs = [d for d in self.docs if d["DocBizId"] not in params["DocBizIds"]]
        return {}

    def DescribeReleaseInfo(self, params):
        return {"IsUpdated": True, "Status": 3}

    def CreateRelease(self, params):
        return {"ReleaseBizId": "1730526803028463616"}

    def DescribeRelease(self, params):
        status = self.release_statuses.pop(0) if len(self.release_statuses) > 1 else self.release_statuses[0]
        return {"Status": status, "StatusDesc": ""}

    def GetAppSecret(self, params):
        return {"AppKey": APP_KEY, "IsRelease": True, "HasPermission": True}


def no_wait():
    return adp.Waiting(interval=0, sleep=lambda seconds: None)


def run(fake: FakeAdp, *argv: str, waiting=None) -> int:
    with fake.client() as http:
        return adp.main(list(argv), environ=ENV, http=http, waiting=waiting or no_wait())


# --- Signing ------------------------------------------------------------------------------------


def test_a_call_is_signed_for_adp_on_the_international_site():
    fake = FakeAdp()
    with fake.client() as http:
        admin = adp.AdpAdmin(SECRET_ID, SECRET_KEY, http, clock=lambda: 1791792000)
        admin.call("ListApp", {"AppType": "knowledge_qa", "PageNumber": 1, "PageSize": 10})
    request = fake.requests[0]
    assert (request.method, str(request.url)) == ("POST", "https://lke.intl.tencentcloudapi.com/")
    assert request.headers["Host"] == "lke.intl.tencentcloudapi.com"
    assert request.headers["X-TC-Action"] == "ListApp"
    assert request.headers["X-TC-Version"] == "2023-11-30"
    assert request.headers["X-TC-Region"] == "ap-jakarta"
    assert request.headers["X-TC-Timestamp"] == "1791792000"
    assert request.headers["Authorization"].startswith(
        f"TC3-HMAC-SHA256 Credential={SECRET_ID}/2026-10-12/lke/tc3_request, SignedHeaders=content-type;host, ")
    # The signature covers exactly the bytes sent.
    expected = tc3_headers(secret_id=SECRET_ID, secret_key=SECRET_KEY, host="lke.intl.tencentcloudapi.com",
                           action="ListApp", version="2023-11-30", region="ap-jakarta", payload=request.content,
                           timestamp=1791792000)
    assert request.headers["Authorization"] == expected["Authorization"]
    assert json.loads(request.content) == {"AppType": "knowledge_qa", "PageNumber": 1, "PageSize": 10}


# The two worked examples of COS's "Request Signature" doc (https://cloud.tencent.com/document/product/436/7778,
# English: https://www.tencentcloud.com/document/product/436/7778). The doc masks its keys, so the HttpString and
# its SHA-1 are the checkable part.
def test_cos_http_string_matches_the_cos_docs_download_example():
    http_string = adp.cos_http_string(
        "GET", "/exampleobject(腾讯云)",
        {"response-content-type": "application/octet-stream", "response-cache-control": "max-age=600"},
        {"Date": "Thu, 16 May 2019 06:55:53 GMT", "Host": "examplebucket-1250000000.cos.ap-beijing.myqcloud.com"})
    assert http_string == (
        "get\n/exampleobject(腾讯云)\n"
        "response-cache-control=max-age%3D600&response-content-type=application%2Foctet-stream\n"
        "date=Thu%2C%2016%20May%202019%2006%3A55%3A53%20GMT&host=examplebucket-1250000000.cos.ap-beijing.myqcloud.com\n")
    assert hashlib.sha1(http_string.encode()).hexdigest() == "54ecfe22f59d3514fdc764b87a32d8133ea611e6"


def test_cos_http_string_matches_the_cos_docs_upload_example():
    http_string = adp.cos_http_string("PUT", "/exampleobject(腾讯云)", {}, {
        "Date": "Thu, 16 May 2019 06:45:51 GMT", "Host": "examplebucket-1250000000.cos.ap-beijing.myqcloud.com",
        "Content-Type": "text/plain", "Content-Length": "13", "Content-MD5": "mQ/fVh815F3k6TAUm8m0eg==",
        "x-cos-acl": "private", "x-cos-grant-read": 'uin="100000000011"'})
    assert hashlib.sha1(http_string.encode()).hexdigest() == "8b2751e77f43a0995d6e9eb9477f4b685cca4172"


def test_cos_authorization_follows_the_documented_derivation():
    headers = {"Host": "examplebucket-1250000000.cos.ap-beijing.myqcloud.com", "Content-Type": "text/markdown"}
    auth = adp.cos_authorization(secret_id="AKIDcos", secret_key="cosKey", method="PUT", path="/doc/a.md",
                                 headers=headers, start=1557989151, end=1557996351)
    key_time = "1557989151;1557996351"
    sign_key = hmac.new(b"cosKey", key_time.encode(), hashlib.sha1).hexdigest()
    http_string = "put\n/doc/a.md\n\ncontent-type=text%2Fmarkdown&host=examplebucket-1250000000.cos.ap-beijing.myqcloud.com\n"
    string_to_sign = f"sha1\n{key_time}\n{hashlib.sha1(http_string.encode()).hexdigest()}\n"
    signature = hmac.new(sign_key.encode(), string_to_sign.encode(), hashlib.sha1).hexdigest()
    assert auth == (f"q-sign-algorithm=sha1&q-ak=AKIDcos&q-sign-time={key_time}&q-key-time={key_time}"
                    f"&q-header-list=content-type;host&q-url-param-list=&q-signature={signature}")


def test_crc64_is_the_one_cos_reports():
    # CRC-64/XZ check value from the CRC catalogue; COS's x-cos-hash-crc64ecma uses the same parameters.
    assert adp.crc64(b"123456789") == 0x995DC9BBDF1939FA
    assert adp.crc64(b"") == 0


# --- The read-only check ------------------------------------------------------------------------


def test_check_reports_access_and_models_and_changes_nothing(capsys):
    fake = FakeAdp(apps=[{"Name": "Glass Vault Help", "AppBizId": "42", "AppStatus": 2,
                          "AppStatusDesc": "running"}])
    assert run(fake, "--check") == 0
    out = capsys.readouterr().out
    assert fake.actions() == ["ListApp", "ListModel"]
    assert fake.params("ListApp") == {"AppType": "knowledge_qa", "Keyword": "Glass Vault Help", "PageSize": 50,
                                      "PageNumber": 1}
    assert fake.params("ListModel") == {"AppType": "knowledge_qa", "Pattern": "standard"}
    assert "ADP is reachable" in out and "found, AppBizId 42 (running)" in out
    assert "deepseek-v4-flash (DeepSeek-V4-Flash): available, RoleLenLimit 4000\n" in out
    assert "youtu-llm (Youtu): available, RoleLenLimit not given\n" in out
    assert f"role description is {len(adp.ROLE)} characters" in out
    assert "Setup would use: deepseek-v4-flash" in out


def test_check_marks_a_model_whose_role_limit_is_too_short(capsys):
    short = [{**m, "RoleLenLimit": 500} if m["ModelName"] == "deepseek-v4-flash" else m for m in MODELS]
    assert run(FakeAdp(models=short), "--check") == 0
    out = capsys.readouterr().out
    assert (f"deepseek-v4-flash (DeepSeek-V4-Flash): available, RoleLenLimit 500, "
            f"too short for Help's {len(adp.ROLE)}-character role") in out
    assert "Setup would use: deepseek-v3.1" in out


def test_an_empty_first_page_of_apps_is_read_again_as_page_0(capsys):
    # ListApp's doc does not say whether pages count from 0 or 1; on a server that counts from 0,
    # page 1 is empty while Total says there is an app.
    fake = FakeAdp(apps=[{"Name": "Glass Vault Help", "AppBizId": "42", "AppStatus": 2}], page_base=0)
    assert run(fake, "--check") == 0
    assert [p["PageNumber"] for p in fake.all_params("ListApp")] == [1, 0]
    assert "found, AppBizId 42" in capsys.readouterr().out


def test_apps_on_pages_counted_from_1_are_read_once():
    apps = [{"Name": f"Glass Vault Help {i}", "AppBizId": str(i)} for i in range(60)]
    fake = FakeAdp(apps=[*apps, {"Name": "Glass Vault Help", "AppBizId": "4200"}])
    with fake.client() as http:
        listed = adp.list_apps(adp.AdpAdmin(SECRET_ID, SECRET_KEY, http))
    assert [p["PageNumber"] for p in fake.all_params("ListApp")] == [1, 2]  # no page 0: Total was reached
    assert len(listed) == 61 and adp.find_app(listed)["AppBizId"] == "4200"  # the exact name, not a longer one


def test_check_explains_how_to_activate_adp(capsys):
    fake = FakeAdp(errors={"ListApp": ("FailedOperation", "450006-The user is not logged in or has not registered")})
    assert run(fake, "--check") == 1
    err = capsys.readouterr().err
    assert "ListApp failed: FailedOperation: 450006-The user is not logged in or has not registered" in err
    assert "Activate ADP and claim the Free plan at https://adp.tencentcloud.com, then run this again." in err


# --- The setup ----------------------------------------------------------------------------------


def test_setup_creates_the_app_uploads_the_faq_releases_and_writes_the_key(tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("# Local settings\nGV_LLM_API_KEY=keep-me\nGV_ADP_APP_KEY=old\n\nGV_OTHER=1")
    fake = FakeAdp()
    assert run(fake, "--env-file", str(env_file)) == 0
    assert fake.actions() == [
        "ListApp", "ListModel", "CreateApp", "DescribeApp", "ModifyApp", "ListDoc", "DescribeStorageCredential",
        "COS PUT", "SaveDoc", "DescribeDoc", "DescribeDoc", "DescribeDoc", "DescribeReleaseInfo", "CreateRelease",
        "DescribeRelease", "DescribeRelease", "GetAppSecret",
    ]
    assert fake.params("CreateApp") == {
        "AppType": "knowledge_qa", "Pattern": "standard", "AgentType": "dialogue",
        "BaseConfig": {"Name": "Glass Vault Help", "Avatar": adp.APP_AVATAR, "Desc": adp.APP_DESC}}

    modify = fake.params("ModifyApp")
    assert (modify["AppBizId"], modify["AppType"]) == ("1801166480814637052", "knowledge_qa")
    assert modify["BaseConfig"] == {"Name": "Glass Vault Help", "Avatar": "https://example.com/a.png",
                                    "Desc": adp.APP_DESC}
    qa = modify["AppConfig"]["KnowledgeQa"]
    assert qa["Model"] == {"Name": "deepseek-v4-flash", "AliasName": "DeepSeek-V4-Flash", "HistoryLimit": 5,
                           "IsUseContext": True}
    assert qa["RoleDescription"] == adp.ROLE and "not DBS" in qa["RoleDescription"]
    assert qa["Greeting"] == adp.GREETING and qa["Pattern"] == "standard"
    assert qa["Output"] == {"Method": 1, "UseGeneralKnowledge": False, "BareAnswer": adp.BARE_ANSWER,
                            "UseRecommended": False}
    assert {s["Type"]: s["IsEnabled"] for s in qa["Search"]} == {"qa": True, "doc": True, "search": False}
    assert "Workflow" not in qa and "Summary" not in modify["AppConfig"]  # nulls are left out

    faq = FAQ_PATH.read_bytes()
    assert fake.uploaded == faq
    put = next(r for r in fake.requests if r.method == "PUT")
    assert str(put.url) == f"https://qidian-qbot-1251316161.cos.ap-jakarta.myqcloud.com{UPLOAD_PATH}"
    assert put.headers["x-cos-security-token"] == TMP_TOKEN
    assert put.headers["Authorization"].startswith(f"q-sign-algorithm=sha1&q-ak={TMP_ID}&q-sign-time=")
    assert "&q-header-list=content-type;host;x-cos-security-token&q-url-param-list=&" in put.headers["Authorization"]

    assert fake.params("ListDoc") == {"BotBizId": "1801166480814637052", "PageNumber": 1, "PageSize": 100,
                                      "Query": "Glass Vault FAQ", "QueryType": "filename"}
    # Named by content; no DuplicateFileHandles, which could hand back a failed or debug-only copy.
    assert fake.params("SaveDoc") == {
        "BotBizId": "1801166480814637052", "FileName": FAQ_NAME, "FileType": "md",
        "CosUrl": UPLOAD_PATH, "ETag": '"58e88ad7665f11c4f66eba0eada383a5"', "CosHash": str(adp.crc64(faq)),
        "Size": str(len(faq)), "AttrRange": 1, "Source": 0, "Opt": 2, "EnableScope": 4}
    assert "DeleteDoc" not in fake.actions()
    assert fake.params("CreateRelease")["BotBizId"] == "1801166480814637052"
    assert fake.params("DescribeRelease") == {"BotBizId": "1801166480814637052",
                                              "ReleaseBizId": "1730526803028463616"}

    assert env_file.read_text() == (f"# Local settings\nGV_LLM_API_KEY=keep-me\nGV_ADP_APP_KEY={APP_KEY}\n\n"
                                    "GV_OTHER=1")
    out, err = capsys.readouterr()
    assert f"GV_ADP_APP_KEY written to {env_file}" in out
    assert "Model: deepseek-v4-flash (DeepSeek-V4-Flash)" in out
    for secret in (APP_KEY, SECRET_ID, SECRET_KEY, TMP_ID, TMP_KEY, TMP_TOKEN):
        assert secret not in out + err


def test_setup_reuses_the_app_and_replaces_an_older_faq_once_the_new_one_is_learned(tmp_path, capsys):
    env_file = tmp_path / ".env"  # not there yet: created, readable by its owner only
    fake = FakeAdp(apps=[{"Name": "Glass Vault Help (old)", "AppBizId": "7"},
                         {"Name": "Glass Vault Help", "AppBizId": "42", "AppStatus": 2}],
                   docs=[{"DocBizId": "900", "FileName": "Glass Vault FAQ.md", "Status": 12},  # the old, unhashed name
                         {"DocBizId": "902", "FileName": "Glass Vault FAQ 0badc0de.md", "Status": 14},
                         {"DocBizId": "903", "FileName": "Glass Vault FAQ 00c0ffee.md", "Status": 6},  # deleted
                         {"DocBizId": "904", "FileName": "Glass Vault FAQ 0decade0.md", "Status": 12,
                          "IsDeleted": True},
                         {"DocBizId": "901", "FileName": "Something else.md", "Status": 12}])
    assert run(fake, "--env-file", str(env_file), "--model", "deepseek-v3.1") == 0
    actions = fake.actions()
    assert "CreateApp" not in actions
    assert fake.params("DescribeApp")["AppBizId"] == "42"
    assert fake.params("ModifyApp")["AppConfig"]["KnowledgeQa"]["Model"]["Name"] == "deepseek-v3.1"
    assert fake.params("ListDoc") == {"BotBizId": "42", "PageNumber": 1, "PageSize": 100,
                                      "Query": "Glass Vault FAQ", "QueryType": "filename"}
    assert fake.params("SaveDoc")["FileName"] == FAQ_NAME
    # The old copies go only after the new one is learned, and before the release; never the new one.
    assert actions.index("DeleteDoc") > max(i for i, a in enumerate(actions) if a == "DescribeDoc")
    assert actions.index("DeleteDoc") < actions.index("CreateRelease")
    assert fake.all_params("DeleteDoc") == [{"BotBizId": "42", "DocBizIds": ["900", "902"]}]
    assert [d["DocBizId"] for d in fake.docs if not d.get("IsDeleted")] == ["903", "901", NEW_DOC]
    assert env_file.read_text() == f"GV_ADP_APP_KEY={APP_KEY}\n"
    assert os.stat(env_file).st_mode & 0o777 == 0o600
    out = capsys.readouterr().out
    assert f'uploaded "{FAQ_NAME}"' in out and f"DocBizId {NEW_DOC}" in out
    assert "deleted 2 older copies" in out


def test_each_faq_gets_its_own_name():
    faq = FAQ_PATH.read_bytes()
    assert FAQ_NAME == f"Glass Vault FAQ {hashlib.sha256(faq).hexdigest()[:8]}.md"
    assert adp.doc_name(faq) == FAQ_NAME  # the same content, the same name
    assert adp.doc_name(faq + b"\nOne more line.\n") != FAQ_NAME
    assert adp.doc_name(b"").startswith(adp.DOC_PREFIX + " ") and adp.doc_name(b"").endswith(".md")


def test_an_faq_already_learned_is_not_uploaded_again_and_a_current_release_is_not_repeated(tmp_path, capsys):
    class UpToDate(FakeAdp):
        def DescribeApp(self, params):
            return {**super().DescribeApp(params), "AppStatus": 2}

        def DescribeReleaseInfo(self, params):
            return {"IsUpdated": False, "Status": 3}

    fake = UpToDate(apps=[{"Name": "Glass Vault Help", "AppBizId": "42", "AppStatus": 2}],
                    docs=[{"DocBizId": "950", "FileName": FAQ_NAME, "Status": 12}])
    assert run(fake, "--env-file", str(tmp_path / ".env")) == 0
    for action in ("DescribeStorageCredential", "COS PUT", "SaveDoc", "DescribeDoc", "DeleteDoc", "CreateRelease"):
        assert action not in fake.actions()
    out = capsys.readouterr().out
    assert f'"{FAQ_NAME}" is already learned (DocBizId 950)' in out and "already up to date" in out


def test_a_learned_copy_stays_and_older_ones_still_go(tmp_path):
    fake = FakeAdp(apps=[{"Name": "Glass Vault Help", "AppBizId": "42", "AppStatus": 2}],
                   docs=[{"DocBizId": "900", "FileName": "Glass Vault FAQ.md", "Status": 12},
                         {"DocBizId": "950", "FileName": FAQ_NAME, "Status": 10},
                         {"DocBizId": "951", "FileName": FAQ_NAME, "Status": 18}])  # a failed twin goes too
    assert run(fake, "--env-file", str(tmp_path / ".env")) == 0
    assert "SaveDoc" not in fake.actions()
    assert fake.all_params("DeleteDoc") == [{"BotBizId": "42", "DocBizIds": ["900", "951"]}]
    assert [d["DocBizId"] for d in fake.docs] == ["950"]


def test_a_copy_still_being_learned_is_waited_for_instead_of_uploading_another(tmp_path, capsys):
    fake = FakeAdp(apps=[{"Name": "Glass Vault Help", "AppBizId": "42", "AppStatus": 2}],
                   docs=[{"DocBizId": "950", "FileName": FAQ_NAME, "Status": 13}], doc_statuses=(13, 10))
    assert run(fake, "--env-file", str(tmp_path / ".env")) == 0
    assert "SaveDoc" not in fake.actions() and "DeleteDoc" not in fake.actions()
    assert {p["DocBizId"] for p in fake.all_params("DescribeDoc")} == {"950"}
    assert "already uploaded (DocBizId 950); waiting" in capsys.readouterr().out


def test_a_failed_copy_is_deleted_and_the_faq_uploaded_again(tmp_path, capsys):
    fake = FakeAdp(apps=[{"Name": "Glass Vault Help", "AppBizId": "42", "AppStatus": 2}],
                   docs=[{"DocBizId": "960", "FileName": FAQ_NAME, "Status": 18},
                         {"DocBizId": "900", "FileName": "Glass Vault FAQ.md", "Status": 12}])
    assert run(fake, "--env-file", str(tmp_path / ".env")) == 0
    actions = fake.actions()
    # The failed copy goes before the new upload; the older one only once the new copy is learned.
    assert fake.all_params("DeleteDoc") == [{"BotBizId": "42", "DocBizIds": ["960"]},
                                            {"BotBizId": "42", "DocBizIds": ["900"]}]
    first_delete = actions.index("DeleteDoc")
    assert first_delete < actions.index("DescribeStorageCredential") < actions.index("SaveDoc")
    assert fake.params("SaveDoc")["FileName"] == FAQ_NAME
    assert [d["DocBizId"] for d in fake.docs] == [NEW_DOC]
    out = capsys.readouterr().out
    assert "an earlier copy of" in out and "(DocBizId 960, parsing failed); deleting it and uploading again" in out


def test_a_failed_upload_is_recovered_by_running_again(tmp_path, capsys):
    env_file = tmp_path / ".env"
    fake = FakeAdp(doc_statuses=(17, 18))
    assert run(fake, "--env-file", str(env_file)) == 1
    assert [(d["DocBizId"], d["Status"]) for d in fake.docs] == [(NEW_DOC, 18)]
    capsys.readouterr()

    fake.apps = [{"Name": "Glass Vault Help", "AppBizId": "1801166480814637052", "AppStatus": 1}]
    fake.doc_statuses = [17, 10]
    fake.calls.clear()
    assert run(fake, "--env-file", str(env_file)) == 0
    assert "CreateApp" not in fake.actions()
    assert fake.all_params("DeleteDoc") == [{"BotBizId": "1801166480814637052", "DocBizIds": [NEW_DOC]}]
    second = str(int(NEW_DOC) + 1)
    assert [(d["DocBizId"], d["Status"]) for d in fake.docs] == [(second, 10)]
    assert env_file.read_text() == f"GV_ADP_APP_KEY={APP_KEY}\n"


def test_a_failed_copy_adp_will_not_delete_is_named_with_a_way_out(tmp_path, capsys):
    fake = FakeAdp(apps=[{"Name": "Glass Vault Help", "AppBizId": "42", "AppStatus": 2}],
                   docs=[{"DocBizId": "960", "FileName": FAQ_NAME, "Status": 18}],
                   errors={"DeleteDoc": ("FailedOperation", "the document cannot be deleted now")})
    assert run(fake, "--env-file", str(tmp_path / ".env")) == 1
    err = capsys.readouterr().err
    assert "could not delete the failed FAQ copy (DocBizId 960)" in err
    assert "DeleteDoc failed: FailedOperation: the document cannot be deleted now" in err
    assert "Run this again later, or delete it under Knowledge in https://adp.tencentcloud.com first" in err
    assert "SaveDoc" not in fake.actions()


def test_an_api_error_stops_the_setup_without_printing_secrets(tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("GV_LLM_API_KEY=keep-me\n")
    # A server that echoes the caller's keys back must not get them printed.
    fake = FakeAdp(errors={"ModifyApp": ("InvalidParameter", f"bad AppConfig for {SECRET_ID} / {SECRET_KEY}")})
    assert run(fake, "--env-file", str(env_file)) == 1
    out, err = capsys.readouterr()
    assert "ModifyApp failed: InvalidParameter: bad AppConfig for *** / ***" in err
    assert "(RequestId req-err)" in err
    assert "Activate ADP" not in err
    for secret in (SECRET_ID, SECRET_KEY, APP_KEY):
        assert secret not in out + err
    assert fake.actions()[-1] == "ModifyApp"  # nothing after the failure
    assert env_file.read_text() == "GV_LLM_API_KEY=keep-me\n"


def test_a_document_adp_cannot_learn_stops_the_setup_and_says_how_to_recover(tmp_path, capsys):
    fake = FakeAdp(doc_statuses=(17, 18))
    assert run(fake, "--env-file", str(tmp_path / ".env")) == 1
    err = capsys.readouterr().err
    assert (f'could not learn the FAQ document "{FAQ_NAME}", DocBizId {NEW_DOC} (parsing failed: bad file). '
            "Running this again deletes that copy and uploads the FAQ again") in err
    assert "fix src/glassvault/faq.md" in err
    assert "CreateRelease" not in fake.actions() and "DeleteDoc" not in fake.actions()
    assert not (tmp_path / ".env").exists()


def test_waiting_for_the_faq_has_a_timeout_that_names_the_copy(tmp_path, capsys):
    now = [0.0]
    waiting = adp.Waiting(interval=5, sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
                          clock=lambda: now[0])
    fake = FakeAdp(doc_statuses=(13,))
    assert run(fake, "--env-file", str(tmp_path / ".env"), "--timeout", "30", waiting=waiting) == 1
    err = capsys.readouterr().err
    assert f'"{FAQ_NAME}", DocBizId {NEW_DOC}, was not learned within 30 seconds (still learning)' in err
    assert "it waits for that copy instead of uploading another" in err


def test_waiting_for_the_release_has_a_timeout(tmp_path, capsys):
    now = [0.0]
    waiting = adp.Waiting(interval=5, sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
                          clock=lambda: now[0])
    fake = FakeAdp(release_statuses=(2,))
    assert run(fake, "--env-file", str(tmp_path / ".env"), "--timeout", "30", waiting=waiting) == 1
    assert "the release did not finish within 30 seconds (still releasing)" in capsys.readouterr().err
    assert "GetAppSecret" not in fake.actions()


def test_a_cos_host_from_adp_must_be_a_plain_cos_name(tmp_path, capsys):
    class Hostile(FakeAdp):
        def DescribeStorageCredential(self, params):
            return {**super().DescribeStorageCredential(params), "Bucket": "evil.example.com/x?"}

    fake = Hostile()
    assert run(fake, "--env-file", str(tmp_path / ".env")) == 1
    assert "the bucket or region is not a COS name" in capsys.readouterr().err
    assert "COS PUT" not in fake.actions()


def test_missing_keys_are_explained(capsys):
    assert adp.main(["--check"], environ={}) == 2
    assert "Set GV_TENCENT_SECRET_ID and GV_TENCENT_SECRET_KEY" in capsys.readouterr().err


# --- Choosing the model -------------------------------------------------------------------------


def test_the_model_is_deepseek_v4_flash_when_offered_and_has_quota():
    assert adp.choose_model(MODELS)["ModelName"] == "deepseek-v4-flash"
    used_up = [{**m, "ResourceStatus": 2} if m["ModelName"] == "deepseek-v4-flash" else m for m in MODELS]
    assert adp.choose_model(used_up)["ModelName"] == "deepseek-v3.1"  # a chat model, never the reasoning R1
    assert adp.choose_model(MODELS[:2])["ModelName"] == "youtu-llm"  # ADP's default before a reasoning model
    assert adp.choose_model(MODELS, "DeepSeek-V3.1")["ModelName"] == "deepseek-v3.1"  # an alias works too


def test_a_model_whose_role_limit_is_too_short_is_skipped():
    role = len(adp.ROLE)
    short = [{**m, "RoleLenLimit": role - 1} if m["ModelName"] == "deepseek-v4-flash" else m for m in MODELS]
    assert adp.choose_model(short)["ModelName"] == "deepseek-v3.1"
    exact = [{**m, "RoleLenLimit": role} if m["ModelName"] == "deepseek-v4-flash" else m for m in MODELS]
    assert adp.choose_model(exact)["ModelName"] == "deepseek-v4-flash"  # a limit of exactly the role's length fits
    unset = [{**m, "RoleLenLimit": 0} for m in MODELS]
    assert adp.choose_model(unset)["ModelName"] == "deepseek-v4-flash"  # 0 means not given
    with pytest.raises(adp.SetupError, match=rf"--model 'deepseek-v4-flash' takes a role description of at most "
                                             rf"{role - 1} characters; Help's is {role}"):
        adp.choose_model(short, "deepseek-v4-flash")  # named outright: refused, not swapped for another


def test_a_model_too_short_for_the_role_is_refused_before_the_app_is_created(tmp_path, capsys):
    short = [{**m, "RoleLenLimit": 300} for m in MODELS]
    fake = FakeAdp(models=short)
    assert run(fake, "--env-file", str(tmp_path / ".env"), "--model", "deepseek-v4-flash") == 1
    assert "Setup stopped: --model 'deepseek-v4-flash' takes a role description of at most 300" in (
        capsys.readouterr().err)
    assert fake.actions() == ["ListApp", "ListModel"]

    fake = FakeAdp(models=short)  # no --model, and no model fits: nothing is created either
    assert run(fake, "--env-file", str(tmp_path / ".env")) == 1
    err = capsys.readouterr().err
    assert f"ListModel offered no model that takes Help's {len(adp.ROLE)}-character role description" in err
    assert "deepseek-v4-flash (DeepSeek-V4-Flash): available, RoleLenLimit 300, too short" in err
    assert "Pass --model with a ModelName from the ADP console; --check lists what ListModel offers" in err
    assert fake.actions() == ["ListApp", "ListModel"]


def test_without_a_usable_model_an_existing_app_keeps_its_own(tmp_path, capsys):
    fake = FakeAdp(apps=[{"Name": "Glass Vault Help", "AppBizId": "42", "AppStatus": 2}], models=[])
    assert run(fake, "--env-file", str(tmp_path / ".env")) == 0
    assert fake.actions()[:3] == ["ListApp", "ListModel", "DescribeApp"]
    model = fake.params("ModifyApp")["AppConfig"]["KnowledgeQa"]["Model"]
    assert (model["Name"], model["AliasName"]) == ("youtu-llm", "Youtu")
    assert "so the app keeps its current one, youtu-llm (Youtu)" in capsys.readouterr().out


def test_without_any_model_and_without_an_app_the_check_says_to_pass_model(capsys):
    fake = FakeAdp(models=[])
    assert run(fake, "--check") == 1
    out, err = capsys.readouterr()
    assert "  none\n" in out
    assert "Check stopped: ListModel offered no model" in err and "(offered: none)" in err
    assert fake.actions() == ["ListApp", "ListModel"]


def test_a_model_listmodel_does_not_list_is_used_as_given_with_a_warning(tmp_path, capsys):
    fake = FakeAdp()
    assert run(fake, "--env-file", str(tmp_path / ".env"), "--model", "hunyuan-turbos") == 0
    model = fake.params("ModifyApp")["AppConfig"]["KnowledgeQa"]["Model"]
    assert (model["Name"], model["AliasName"]) == ("hunyuan-turbos", "hunyuan-turbos")
    out = capsys.readouterr().out
    assert "Warning: ListModel does not list --model 'hunyuan-turbos' (it offered: youtu-llm," in out
    assert "using it as given" in out and "Model: hunyuan-turbos\n" in out


def test_check_warns_about_an_unlisted_model_and_changes_nothing(capsys):
    fake = FakeAdp()
    assert run(fake, "--check", "--model", "gpt-9") == 0
    out = capsys.readouterr().out
    assert "Warning: ListModel does not list --model 'gpt-9'" in out and "Setup would use: gpt-9" in out
    assert fake.actions() == ["ListApp", "ListModel"]


# --- Writing .env -------------------------------------------------------------------------------


def test_the_key_is_written_atomically_keeping_every_other_line_at_0600(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_bytes(b"# Local\r\nGV_LLM_API_KEY=keep-me\r\nexport GV_ADP_APP_KEY=old\nGV_ADP_APP_KEY=dup\n"
                         b"GV_OTHER='a b'")
    env_file.chmod(0o644)  # readable by others: the replacement is not
    adp.write_env_key(env_file, "new.Key-1")
    assert env_file.read_bytes() == (b"# Local\r\nGV_LLM_API_KEY=keep-me\r\nGV_ADP_APP_KEY=new.Key-1\n"
                                     b"GV_OTHER='a b'")
    assert os.stat(env_file).st_mode & 0o777 == 0o600
    assert [p.name for p in tmp_path.iterdir()] == [".env"]  # no temporary file left behind

    adp.write_env_key(env_file, "has space")
    assert env_file.read_text().splitlines()[2] == "GV_ADP_APP_KEY='has space'"


def test_a_failed_write_leaves_the_old_env_whole(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("GV_LLM_API_KEY=keep-me\n")

    def refuse(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(adp.os, "replace", refuse)
    with pytest.raises(OSError, match="disk full"):
        adp.write_env_key(env_file, "new")
    assert env_file.read_text() == "GV_LLM_API_KEY=keep-me\n"
    assert [p.name for p in tmp_path.iterdir()] == [".env"]


def test_writing_through_a_symlink_keeps_the_link(tmp_path):
    real = tmp_path / "real.env"
    real.write_text("A=1\n")
    link = tmp_path / ".env"
    link.symlink_to(real)
    adp.write_env_key(link, "k")
    assert link.is_symlink() and real.read_text() == "A=1\nGV_ADP_APP_KEY=k\n"
    assert os.stat(real).st_mode & 0o777 == 0o600
