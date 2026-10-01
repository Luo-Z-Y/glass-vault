"""TRTC voice: request signing, UserSig, script splitting, and both clients against a mocked TRTC."""

import base64
import hashlib
import hmac
import importlib
import io
import json
import struct
import uuid
import wave
import zlib

import httpx
import pytest
from fastapi.testclient import TestClient

from glassvault.voice import (
    TTS_SAMPLE_RATE,
    TrtcAsr,
    TrtcTts,
    VoiceError,
    check_wav,
    pcm_to_wav,
    split_script,
    tc3_headers,
    tts_length,
    user_sig,
    voice_from_env,
)

# --- Signing: pinned to Tencent's own code ------------------------------------------------------


def test_tc3_signature_matches_the_official_sdk():
    # Expected value from tencentcloud-sdk-python's Sign.sign_tc3 over the same canonical request.
    headers = tc3_headers(
        secret_id="AKIDexampleexampleexample", secret_key="exampleSecretKeyexampleSecretKey",
        host="trtc.intl.tencentcloudapi.com", action="TextToSpeech", version="2019-07-22",
        region="ap-singapore", timestamp=1551113065,
        payload=b'{"Text":"hello","Voice":{"VoiceId":"v-female-p9Xy7Q1L"},"SdkAppId":20000000}',
    )
    assert headers["Authorization"] == (
        "TC3-HMAC-SHA256 Credential=AKIDexampleexampleexample/2019-02-25/trtc/tc3_request, "
        "SignedHeaders=content-type;host, "
        "Signature=04e9ab4760f99b0800f6a47208ee34bc8f8189ffb1a32b3e78cf6c64e7d8dc16")
    assert (headers["Host"], headers["X-TC-Action"], headers["X-TC-Version"], headers["X-TC-Region"],
            headers["X-TC-Timestamp"]) == ("trtc.intl.tencentcloudapi.com", "TextToSpeech", "2019-07-22",
                                           "ap-singapore", "1551113065")
    assert headers["Content-Type"] == "application/json; charset=utf-8"


def test_tc3_scope_uses_the_utc_date():
    # 23:59:59 UTC on 2019-02-24 is already 2019-02-25 in Singapore; the scope must say the 24th.
    headers = tc3_headers(secret_id="id", secret_key="key", host="trtc.intl.tencentcloudapi.com",
                          action="TextToSpeech", version="2019-07-22", region="ap-singapore",
                          payload=b"{}", timestamp=1551052799)
    assert "/2019-02-24/trtc/tc3_request" in headers["Authorization"]


def test_user_sig_matches_the_official_library():
    # Expected value from tls-sig-api-v2 (TLSSigAPIv2.gen_sig) with time fixed at 1700000000.
    key = "5bd2850fff3ecb11d7c805251c51ee463a25727bddc2385f3fa8bfee1bb93b5e"
    assert user_sig(1400000000, key, "request-123", expire=86400, now=1700000000) == (
        "eJyrVgrxCdYrSy1SslJQMtIzUNJRAItkpqTmlWSmZUIkilILS1OLS3QNjYxhCopTshMLCjJTgNKGJgZQAJVLrSjILEoFyliYmcAFSzJzQUKG5"
        "miKizPTQVa4Bhh7B-p6hiUGFjm5mbvpBwcEeiR7Zhj6BCUGFZYmejvp5-t4hRmZ5YZZ2CrVAgAftzKd")


def decode_sig(sig: str) -> dict:
    return json.loads(zlib.decompress(base64.b64decode(sig.replace("*", "+").replace("-", "/").replace("_", "="))))


# --- Splitting and audio ---------------------------------------------------------------------


def test_a_script_is_split_at_sentences_within_the_limit():
    script = " ".join(f"Sentence number {i} is about paying a bill to SP Group." for i in range(12))
    parts = split_script(script)
    assert len(parts) > 1 and all(tts_length(p) <= 255 for p in parts)
    assert " ".join(parts) == script and all(p.endswith(".") for p in parts)


