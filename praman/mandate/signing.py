"""Ed25519 signing over a canonical mandate serialization.

What this buys, precisely: a mandate cannot be altered between issue and
adjudication without the change being detectable. If a compromised agent widens
its own cap from ₹2,000 to ₹10,000, verification fails and the gate refuses to
adjudicate at all -- it does not fall back to "well, the bounds look fine".

What this does not buy, and we say so rather than implying otherwise:

  * It is not a W3C Verifiable Credential. There is no DID resolution, no proof
    suite, no revocation registry. Revocation here is an event in the log
    (``mandate_revoked``), which is sufficient for a single-issuer system and
    honest about being so.
  * It does not prove the *human* consented -- only that the issuer's key signed
    this object. Binding to a real person is what NPCI's UAP and UPI Circle
    delegation are for, and Praman is designed to sit downstream of that, not to
    reimplement it.

Canonicalization is the same ``_canonical`` the event log uses: sorted keys, no
whitespace, Decimals as strings. Two serializations of the same mandate must
produce the same bytes, or the signature is a coin flip.
"""
from __future__ import annotations

import base64

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (Ed25519PrivateKey,
                                                               Ed25519PublicKey)

from ..ledger.eventlog import _canonical
from .schema import Mandate

PREFIX = "ed25519:"


class SigningError(Exception):
    pass


def generate_keypair() -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    sk = Ed25519PrivateKey.generate()
    return sk, sk.public_key()


def private_key_to_pem(sk: Ed25519PrivateKey) -> bytes:
    return sk.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def public_key_to_b64(pk: Ed25519PublicKey) -> str:
    raw = pk.public_bytes(encoding=serialization.Encoding.Raw,
                          format=serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def public_key_from_b64(s: str) -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(base64.b64decode(s))


def load_private_key(pem: bytes) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(pem, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise SigningError("mandate keys must be Ed25519")
    return key


def canonical_bytes(mandate: Mandate) -> bytes:
    return _canonical(mandate.signing_payload()).encode("utf-8")


def sign_mandate(mandate: Mandate, sk: Ed25519PrivateKey) -> Mandate:
    sig = sk.sign(canonical_bytes(mandate))
    return mandate.model_copy(
        update={"signature": PREFIX + base64.b64encode(sig).decode()})


def verify_mandate(mandate: Mandate, pk: Ed25519PublicKey) -> bool:
    """True only if the signature is present, well-formed, and valid.

    Returns a bool rather than raising because the gate's answer to an
    unverifiable mandate is a decision (BLOCK, cited), not an exception.
    """
    if not mandate.signature or not mandate.signature.startswith(PREFIX):
        return False
    try:
        sig = base64.b64decode(mandate.signature[len(PREFIX):])
    except Exception:
        return False
    try:
        pk.verify(sig, canonical_bytes(mandate))
        return True
    except InvalidSignature:
        return False


class MandateIssuer:
    """Holds the signing key. In production this is an HSM or a KMS handle.

    Keeping it behind a class rather than passing raw keys around means the
    swap to a real key custodian is a constructor change, not a refactor.
    """

    def __init__(self, private_key: Ed25519PrivateKey | None = None):
        self._sk = private_key or Ed25519PrivateKey.generate()

    @property
    def public_key_b64(self) -> str:
        return public_key_to_b64(self._sk.public_key())

    def issue(self, mandate: Mandate) -> Mandate:
        return sign_mandate(mandate, self._sk)

    def verify(self, mandate: Mandate) -> bool:
        return verify_mandate(mandate, self._sk.public_key())
