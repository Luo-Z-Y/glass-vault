"""Hosting the demo for others: no sign-in, a demo per visitor, and a cap on paid calls."""

import importlib
from collections import OrderedDict, deque

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def server(monkeypatch, tmp_path):
    # The offline LLM and no TRTC or ADP: no keys in the environment and no .env where the server looks.
    for key in ("GV_LLM_API_KEY", "GV_TRTC_SDK_APP_ID", "GV_ADP_APP_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    server = importlib.import_module("glassvault.web.server")
    monkeypatch.setattr(server, "demo", server.new_demo())
    monkeypatch.setattr(server, "SESSIONS", False)
    monkeypatch.setattr(server, "PAID_CALLS_PER_HOUR", 0)
    monkeypatch.setattr(server, "_sessions", OrderedDict())
    monkeypatch.setattr(server, "_paid_calls", deque())
    return server


def test_by_default_nothing_changes_one_shared_demo(server):
    with TestClient(server.app) as a, TestClient(server.app) as b:
        assert a.get("/").status_code == 200
        a.post("/api/rogue", json={"attack": "extra_transfer"})
        assert b.get("/api/state").json()["rogue"] == "extra_transfer"
        assert "gv_visitor" not in a.cookies


def test_anyone_with_the_link_gets_in_and_the_health_check_answers(server, monkeypatch):
    monkeypatch.setattr(server, "SESSIONS", True)  # as hosted
    with TestClient(server.app) as client:
        for path in ("/", "/api/state", "/static/app.js"):
            opened = client.get(path)
            assert opened.status_code == 200 and "www-authenticate" not in opened.headers
        assert client.post("/api/message", json={"text": "Send mum 300"}).status_code == 200
        assert client.get("/healthz").json() == {"status": "ok"}  # for the container's health check


def test_each_visitor_has_a_demo_of_their_own(server, monkeypatch):
    monkeypatch.setattr(server, "SESSIONS", True)
    with TestClient(server.app) as a, TestClient(server.app) as b:
        a.get("/")
        b.get("/")
        assert a.cookies["gv_visitor"] != b.cookies["gv_visitor"]

        a.post("/api/rogue", json={"attack": "extra_transfer"})
        assert a.get("/api/state").json()["rogue"] == "extra_transfer"
        assert b.get("/api/state").json()["rogue"] is None

        outcome = a.post("/api/message", json={"text": "Send mum 300 for groceries"}).json()["outcome"]
        assert outcome["kind"] == "frozen"  # a's rogue drafter, not b's honest one
        outcome = b.post("/api/message", json={"text": "Send mum 300 for groceries"}).json()["outcome"]
        assert outcome["kind"] == "confirm"
        # b's approval is b's: a never saw that challenge, and a's balances do not move.
        nonce = outcome["challenge"]["nonce"]
        stolen = a.post("/api/approve", json={"nonce": nonce, "simulated": True})
        assert stolen.status_code == 401 and stolen.json()["code"] == "SIGNATURE_REQUIRED"  # a can't sign it
        assert b.post("/api/approve", json={"nonce": nonce, "simulated": True}).status_code == 200
        balance = lambda c: {x["id"]: x["balance"] for x in c.get("/api/state").json()["accounts"]}["SAV"]  # noqa: E731
        assert balance(a) == "12400.00" and balance(b) == "12100.00"
        assert len(a.get("/api/audit").json()["entries"]) < len(b.get("/api/audit").json()["entries"])

        # Reset starts the visitor's own demo again, nobody else's.
        a.post("/api/reset")
        assert a.get("/api/state").json()["rogue"] is None and balance(b) == "12100.00"
    assert server.demo.rogue is None  # the shared demo was never touched


def test_a_visitor_cookie_is_kept_and_a_forged_one_replaced(server, monkeypatch):
    monkeypatch.setattr(server, "SESSIONS", True)
    with TestClient(server.app) as client:
        first = client.get("/").cookies["gv_visitor"]
        response = client.get("/api/state")
        assert "gv_visitor" not in response.cookies and client.cookies["gv_visitor"] == first
        client.cookies.set("gv_visitor", "short")
        assert client.get("/api/state").cookies["gv_visitor"] not in ("short", first)


def test_the_least_recently_used_demo_goes_when_there_are_too_many(server, monkeypatch):
    monkeypatch.setattr(server, "SESSIONS", True)
    monkeypatch.setattr(server, "MAX_SESSIONS", 2)
    clients = [TestClient(server.app) for _ in range(3)]
    for c in clients:
        c.get("/")  # the page sets the visitor cookie
        c.post("/api/rogue", json={"attack": "extra_transfer"})
    assert len(server._sessions) == 2
    assert clients[0].get("/api/state").json()["rogue"] is None  # its demo was dropped; it got a fresh one
    assert clients[2].get("/api/state").json()["rogue"] == "extra_transfer"


class Live:
    """A stand-in for a live model: the offline LLM's answers, but not an OfflineLLM, so it counts."""

    def __init__(self, inner):
        self.inner = inner
        self.name = "live-stand-in@example"

    def __getattr__(self, name):
        return getattr(self.inner, name)


class FakeTts:
    name = "fake-tts"

    def __init__(self):
        self.spoken = []

    def synthesize(self, script):
        self.spoken.append(script)
        return b"RIFF" + script.encode()


def test_paid_calls_are_capped_per_hour_and_only_spending_calls_count(server, monkeypatch):
    monkeypatch.setattr(server, "PAID_CALLS_PER_HOUR", 3)
    server.demo.drafter = Live(server.demo.drafter)
    server.demo.tts = FakeTts()
    with TestClient(server.app) as a:
        # Refusals that spend nothing are not counted.
        assert a.post("/api/message", json={"text": "   "}).status_code == 400
        assert a.get("/api/voice/" + "0" * 64).status_code == 404
        assert a.post("/api/faq", json={"text": "What are my transfer limits?"}).status_code == 200  # built-in FAQ
        assert server._paid_calls == deque()

        outcome = a.post("/api/message", json={"text": "Send mum 300 for groceries"}).json()["outcome"]  # 1
        assert a.get(f"/api/voice/{outcome['speech']}").status_code == 200  # 2: synthesised
        assert a.get(f"/api/voice/{outcome['speech']}").status_code == 200  # cached: free
        assert a.post("/api/message", json={"text": "What's my balance?"}).status_code == 200  # 3
        busy = a.post("/api/message", json={"text": "What's my balance?"})
        assert busy.status_code == 429 and busy.json()["code"] == "BUSY"
        assert a.get("/api/state").status_code == 200  # free endpoints keep working
        assert a.post("/api/reset").status_code == 200

    server._paid_calls[0] -= 3601  # an hour later, the oldest call no longer counts
    server.demo.drafter = Live(server.demo.drafter)
    with TestClient(server.app) as a:
        assert a.post("/api/message", json={"text": "What's my balance?"}).status_code == 200


def test_requests_without_a_visitor_cookie_never_push_out_a_judges_demo(server, monkeypatch):
    monkeypatch.setattr(server, "SESSIONS", True)
    monkeypatch.setattr(server, "MAX_SESSIONS", 2)
    with TestClient(server.app) as judge:
        judge.get("/")
        judge.post("/api/rogue", json={"attack": "extra_transfer"})
        for _ in range(20):  # a script that never keeps cookies
            with TestClient(server.app) as stranger:
                assert stranger.post("/api/rogue", json={"attack": "swap_payee"}).json()["state"]["rogue"] == "swap_payee"
        assert judge.get("/api/state").json()["rogue"] == "extra_transfer"
    assert len(server._sessions) == 1


def test_bodies_over_4_mb_and_writes_from_other_sites_are_refused(server):
    with TestClient(server.app) as client:
        assert client.post("/api/message", content=b"x" * (5 * 1024 * 1024),
                           headers={"Content-Type": "application/json"}).status_code == 413
        for headers in ({"Sec-Fetch-Site": "same-site"}, {"Origin": "https://1-2-3-4.sslip.io"}):
            refused = client.post("/api/reset", headers=headers)
            assert refused.status_code == 403 and refused.json()["code"] == "CROSS_SITE"
        assert client.post("/api/reset", headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200
        assert client.get("/api/state", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200  # reads stay open


def test_over_https_the_visitor_cookie_is_a_host_only_secure_cookie(server, monkeypatch):
    monkeypatch.setattr(server, "SESSIONS", True)
    monkeypatch.setattr(server, "ORIGIN", "https://glassvault.luo-zy.com")
    monkeypatch.setattr(server, "VISITOR_COOKIE", "__Host-gv_visitor")
    with TestClient(server.app) as client:
        cookie = client.get("/").headers["set-cookie"]
    assert cookie.startswith("__Host-gv_visitor=") and "Secure" in cookie and "Path=/" in cookie
    assert "Domain" not in cookie and "HttpOnly" in cookie


def test_a_demo_that_grew_too_big_asks_for_a_reset(server, monkeypatch):
    monkeypatch.setattr(server, "MAX_AUDIT", 3)
    with TestClient(server.app) as client:
        codes = [client.post("/api/attacks/forge").status_code for _ in range(6)]
        assert codes[-1] == 429 and client.post("/api/attacks/forge").json()["code"] == "DEMO_FULL"
        client.post("/api/reset")
        assert client.post("/api/attacks/forge").status_code == 200


def test_only_the_newest_audio_is_kept(server):
    server.demo.tts = FakeTts()
    with TestClient(server.app) as client:
        for amount in range(1, 12):
            key = client.post("/api/message", json={"text": f"Send mum {amount} for lunch"}).json()["outcome"]["speech"]
            assert client.get(f"/api/voice/{key}").status_code == 200
    assert len(server.demo.audio) == server.MAX_AUDIO and set(server.demo.audio) <= set(server.demo.scripts)


def test_a_visitors_passkey_handle_survives_reset_but_differs_between_visitors(server, monkeypatch):
    """Setting up Face ID again after Reset replaces the passkey on the visitor's phone (same user
    handle) rather than saving another; two visitors never share a handle."""
    monkeypatch.setattr(server, "SESSIONS", True)
    handle = lambda c: c.post("/api/passkey/register/options", json={}).json()["user"]["id"]  # noqa: E731
    with TestClient(server.app) as a, TestClient(server.app) as b:
        a.get("/")
        b.get("/")
        first = handle(a)
        a.post("/api/reset", json={})
        assert handle(a) == first != handle(b)
        monkeypatch.setattr(server, "_sessions", OrderedDict())  # a redeploy: every demo gone
        assert handle(a) == first  # and the same handle afterwards
