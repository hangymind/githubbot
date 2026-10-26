import hashlib
import hmac

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def qq_key(secret: str):
    seed = secret.encode()
    if not seed:
        raise ValueError("Empty AppSecret")
    return Ed25519PrivateKey.from_private_bytes((seed * (32 // len(seed) + 1))[:32])


def github_signature(secret: str, body: bytes, signature: str):
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def qq_signature(secret: str, body: bytes, timestamp: str, signature: str):
    if not timestamp or len(timestamp) > 32 or not timestamp.isdigit():
        return False
    try:
        qq_key(secret).public_key().verify(bytes.fromhex(signature), timestamp.encode() + body)
        return True
    except (ValueError, InvalidSignature):
        return False
