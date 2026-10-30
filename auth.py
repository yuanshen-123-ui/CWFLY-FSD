"""Authentication module - wraps CertificateStore for login operations."""

from typing import Optional
from certificate import CertificateStore
from protocol import LEV_OBSPILOT


def check_login(certs: CertificateStore, cid: str, password: str
                ) -> tuple[bool, int]:
    """Authenticate with plaintext or pre-hashed password.

    Handles both legacy plaintext and SHA-256 hex passwords.
    Returns (success, max_level).
    """
    return certs.max_level(cid, password)


def check_login_challenge(certs: CertificateStore, cid: str,
                          response: str, challenge: str) -> tuple[bool, int]:
    """Authenticate using HMAC-SHA256 challenge-response.

    Returns (success, max_level).
    """
    return certs.max_level_challenge(cid, response, challenge)
