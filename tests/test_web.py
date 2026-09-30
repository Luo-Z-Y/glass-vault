"""The demo server's JSON: what the web page needs to show and speak each outcome."""

import importlib

from fastapi.testclient import TestClient


def test_an_answer_carries_its_lines_and_a_script_for_the_bank_voice(monkeypatch, tmp_path):
    # The offline LLM: no API key in the environment and no .env where the server looks.
    monkeypatch.delenv("GV_LLM_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    server = importlib.import_module("glassvault.web.server")
    monkeypatch.setattr(server, "demo", server.new_demo())

    with TestClient(server.app) as client:
        outcome = client.post("/api/message", json={"text": "What's my balance?", "voice": True}).json()["outcome"]

    assert outcome["kind"] == "answer"
    assert outcome["answer"]["lines"] == outcome["message"].split("\n")
    assert outcome["answer"]["spoken_script"].startswith("Your Savings Account has")
    assert [t["stage"] for t in outcome["trace"]][-1] == "records"
    assert "challenge" not in outcome
