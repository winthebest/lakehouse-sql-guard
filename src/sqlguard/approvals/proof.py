"""Proof of approval: an HMAC over exactly what is being decided, minted when a page is shown.

    proof = "v1.<nonce>.<expiry>.<hex hmac>"
    hmac  = HMAC-SHA256(secret, canonical json [action_id, action_hash, decision,
                                                 approver_label, nonce, expiry])

The decision and the approver label are covered as well as the action, so a proof minted for
"reject" cannot approve, and one minted for one person is useless for another. The secret is
generated when the process starts and never written anywhere, so only this process can mint or
check a proof. `Guard.resolve` additionally spends each accepted proof once (it stores only a
hash of it), so a proof cannot be replayed even within its lifetime.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlguard.safety.guard import ProofRequest

DEFAULT_PROOF_TTL = timedelta(minutes=15)
_DOMAIN = "sqlguard-approval-proof-v1"
_MIN_SECRET_BYTES = 16


class HmacProofs:
    """Mints proofs and is the `ProofVerifier` a `Guard` checks them with."""

    def __init__(
        self,
        secret: bytes | None = None,
        *,
        ttl: timedelta = DEFAULT_PROOF_TTL,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if secret is not None and len(secret) < _MIN_SECRET_BYTES:
            raise ValueError(f"the proof secret must be at least {_MIN_SECRET_BYTES} bytes")
        if ttl <= timedelta(0):
            raise ValueError("the proof lifetime must be positive")
        self._secret = secret if secret is not None else secrets.token_bytes(32)
        self._ttl = ttl
        self._clock = clock or (lambda: datetime.now(UTC))

    def __repr__(self) -> str:  # never show the key, not even in a traceback or a log line
        return "HmacProofs(<secret hidden>)"

    def _mac(self, request: ProofRequest, nonce: str, expiry: int) -> str:
        message = json.dumps(
            [
                _DOMAIN,
                request.action_id,
                request.action_hash,
                request.decision,
                request.approver_label,
                nonce,
                expiry,
            ],
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return hmac.new(self._secret, message.encode("ascii"), hashlib.sha256).hexdigest()

    def mint(self, request: ProofRequest) -> str:
        """A fresh proof for `request`, valid for the configured lifetime."""
        nonce = secrets.token_hex(16)
        expiry = int((self._clock() + self._ttl).timestamp())
        return f"v1.{nonce}.{expiry}.{self._mac(request, nonce, expiry)}"

    def verify(self, request: ProofRequest, proof: str) -> bool:
        """True only for an unexpired proof this process minted for exactly `request`."""
        if not isinstance(proof, str) or len(proof) > 256:
            return False
        parts = proof.split(".")
        if len(parts) != 4 or parts[0] != "v1":
            return False
        _, nonce, raw_expiry, mac = parts
        if not raw_expiry.isascii() or not raw_expiry.isdigit() or not nonce.isalnum():
            return False
        expiry = int(raw_expiry)
        if self._clock().timestamp() >= expiry:
            return False
        return hmac.compare_digest(self._mac(request, nonce, expiry), mac)
