"""Step-up authentication: a second factor bound to one approval.

Models a bank "digital token" doing transaction signing. The token derives a
6-digit code from the challenge nonce using a secret shared only with the
gateway (HMAC-SHA256, truncated like OATH OCRA), so a code is valid for
exactly one approval and the agent zone, which never holds the secret, cannot
produce one. The gateway asks for it when its own risk rules say "step_up".
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

DIGITS = 6
MAX_ATTEMPTS = 3


class DigitalToken:
    """The customer's token device (simulated in the demo)."""

    def __init__(self, secret: bytes | None = None):
        self._secret = secret or secrets.token_bytes(32)

    def code(self, nonce: str) -> str:
        """The 6-digit code for the approval with this nonce: what the token's screen would show."""
        mac = hmac.new(self._secret, b"glassvault-step-up:" + nonce.encode(), hashlib.sha256).digest()
        return str(int.from_bytes(mac[:8], "big") % 10**DIGITS).zfill(DIGITS)


class StepUpVerifier:
    """The gateway's side: knows each customer's token secret, checks codes, counts failures."""

    def __init__(self) -> None:
        self._tokens: dict[str, DigitalToken] = {}
        self._failures: dict[str, int] = {}

    def enrol(self, customer_id: str, token: DigitalToken) -> None:
        """Pair a customer with their token. A real bank does this at onboarding, out of the agent's reach."""
        self._tokens[customer_id] = token

    def verify(self, customer_id: str, nonce: str, code: object) -> bool:
        """True if `code` is this customer's code for this nonce, compared in constant time."""
        token = self._tokens.get(customer_id)
        if token is None or not isinstance(code, str):
            return False
        return hmac.compare_digest(token.code(nonce), code.strip())

    def record_failure(self, nonce: str) -> int:
        """Returns how many wrong codes this approval has seen."""
        self._failures[nonce] = self._failures.get(nonce, 0) + 1
        return self._failures[nonce]
