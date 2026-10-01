"""The bank's voice and ears on Tencent RTC (TRTC): text-to-speech and speech recognition.

- ``TrtcTts`` speaks a script with TRTC's TextToSpeech API (FlowTTS). It is a
  Tencent Cloud API 3.0 call, signed with TC3-HMAC-SHA256 using the account's
  SecretId/SecretKey. The API takes at most 255 characters, so a script is split
  at sentence or clause breaks, each part is fetched as 16-bit PCM, and the parts
  are joined into one WAV file.
- ``TrtcAsr`` transcribes one recorded utterance with TRTC's sentence
  recognition (the v3 ``/v3/transcribe`` protocol of the official SDKs). It is
  authenticated with a UserSig made from the SDKAppID's SDKSecretKey, signed
  for a fresh request id each time.

The keys stay on the server: the browser sends audio up and gets audio back.
The web demo asks ``TrtcTts`` only for scripts the control plane rendered (a
confirmation or an enquiry answer), which hold no text from an LLM or from a
third party, and never for text a client supplies. Either service is optional:
without its settings, the web demo uses the browser's own speech.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import re
import time
import uuid
import wave
import zlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

TTS_HOST = "trtc.intl.tencentcloudapi.com"  # international site; trtc.tencentcloudapi.com on the China site
TTS_REGION = "ap-singapore"
TTS_VERSION = "2019-07-22"
TTS_VOICE = "v-female-p9Xy7Q1L"  # "Articulate Narrator": neutral, steady, clear
TTS_MAX_CHARS = 255  # per request; a CJK ideograph counts as 2
TTS_SAMPLE_RATE = 24000

ASR_HOST = "asr-intl.cloud-rtc.com"  # international site; asr.cloud-rtc.com on the China site
ASR_MAX_BYTES = 2 * 1024 * 1024  # the API takes up to 3 MB after base64, and 60 seconds of audio
ASR_SAMPLE_RATE = 16000


class VoiceError(Exception):
    """A TRTC call that gave no audio or no text. `code` is TRTC's error code, or ours."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# --- Signing ------------------------------------------------------------------------


