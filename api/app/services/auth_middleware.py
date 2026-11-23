"""FastAPI dependencies for OTP-issued session tokens."""
from __future__ import annotations

from typing import Optional

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import settings
from app.services.otp_service import decode_session_token

security = HTTPBearer(auto_error=False)


async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> dict | None:
    if not settings.otp_auth_enabled:
        return {"sub": "dev-user", "email": "dev@example.com"}
    if settings.debug and not credentials:
        return {"sub": "dev-user", "email": "dev@example.com"}
    token = credentials.credentials if credentials else request.cookies.get("momentlens_session")
    if not token:
        raise HTTPException(status_code=401, detail="Authorization header required")
    claims = decode_session_token(token)
    if not claims:
        raise HTTPException(status_code=401, detail="Invalid or expired session token")
    return claims


async def require_auth(user: dict = Depends(get_current_user)) -> dict:
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user