def test_splitting_falls_back_to_clauses_words_and_cuts():
    clauses = ", ".join(["one thousand dollars"] * 30)
    assert all(tts_length(p) <= 255 for p in split_script(clauses))
    assert all(len(p) <= 40 for p in split_script("x" * 100, limit=40)) and "".join(split_script("x" * 100, 40)) == "x" * 100
    assert split_script("   ") == []


def test_cjk_counts_twice():
    assert tts_length("你好") == 4 and tts_length("Hello") == 5
    assert all(tts_length(p) <= 255 for p in split_script("你好。" * 120))


def test_pcm_round_trips_through_wav():
    pcm = struct.pack("<4h", 0, 1000, -1000, 32767)
    wav = pcm_to_wav(pcm, 16000)
    assert check_wav(wav) == 4 / 16000
    with wave.open(io.BytesIO(wav)) as w:
        assert w.readframes(4) == pcm


def _corrupt_fmt_size(wav: bytes) -> bytes:
    return wav[:16] + struct.pack("<I", 0xF8) + wav[20:]  # a 'fmt ' chunk that claims 248 bytes


@pytest.mark.parametrize("wav", [
    pcm_to_wav(b"\0\0" * 10, 8000),
    pcm_to_wav(b"\0\0\0\0" * 10, 16000, channels=2),
    b"RIFF....not a wav",
    _corrupt_fmt_size(pcm_to_wav(b"\1\0" * 100, 16000)),
    pcm_to_wav(b"\1\0" * 100, 16000)[:30],
])
def test_only_mono_16_bit_16_khz_wav_is_accepted(wav):
    with pytest.raises(VoiceError) as e:
        check_wav(wav)
    assert e.value.code == "BAD_AUDIO"


# --- Text to speech against a mocked TRTC --------------------------------------------------