def tc3_headers(
    *,
    secret_id: str,
    secret_key: str,
    host: str,
    action: str,
    version: str,
    region: str,
    payload: bytes,
    timestamp: int,
    content_type: str = "application/json; charset=utf-8",
) -> dict[str, str]:
    """The headers of a Tencent Cloud API 3.0 POST, signed with TC3-HMAC-SHA256.

    `payload` must be the exact bytes sent, and `content_type` the exact header
    sent: both are signed. The credential scope uses the UTC date of `timestamp`.
    """
    service = host.split(".", 1)[0]
    date = datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%d")
    signed_headers = "content-type;host"
    canonical_request = "\n".join([
        "POST", "/", "",
        f"content-type:{content_type}\nhost:{host}\n",
        signed_headers,
        hashlib.sha256(payload).hexdigest(),
    ])
    scope = f"{date}/{service}/tc3_request"
    string_to_sign = "\n".join([
        "TC3-HMAC-SHA256", str(timestamp), scope, hashlib.sha256(canonical_request.encode()).hexdigest(),
    ])

    def _hmac(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    signing_key = _hmac(_hmac(_hmac(("TC3" + secret_key).encode(), date), service), "tc3_request")
    signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    return {
        "Authorization": (f"TC3-HMAC-SHA256 Credential={secret_id}/{scope}, "
                          f"SignedHeaders={signed_headers}, Signature={signature}"),
        "Content-Type": content_type,
        "Host": host,
        "X-TC-Action": action,
        "X-TC-Timestamp": str(timestamp),
        "X-TC-Version": version,
        "X-TC-Region": region,
    }


def user_sig(sdk_app_id: int, sdk_secret_key: str, identifier: str, *, expire: int = 86400,
             now: int | None = None) -> str:
    """A TRTC UserSig (TLSSigAPIv2) for `identifier`, valid for `expire` seconds."""
    issued = int(time.time()) if now is None else now
    content = (f"TLS.identifier:{identifier}\nTLS.sdkappid:{sdk_app_id}\n"
               f"TLS.time:{issued}\nTLS.expire:{expire}\n")
    sig = base64.b64encode(hmac.new(sdk_secret_key.encode(), content.encode(), hashlib.sha256).digest()).decode()
    doc = {"TLS.ver": "2.0", "TLS.identifier": identifier, "TLS.sdkappid": int(sdk_app_id),
           "TLS.expire": int(expire), "TLS.time": issued, "TLS.sig": sig}
    packed = base64.b64encode(zlib.compress(json.dumps(doc).encode())).decode()
    return packed.replace("+", "*").replace("/", "-").replace("=", "_")


# --- Audio helpers ------------------------------------------------------------------


def tts_length(text: str) -> int:
    """Length as TRTC counts it for TextToSpeech: a CJK ideograph is 2, anything else 1."""
    return sum(2 if "一" <= c <= "鿿" else 1 for c in text)


def split_script(text: str, limit: int = TTS_MAX_CHARS) -> list[str]:
    """`text` in parts of at most `limit` (as TRTC counts), broken at sentences, then clauses, then words."""
    text = " ".join(text.split())
    if not text:
        return []
    parts: list[str] = []
    for pattern in (r"(?<=[.!?])\s+", r"(?<=[,;:])\s+", r"\s+"):
        parts = []
        for piece in re.split(pattern, text):
            if parts and tts_length(parts[-1] + " " + piece) <= limit:
                parts[-1] += " " + piece
            else:
                parts.append(piece)
        if all(tts_length(p) <= limit for p in parts):
            return parts
    # A single "word" longer than the limit: cut it.
    out: list[str] = []
    for part in parts:
        while tts_length(part) > limit:
            cut = limit
            while tts_length(part[:cut]) > limit:
                cut -= 1
            out.append(part[:cut])
            part = part[cut:]
        out.append(part)
    return [p for p in out if p]


def pcm_to_wav(pcm: bytes, sample_rate: int, *, channels: int = 1, sample_width: int = 2) -> bytes:
    """Wrap raw little-endian PCM in a WAV header."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(sample_width)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buffer.getvalue()


def check_wav(data: bytes, *, sample_rate: int = ASR_SAMPLE_RATE) -> float:
    """Seconds of audio in `data`, if it is the mono 16-bit PCM WAV the recogniser expects; else VoiceError."""
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            shape = (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getcomptype())
            frames = w.getnframes()
    except Exception as e:  # the wave module raises several types on a malformed file
        raise VoiceError("BAD_AUDIO", f"not a WAV file: {type(e).__name__}") from e
    if shape != (1, 2, sample_rate, "NONE"):
        raise VoiceError("BAD_AUDIO", f"expected mono 16-bit {sample_rate} Hz PCM, got {shape}")
    return frames / sample_rate


# --- Text to speech -----------------------------------------------------------------


@dataclass
class TrtcTts:
    """TRTC TextToSpeech with one fixed voice: the bank's."""

    sdk_app_id: int
    secret_id: str
    secret_key: str
    voice: str = TTS_VOICE
    host: str = TTS_HOST
    region: str = TTS_REGION
    model: str | None = None  # None: the API's default model
    language: str = "en"
    timeout: float = 10.0  # per request
    deadline: float = 20.0  # for the whole script, so a slow TRTC fails before the page gives up
    http: Any = None  # an httpx.Client; tests pass one with a mock transport

    @property
    def name(self) -> str:
        return f"TRTC FlowTTS · {self.voice}"

    def synthesize(self, script: str) -> bytes:
        """The script spoken in the bank's voice, as one WAV file (mono, 16-bit, 24 kHz)."""
        parts = split_script(script)
        if not parts:
            raise VoiceError("EMPTY_TEXT", "nothing to say")
        give_up = time.monotonic() + self.deadline
        pcm = []
        for part in parts:
            remaining = give_up - time.monotonic()
            if remaining <= 0:
                raise VoiceError("TIMEOUT", f"no audio within {self.deadline:.0f} seconds")
            pcm.append(self._pcm(part, min(self.timeout, remaining)))
        return pcm_to_wav(b"".join(pcm), TTS_SAMPLE_RATE)

    def _pcm(self, text: str, timeout: float) -> bytes:
        body: dict[str, Any] = {
            "Text": text,
            "Voice": {"VoiceId": self.voice, "Speed": 1.0},
            "SdkAppId": self.sdk_app_id,
            "AudioFormat": {"Format": "pcm", "SampleRate": TTS_SAMPLE_RATE},
            "Language": self.language,
        }
        if self.model:
            body["Model"] = self.model
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
        headers = tc3_headers(secret_id=self.secret_id, secret_key=self.secret_key, host=self.host,
                              action="TextToSpeech", version=TTS_VERSION, region=self.region,
                              payload=payload, timestamp=int(time.time()))
        data = _post_json(self.http, f"https://{self.host}/", payload, headers, timeout)
        response = data.get("Response") if isinstance(data, dict) else None
        if not isinstance(response, dict):
            raise VoiceError("BAD_RESPONSE", "no Response object")
        if "Error" in response:
            error = response["Error"] or {}
            raise VoiceError(str(error.get("Code", "Error")), str(error.get("Message", ""))[:200])
        try:
            return base64.b64decode(response["Audio"], validate=True)
        except (KeyError, TypeError, ValueError) as e:
            raise VoiceError("BAD_RESPONSE", "no audio in the response") from e


# --- Speech recognition -------------------------------------------------------------


@dataclass
class TrtcAsr:
    """TRTC sentence recognition for one short utterance (up to 60 seconds)."""

    sdk_app_id: int
    sdk_secret_key: str
    host: str = ASR_HOST
    language: str = "en"  # "" lets the model detect the language
    engine: str = "bigmodel"
    timeout: float = 20.0
    http: Any = None

    @property
    def name(self) -> str:
        return f"TRTC ASR · {self.engine}" + (f" ({self.language})" if self.language else "")

    def transcribe(self, wav: bytes) -> str:
        """The words in a mono 16-bit 16 kHz WAV recording."""
        if len(wav) > ASR_MAX_BYTES:
            raise VoiceError("AUDIO_TOO_LONG", f"recording over {ASR_MAX_BYTES} bytes")
        if check_wav(wav) > 60:
            raise VoiceError("AUDIO_TOO_LONG", "recording over 60 seconds")
        request_id = str(uuid.uuid4())
        params: dict[str, Any] = {
            "engine_model_type": self.engine,
            "source_type": 1,
            "voice_format": "wav",
            "data": base64.b64encode(wav).decode(),
            "data_len": len(wav),
        }
        if self.language:
            params["language"] = self.language
        body = {
            "auth": {"sdkappid": str(self.sdk_app_id), "request_id": request_id,
                     "usersig": user_sig(self.sdk_app_id, self.sdk_secret_key, request_id)},
            "params": params,
        }
        payload = json.dumps(body, separators=(",", ":")).encode()
        headers = {"Content-Type": "application/json; charset=utf-8"}
        data = _post_json(self.http, f"https://{self.host}/v3/transcribe", payload, headers, self.timeout,
                          error_statuses_have_json=True)
        if not isinstance(data, dict):
            raise VoiceError("BAD_RESPONSE", "not a JSON object")
        if data.get("code") != 0:  # an authentication failure arrives as HTTP 200 with a code
            raise VoiceError(str(data.get("code", "Error")), str(data.get("message", ""))[:200])
        result = data.get("result")
        if not isinstance(result, str):
            raise VoiceError("BAD_RESPONSE", "no text in the response")
        return result.strip()


def _post_json(http: Any, url: str, payload: bytes, headers: Mapping[str, str], timeout: float,
               *, error_statuses_have_json: bool = False) -> Any:
    """POST `payload` and return the JSON reply; network and HTTP failures become VoiceError."""
    try:
        if http is not None:
            response = http.post(url, content=payload, headers=dict(headers), timeout=timeout)
        else:
            with httpx.Client(timeout=timeout) as client:
                response = client.post(url, content=payload, headers=dict(headers))
    except httpx.HTTPError as e:
        raise VoiceError("UNREACHABLE", f"{type(e).__name__}: {e}"[:200]) from e
    if response.status_code >= 400 and not error_statuses_have_json:
        raise VoiceError(f"HTTP_{response.status_code}", response.text[:200])
    try:
        return response.json()
    except ValueError as e:
        raise VoiceError(f"HTTP_{response.status_code}", "reply is not JSON") from e


# --- Configuration ------------------------------------------------------------------


def voice_from_env(environ: Mapping[str, str] | None = None) -> tuple[TrtcTts | None, TrtcAsr | None]:
    """The TRTC voice services the settings allow, from GV_* variables or .env; None for each one not set up.

    Text to speech needs GV_TRTC_SDK_APP_ID, GV_TENCENT_SECRET_ID and
    GV_TENCENT_SECRET_KEY. Speech recognition needs GV_TRTC_SDK_APP_ID and
    GV_TRTC_SDK_SECRET_KEY. Optional: GV_TRTC_VOICE, GV_TRTC_TTS_MODEL,
    GV_TRTC_TTS_HOST, GV_TRTC_TTS_REGION, GV_TRTC_ASR_HOST, GV_TRTC_ASR_LANGUAGE.
    """
    if environ is None:
        from .llm import _env_with_dotenv

        environ = _env_with_dotenv()

    def get(key: str) -> str:
        return (environ.get(key) or "").strip()

    app_id = get("GV_TRTC_SDK_APP_ID")
    if not re.fullmatch(r"[0-9]+", app_id):
        return None, None
    tts = asr = None
    if get("GV_TENCENT_SECRET_ID") and get("GV_TENCENT_SECRET_KEY"):
        tts = TrtcTts(int(app_id), get("GV_TENCENT_SECRET_ID"), get("GV_TENCENT_SECRET_KEY"),
                      voice=get("GV_TRTC_VOICE") or TTS_VOICE, host=get("GV_TRTC_TTS_HOST") or TTS_HOST,
                      region=get("GV_TRTC_TTS_REGION") or TTS_REGION, model=get("GV_TRTC_TTS_MODEL") or None)
    if get("GV_TRTC_SDK_SECRET_KEY"):
        language = environ.get("GV_TRTC_ASR_LANGUAGE")
        asr = TrtcAsr(int(app_id), get("GV_TRTC_SDK_SECRET_KEY"), host=get("GV_TRTC_ASR_HOST") or ASR_HOST,
                      language="en" if language is None else language.strip())
    return tts, asr
