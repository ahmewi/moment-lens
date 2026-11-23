"""E-mail one-time-passcode (OTP) authentication service."""
from __future__ import annotations

import hashlib
import logging
import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

from jose import jwt

from app.config import settings

log = logging.getLogger("momentlens.otp")

_otp_store: dict[str, dict] = {}
_rate_store: dict[str, list[datetime]] = {}
_lock = threading.Lock()


def _hash_otp(code: str, email: str) -> str:
    return hashlib.sha256(f"{email}:{code}".encode("utf-8")).hexdigest()


def _cleanup(now: datetime) -> None:
    for key in [k for k, v in _otp_store.items() if v["expires_at"] <= now]:
        del _otp_store[key]
    for key, entries in list(_rate_store.items()):
        fresh = [t for t in entries if (now - t).total_seconds() < 3600]
        if fresh:
            _rate_store[key] = fresh
        else:
            del _rate_store[key]


def check_rate_limit(key: str, limit: int, window_seconds: int) -> bool:
    now = datetime.now(timezone.utc)
    with _lock:
        _cleanup(now)
        entries = [t for t in _rate_store.get(key, []) if (now - t).total_seconds() < window_seconds]
        if len(entries) >= limit:
            _rate_store[key] = entries
            return False
        entries.append(now)
        _rate_store[key] = entries
        return True


def validate_email_domain(email: str) -> bool:
    if not email or "@" not in email:
        return False
    domain = email.rsplit("@", 1)[1].lower().strip()
    return domain in settings.allowed_domains_list


def generate_otp() -> str:
    upper = 10 ** settings.otp_length
    return str(secrets.randbelow(upper)).zfill(settings.otp_length)


async def store_otp_memory(email: str, code: str) -> None:
    email_lower = email.lower()
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=settings.otp_expiry_seconds)
    with _lock:
        _cleanup(datetime.now(timezone.utc))
        _otp_store[email_lower] = {
            "code_hash": _hash_otp(code, email_lower),
            "attempts": 0,
            "expires_at": expires_at,
        }
    log.info("OTP stored for %s", email_lower)


async def verify_otp_memory(email: str, code: str) -> bool:
    email_lower = email.lower()
    with _lock:
        _cleanup(datetime.now(timezone.utc))
        entry = _otp_store.get(email_lower)
        if not entry:
            return False
        if entry.get("attempts", 0) >= 5:
            del _otp_store[email_lower]
            return False
        if entry["expires_at"] <= datetime.now(timezone.utc):
            del _otp_store[email_lower]
            return False
        if not secrets.compare_digest(entry["code_hash"], _hash_otp(code, email_lower)):
            entry["attempts"] = entry.get("attempts", 0) + 1
            return False
        del _otp_store[email_lower]
    return True


def create_session_token(email: str) -> dict:
    now = datetime.now(timezone.utc)
    expiry = now + timedelta(minutes=settings.session_token_expiry_minutes)
    claims = {
        "sub": email.lower(),
        "email": email.lower(),
        "iat": int(now.timestamp()),
        "exp": int(expiry.timestamp()),
        "iss": "momentlens",
        "type": "session",
    }
    token = jwt.encode(claims, settings.session_token_secret, algorithm="HS256")
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_at": expiry.isoformat(),
        "expires_in_seconds": settings.session_token_expiry_minutes * 60,
    }


def decode_session_token(token: str) -> Optional[dict]:
    try:
        claims = jwt.decode(
            token,
            settings.session_token_secret,
            algorithms=["HS256"],
            issuer="momentlens",
            options={"verify_exp": True, "verify_iss": True},
        )
        if claims.get("type") != "session":
            return None
        if not validate_email_domain(claims.get("email", "")):
            return None
        return claims
    except Exception as exc:  # noqa: BLE001
        log.debug("Session token decode failed: %s", exc)
        return None


async def send_otp_email(email: str, code: str) -> bool:
    if not settings.acs_connection_string:
        log.warning("ACS not configured; OTP for %s: %s", email, code)
        return True
    try:
        from azure.communication.email import EmailClient

        client = EmailClient.from_connection_string(settings.acs_connection_string)
        message = {
            "senderAddress": settings.acs_sender_email,
            "recipients": {"to": [{"address": email}]},
            "content": {
                "subject": "Your MomentLens access code",
                "plainText": f"Your MomentLens access code is: {code}\n\nThis code expires in 5 minutes.",
            },
        }
        client.begin_send(message).result()
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to send OTP email to %s: %s", email, exc)
        return False
