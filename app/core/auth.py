# app/core/auth.py
import json
import hmac
import hashlib
import base64
import time
from typing import Optional

from app.core.config import settings

# Session lifetime (1 week)
SESSION_EXP_SECONDS = 7 * 24 * 60 * 60

def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")

def _b64decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)

def create_session_token(guid: str) -> str:
    payload = {"sub": guid, "exp": int(time.time()) + SESSION_EXP_SECONDS}
    payload_json = json.dumps(payload, separators=(",", ":")).encode()
    payload_b64 = _b64encode(payload_json)
    signature = hmac.new(
        settings.SECRET_KEY.encode(), payload_b64.encode(), hashlib.sha256
    ).digest()
    signature_b64 = _b64encode(signature)
    return f"{payload_b64}.{signature_b64}"

def decode_session_token(token: str) -> Optional[str]:
    try:
        payload_b64, signature_b64 = token.split(".", 1)
        expected_sig = hmac.new(
            settings.SECRET_KEY.encode(), payload_b64.encode(), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(expected_sig, _b64decode(signature_b64)):
            return None
        payload = json.loads(_b64decode(payload_b64))
        if payload.get("exp", 0) < time.time():
            return None
        return payload.get("sub")
    except Exception:
        return None
