"""OTP authentication routes for MomentLens."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field

from app.config import settings
from app.services.auth_middleware import get_current_user
from app.services.otp_service import (
    check_rate_limit,
    create_session_token,
    generate_otp,
    send_otp_email,
    store_otp_memory,
    validate_email_domain,
    verify_otp_memory,
)

router = APIRouter()


class OTPRequestBody(BaseModel):
    email: EmailStr = Field(..., description="Business email address")


class OTPVerifyBody(BaseModel):
    email: EmailStr = Field(..., description="Email used to request the OTP")
    code: str = Field(..., min_length=6, max_length=6, pattern=r"^\d{6}$")


class OTPRequestResponse(BaseModel):
    success: bool
    message: str


class OTPVerifyResponse(BaseModel):
    success: bool
    access_token: str = ""
    token_type: str = "bearer"
    expires_at: str = ""
    expires_in_seconds: int = 0
    email: str = ""


@router.post("/request-otp", response_model=OTPRequestResponse)
async def request_otp(body: OTPRequestBody, request: Request) -> OTPRequestResponse:
    email = body.email.strip().lower()
    remote = request.client.host if request.client else "unknown"
    if not check_rate_limit(f"otp-request:{remote}:{email}", limit=3, window_seconds=600):
        raise HTTPException(status_code=429, detail="Too many code requests. Please try again later.")
    if not validate_email_domain(email):
        raise HTTPException(
            status_code=403,
            detail="Access restricted. Your email domain is not authorized for this demo.",
        )
    code = generate_otp()
    await store_otp_memory(email, code)
    if not await send_otp_email(email, code):
        raise HTTPException(status_code=500, detail="Failed to send verification email. Please try again.")
    return OTPRequestResponse(success=True, message="Verification code sent. It expires in 5 minutes.")


@router.post("/verify-otp", response_model=OTPVerifyResponse)
async def verify_otp_endpoint(body: OTPVerifyBody, request: Request, response: Response) -> OTPVerifyResponse:
    email = body.email.strip().lower()
    remote = request.client.host if request.client else "unknown"
    if not check_rate_limit(f"otp-verify:{remote}:{email}", limit=5, window_seconds=600):
        raise HTTPException(status_code=429, detail="Too many verification attempts. Please try again later.")
    if not validate_email_domain(email):
        raise HTTPException(status_code=403, detail="Email domain not authorized.")
    if not await verify_otp_memory(email, body.code.strip()):
        raise HTTPException(status_code=401, detail="Invalid or expired code. Please check and try again.")
    token_data = create_session_token(email)
    response.set_cookie(
        key="momentlens_session",
        value=token_data["access_token"],
        max_age=token_data["expires_in_seconds"],
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    return OTPVerifyResponse(success=True, email=email, **token_data)


@router.post("/logout")
async def logout(response: Response) -> dict:
    response.delete_cookie("momentlens_session", path="/")
    return {"success": True}


@router.get("/me")
async def me(user: dict | None = Depends(get_current_user)) -> dict:
    return {
        "authenticated": bool(user),
        "email": (user or {}).get("email", ""),
        "auth_enabled": settings.otp_auth_enabled,
        "allowed_domains": settings.allowed_domains_list,
    }