def tts_with(handler) -> tuple[TrtcTts, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    http = httpx.Client(transport=httpx.MockTransport(record))
    return TrtcTts(1400000000, "AKIDtest", "secret", http=http), seen


def audio_reply(request: httpx.Request) -> httpx.Response:
    text = json.loads(request.content)["Text"]
    pcm = struct.pack("<h", len(text)) * 4  # four samples per part, valued by the part's length
    return httpx.Response(200, json={"Response": {"Audio": base64.b64encode(pcm).decode(), "RequestId": "r"}})


def test_tts_signs_each_part_and_joins_the_audio_into_one_wav():
    tts, seen = tts_with(audio_reply)
    script = " ".join(f"Step {i}: send fifty dollars to John Tan Wei Jie from your Savings Account." for i in range(8))
    wav = tts.synthesize(script)

    parts = split_script(script)
    assert len(seen) == len(parts) > 1
    for request, part in zip(seen, parts, strict=True):
        assert str(request.url) == "https://trtc.intl.tencentcloudapi.com/"
        assert request.headers["X-TC-Action"] == "TextToSpeech" and request.headers["X-TC-Region"] == "ap-singapore"
        assert request.headers["Authorization"].startswith("TC3-HMAC-SHA256 Credential=AKIDtest/")
        assert request.headers["Content-Type"] == "application/json; charset=utf-8"
        body = json.loads(request.content)
        assert body == {"Text": part, "Voice": {"VoiceId": "v-female-p9Xy7Q1L", "Speed": 1.0},
                        "SdkAppId": 1400000000, "AudioFormat": {"Format": "pcm", "SampleRate": 24000},
                        "Language": "en"}
    with wave.open(io.BytesIO(wav)) as w:
        assert w.getframerate() == TTS_SAMPLE_RATE and w.getnchannels() == 1 and w.getsampwidth() == 2
        assert w.readframes(w.getnframes()) == b"".join(struct.pack("<h", len(p)) * 4 for p in parts)


@pytest.mark.parametrize("reply, code", [
    (httpx.Response(200, json={"Response": {"Error": {"Code": "AuthFailure.SignatureFailure", "Message": "no"}}}),
     "AuthFailure.SignatureFailure"),
    (httpx.Response(200, json={"Response": {"RequestId": "r"}}), "BAD_RESPONSE"),
    (httpx.Response(200, json={"Response": {"Audio": "not base64!!"}}), "BAD_RESPONSE"),
    (httpx.Response(502, text="bad gateway"), "HTTP_502"),
])
def test_tts_failures_become_voice_errors(reply, code):
    tts, _ = tts_with(lambda request: reply)
    with pytest.raises(VoiceError) as e:
        tts.synthesize("Hello.")
    assert e.value.code == code


def test_a_slow_trtc_gives_up_within_the_deadline(monkeypatch):
    import glassvault.voice as voice_module

    clock = iter([0.0, 0.0, 25.0])  # the second part would start after the 20-second deadline
    monkeypatch.setattr(voice_module.time, "monotonic", lambda: next(clock))
    tts, seen = tts_with(audio_reply)
    with pytest.raises(VoiceError) as e:
        tts.synthesize("First sentence. " + "word " * 60 + "Second sentence.")
    assert e.value.code == "TIMEOUT" and len(seen) == 1


def test_an_unreachable_tts_is_a_voice_error():
    def down(request):
        raise httpx.ConnectError("no route")

    tts, _ = tts_with(down)
    with pytest.raises(VoiceError) as e:
        tts.synthesize("Hello.")
    assert e.value.code == "UNREACHABLE"


# --- Speech recognition against a mocked TRTC ----------------------------------------------


def asr_with(handler, **kwargs) -> tuple[TrtcAsr, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request):
        seen.append(request)
        return handler(request)

    return TrtcAsr(1400000000, "sdk-secret", http=httpx.Client(transport=httpx.MockTransport(record)), **kwargs), seen


SPEECH = pcm_to_wav(b"\x01\x00" * 16000, 16000)  # one second


def test_asr_sends_the_recording_with_a_user_sig_for_this_request():
    asr, seen = asr_with(lambda r: httpx.Response(200, json={"code": 0, "message": "success", "result": " Send mum 300. "}))
    assert asr.transcribe(SPEECH) == "Send mum 300."
    [request] = seen
    assert str(request.url) == "https://asr-intl.cloud-rtc.com/v3/transcribe"
    body = json.loads(request.content)
    auth, params = body["auth"], body["params"]
    assert auth["sdkappid"] == "1400000000" and uuid.UUID(auth["request_id"])
    assert params == {"engine_model_type": "bigmodel", "source_type": 1, "voice_format": "wav",
                      "data": base64.b64encode(SPEECH).decode(), "data_len": len(SPEECH), "language": "en"}
    doc = decode_sig(auth["usersig"])
    assert doc["TLS.identifier"] == auth["request_id"] and doc["TLS.sdkappid"] == 1400000000
    content = (f"TLS.identifier:{doc['TLS.identifier']}\nTLS.sdkappid:1400000000\n"
               f"TLS.time:{doc['TLS.time']}\nTLS.expire:{doc['TLS.expire']}\n")
    expected = base64.b64encode(hmac.new(b"sdk-secret", content.encode(), hashlib.sha256).digest()).decode()
    assert doc["TLS.sig"] == expected


def test_asr_language_can_be_left_to_detection():
    asr, seen = asr_with(lambda r: httpx.Response(200, json={"code": 0, "result": "hello"}), language="")
    asr.transcribe(SPEECH)
    assert "language" not in json.loads(seen[0].content)["params"]


@pytest.mark.parametrize("reply, code", [
    (httpx.Response(200, json={"code": 4002, "message": "auth failed"}), "4002"),  # auth failures come as HTTP 200
    (httpx.Response(429, json={"code": 4006, "message": "too many"}), "4006"),
    (httpx.Response(200, json={"code": 0}), "BAD_RESPONSE"),
])
def test_asr_failures_become_voice_errors(reply, code):
    asr, _ = asr_with(lambda r: reply)
    with pytest.raises(VoiceError) as e:
        asr.transcribe(SPEECH)
    assert e.value.code == code


def test_asr_refuses_long_or_malformed_audio_before_calling_trtc():
    asr, seen = asr_with(lambda r: httpx.Response(200, json={"code": 0, "result": "x"}))
    for audio, code in ((pcm_to_wav(b"\0\0" * 16000 * 61, 16000), "AUDIO_TOO_LONG"), (b"junk", "BAD_AUDIO")):
        with pytest.raises(VoiceError) as e:
            asr.transcribe(audio)
        assert e.value.code == code
    assert seen == []


# --- Configuration --------------------------------------------------------------------------


def test_voice_services_need_their_own_keys():
    assert voice_from_env({}) == (None, None)
    assert voice_from_env({"GV_TRTC_SDK_APP_ID": "not-a-number", "GV_TRTC_SDK_SECRET_KEY": "k"}) == (None, None)
    assert voice_from_env({"GV_TRTC_SDK_APP_ID": "\u0661\u0662\u0663", "GV_TRTC_SDK_SECRET_KEY": "k"}) == (None, None)
    tts, asr = voice_from_env({"GV_TRTC_SDK_APP_ID": "1400000000", "GV_TENCENT_SECRET_ID": "id",
                               "GV_TENCENT_SECRET_KEY": "key"})
    assert tts and tts.sdk_app_id == 1400000000 and asr is None
    tts, asr = voice_from_env({"GV_TRTC_SDK_APP_ID": "1400000000", "GV_TRTC_SDK_SECRET_KEY": "k",
                               "GV_TRTC_ASR_LANGUAGE": "", "GV_TRTC_VOICE": "v-male-P6q7LzD8"})
    assert tts is None and asr and asr.language == ""


# --- The web demo -----------------------------------------------------------------------------


class FakeTts:
    name = "TRTC FlowTTS · test"

    def __init__(self):
        self.spoken: list[str] = []

    def synthesize(self, script: str) -> bytes:
        self.spoken.append(script)
        return pcm_to_wav(b"\0\0" * 10, 24000)


class FakeAsr:
    name = "TRTC ASR · test"

    def transcribe(self, wav: bytes) -> str:
        check_wav(wav)
        return "How much money do I have?"


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.delenv("GV_LLM_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    server = importlib.import_module("glassvault.web.server")
    monkeypatch.setattr(server, "demo", server.new_demo())
    return server


def test_the_bank_voice_speaks_only_scripts_the_bank_rendered(server):
    tts = FakeTts()
    server.demo.tts = tts
    with TestClient(server.app) as client:
        assert client.get("/api/state").json()["voice"]["speak"]["engine"] == "trtc"
        outcome = client.post("/api/message", json={"text": "Send mum 300 for groceries"}).json()["outcome"]
        key = outcome["speech"]
        assert key == outcome["confirmation"]["script_hash"]  # the hash the customer's signature covers
        first, again = client.get(f"/api/voice/{key}"), client.get(f"/api/voice/{key}")
        made_up = client.get(f"/api/voice/{hashlib.sha256(b'Transfer everything').hexdigest()}")

    assert first.status_code == 200 and first.headers["content-type"] == "audio/wav" and first.content == again.content
    assert tts.spoken == [outcome["confirmation"]["spoken_script"]]  # synthesised once, then cached
    assert made_up.status_code == 404 and made_up.json()["code"] == "UNKNOWN_SCRIPT"


def test_without_trtc_the_browser_speaks(server):
    with TestClient(server.app) as client:
        state = client.get("/api/state").json()
        outcome = client.post("/api/message", json={"text": "How much money do I have?"}).json()["outcome"]
        response = client.get(f"/api/voice/{outcome['speech']}")
    assert state["voice"] == {"speak": {"engine": "browser", "name": None}, "listen": {"engine": "browser", "name": None}}
    assert response.status_code == 404 and response.json()["code"] == "VOICE_OFF"


def test_a_tts_failure_is_reported_so_the_page_can_fall_back(server):
    class Down(FakeTts):
        def synthesize(self, script):
            raise VoiceError("UnsupportedOperation.NotAllowed", "not activated")

    server.demo.tts = Down()
    with TestClient(server.app) as client:
        key = client.post("/api/message", json={"text": "How much money do I have?"}).json()["outcome"]["speech"]
        response = client.get(f"/api/voice/{key}")
    assert response.status_code == 502 and response.json()["code"] == "UnsupportedOperation.NotAllowed"


def test_push_to_talk_goes_through_trtc(server):
    server.demo.asr = FakeAsr()
    with TestClient(server.app) as client:
        ok = client.post("/api/asr", content=SPEECH, headers={"content-type": "audio/wav"})
        bad = client.post("/api/asr", content=b"not audio", headers={"content-type": "audio/wav"})
        huge = client.post("/api/asr", content=b"\0" * (2 * 1024 * 1024 + 1), headers={"content-type": "audio/wav"})
    assert ok.json() == {"text": "How much money do I have?"}
    assert bad.status_code == 400 and bad.json()["code"] == "BAD_AUDIO"
    assert huge.status_code == 413


def test_push_to_talk_without_trtc_is_refused(server):
    with TestClient(server.app) as client:
        response = client.post("/api/asr", content=SPEECH)
    assert response.status_code == 404 and response.json()["code"] == "ASR_OFF"


def test_another_site_cannot_spend_the_trtc_credit(server):
    server.demo.tts, server.demo.asr = FakeTts(), FakeAsr()
    with TestClient(server.app) as client:
        key = client.post("/api/message", json={"text": "How much money do I have?"}).json()["outcome"]["speech"]
        cross = {"sec-fetch-site": "cross-site"}
        voice = client.get(f"/api/voice/{key}", headers=cross)
        asr = client.post("/api/asr", content=SPEECH, headers={"content-type": "audio/wav", **cross})
        other_origin = client.post("/api/asr", content=SPEECH,
                                   headers={"content-type": "audio/wav", "origin": "https://evil.example"})
        plain = client.post("/api/asr", content=SPEECH, headers={"content-type": "text/plain"})
        same = client.post("/api/asr", content=SPEECH, headers={"content-type": "audio/wav",
                                                                 "sec-fetch-site": "same-origin",
                                                                 "origin": "http://testserver"})
    assert [r.status_code for r in (voice, asr, other_origin)] == [403, 403, 403]
    assert plain.status_code == 415  # a type no form or no-cors fetch can send
    assert same.status_code == 200


def test_listen_again_is_free_while_the_script_is_remembered(server, monkeypatch):
    tts = FakeTts()
    server.demo.tts = tts
    monkeypatch.setattr(server, "MAX_SCRIPTS", 2)
    with TestClient(server.app) as client:
        keys = []
        for text in ("How much money do I have?", "show me my recent transactions", "what's the balance in my joint account"):
            keys.append(client.post("/api/message", json={"text": text}).json()["outcome"]["speech"])
            client.get(f"/api/voice/{keys[-1]}")
            client.get(f"/api/voice/{keys[-1]}")
        oldest = client.get(f"/api/voice/{keys[0]}")
    assert len(tts.spoken) == 3  # each script synthesised once, however often it is played
    assert oldest.status_code == 404  # forgotten with its audio once newer scripts push it out
    assert set(server.demo.audio) <= set(server.demo.scripts) and len(server.demo.scripts) == 2


def test_the_configured_public_origin_is_this_site_even_behind_a_proxy(server, monkeypatch):
    server.demo.asr = FakeAsr()
    monkeypatch.setattr(server, "ORIGIN", "https://vault.example.com")
    with TestClient(server.app) as client:  # the proxy rewrote Host to the internal address
        behind_proxy = client.post("/api/asr", content=SPEECH, headers={
            "content-type": "audio/wav", "origin": "https://vault.example.com", "host": "127.0.0.1:8000"})
    assert behind_proxy.status_code == 200


def test_a_failed_synthesis_keeps_one_lock_per_script(server):
    class FailsOnce(FakeTts):
        def synthesize(self, script):
            if not self.spoken:
                self.spoken.append("failed")
                raise VoiceError("InternalError", "try again")
            return super().synthesize(script)

    server.demo.tts = FailsOnce()
    with TestClient(server.app) as client:
        key = client.post("/api/message", json={"text": "How much money do I have?"}).json()["outcome"]["speech"]
        failed, retried = client.get(f"/api/voice/{key}"), client.get(f"/api/voice/{key}")
    assert (failed.status_code, retried.status_code) == (502, 200)
    assert list(server.demo.synthesising) == [key]  # the same lock serves every later request
