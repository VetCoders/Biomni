from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

from app.database import create_user, get_user_by_email, get_user_by_id
from app.models import AuthMessageResponse, AuthResponse, LoginRequest, RegisterRequest, UserResponse

try:
    import aiosqlite  # type: ignore[import-not-found]
except Exception:  # pragma: no cover - used only when dependency is missing
    class _FallbackAioSqlite:
        IntegrityError = sqlite3.IntegrityError

    aiosqlite = _FallbackAioSqlite()  # type: ignore[assignment]

try:
    from jose import JWTError  # type: ignore[import-not-found]
    from jose import jwt as jose_jwt  # type: ignore[import-not-found]

    HAS_JOSE = True
except Exception:  # pragma: no cover - fallback used only when dependency is missing
    class JWTError(Exception):
        pass

    jose_jwt = None
    HAS_JOSE = False

try:
    from passlib.context import CryptContext  # type: ignore[import-not-found]

    _pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
    HAS_PASSLIB = True
except Exception:  # pragma: no cover - fallback used only when dependency is missing
    _pwd_context = None
    HAS_PASSLIB = False

if HAS_PASSLIB:
    # Some environments ship a bcrypt build that passlib can import but not use reliably.
    try:
        import bcrypt as _bcrypt  # type: ignore[import-not-found]

        if not hasattr(_bcrypt, "__about__"):
            HAS_PASSLIB = False
    except Exception:
        HAS_PASSLIB = False


ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_DAYS = 7
JWT_SECRET = (
    os.getenv("JWT_SECRET_KEY")
    or os.getenv("BIOMNI_JWT_SECRET")
    or os.getenv("APP_SECRET_KEY")
    or "change-this-in-production"
)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")
router = APIRouter(prefix="/api/auth", tags=["auth"])


def _is_valid_email(email: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email))


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _to_user_response(user_row: dict[str, Any]) -> UserResponse:
    return UserResponse(
        id=user_row["id"],
        email=user_row["email"],
        display_name=user_row["display_name"],
        role=user_row.get("role", "user"),
        created_at=user_row["created_at"],
    )


def hash_password(password: str) -> str:
    global HAS_PASSLIB
    if HAS_PASSLIB and _pwd_context is not None:
        try:
            return _pwd_context.hash(password)
        except Exception:
            HAS_PASSLIB = False

    # Fallback for environments missing passlib.
    try:
        import bcrypt as _bcrypt  # type: ignore[import-not-found]

        return _bcrypt.hashpw(password.encode("utf-8"), _bcrypt.gensalt()).decode("utf-8")
    except Exception:
        pass

    # Last-resort fallback if bcrypt itself is unavailable.
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 200_000).hex()
    return f"pbkdf2_sha256${salt}${digest}"


def verify_password(plain_password: str, password_hash: str) -> bool:
    global HAS_PASSLIB
    if HAS_PASSLIB and _pwd_context is not None:
        try:
            return _pwd_context.verify(plain_password, password_hash)
        except Exception:
            HAS_PASSLIB = False

    if password_hash.startswith("$2"):
        try:
            import bcrypt as _bcrypt  # type: ignore[import-not-found]

            return _bcrypt.checkpw(plain_password.encode("utf-8"), password_hash.encode("utf-8"))
        except Exception:
            return False

    try:
        scheme, salt, digest = password_hash.split("$", 2)
    except ValueError:
        return False
    if scheme != "pbkdf2_sha256":
        return False
    expected = hashlib.pbkdf2_hmac("sha256", plain_password.encode("utf-8"), salt.encode("utf-8"), 200_000).hex()
    return hmac.compare_digest(expected, digest)


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")


def _b64url_decode(raw: str) -> bytes:
    padding = "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode(raw + padding)


def _encode_jwt_fallback(payload: dict[str, Any], secret: str) -> str:
    header = {"alg": ALGORITHM, "typ": "JWT"}
    header_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header_b64}.{payload_b64}".encode()
    signature = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{header_b64}.{payload_b64}.{_b64url_encode(signature)}"


def _decode_jwt_fallback(token: str, secret: str) -> dict[str, Any]:
    try:
        header_b64, payload_b64, signature_b64 = token.split(".")
    except ValueError as exc:
        raise JWTError("Malformed token") from exc

    signing_input = f"{header_b64}.{payload_b64}".encode()
    expected_sig = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    actual_sig = _b64url_decode(signature_b64)
    if not hmac.compare_digest(expected_sig, actual_sig):
        raise JWTError("Invalid token signature")

    try:
        payload = json.loads(_b64url_decode(payload_b64))
    except Exception as exc:  # pragma: no cover
        raise JWTError("Invalid token payload") from exc

    exp = payload.get("exp")
    if exp is None:
        raise JWTError("Token missing exp claim")
    now_ts = int(_now_utc().timestamp())
    if int(exp) < now_ts:
        raise JWTError("Token expired")
    return payload


def create_access_token(
    subject: str,
    expires_delta: timedelta | None = None,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    now = _now_utc()
    expire_at = now + (expires_delta or timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS))
    payload: dict[str, Any] = {
        "sub": subject,
        "iat": int(now.timestamp()),
        "exp": int(expire_at.timestamp()),
    }
    if extra_claims:
        payload.update(extra_claims)

    if HAS_JOSE and jose_jwt is not None:
        return jose_jwt.encode(payload, JWT_SECRET, algorithm=ALGORITHM)
    return _encode_jwt_fallback(payload, JWT_SECRET)


def decode_access_token(token: str) -> dict[str, Any]:
    if HAS_JOSE and jose_jwt is not None:
        return jose_jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM])
    return _decode_jwt_fallback(token, JWT_SECRET)


async def get_current_user(token: str = Depends(oauth2_scheme)) -> dict[str, Any]:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid authentication credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = decode_access_token(token)
    except JWTError as exc:
        raise credentials_exception from exc

    user_id = payload.get("sub")
    if not user_id:
        raise credentials_exception

    user = await get_user_by_id(str(user_id))
    if not user:
        raise credentials_exception
    return user


@router.post("/register", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
async def register(request: RegisterRequest) -> AuthResponse:
    email = request.email.strip().lower()
    if not _is_valid_email(email):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid email address")

    existing = await get_user_by_email(email)
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

    display_name = request.display_name.strip()
    if not display_name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Display name is required")

    try:
        user = await create_user(
            email=email,
            password_hash=hash_password(request.password),
            display_name=display_name,
            role="user",
        )
    except aiosqlite.IntegrityError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered") from exc

    expires_at = _now_utc() + timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS)
    token = create_access_token(user["id"])
    return AuthResponse(
        access_token=token,
        expires_at=expires_at.isoformat(),
        user=_to_user_response(user),
    )


@router.post("/login", response_model=AuthResponse)
async def login(request: LoginRequest) -> AuthResponse:
    email = request.email.strip().lower()
    user = await get_user_by_email(email)
    if not user or not verify_password(request.password, user["password_hash"]):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")

    expires_at = _now_utc() + timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS)
    token = create_access_token(user["id"])
    return AuthResponse(
        access_token=token,
        expires_at=expires_at.isoformat(),
        user=_to_user_response(user),
    )


@router.get("/me", response_model=UserResponse)
async def me(current_user: dict[str, Any] = Depends(get_current_user)) -> UserResponse:
    return _to_user_response(current_user)


@router.post("/logout", response_model=AuthMessageResponse)
async def logout(_current_user: dict[str, Any] = Depends(get_current_user)) -> AuthMessageResponse:
    # Stateless JWT auth: token invalidation is handled client-side.
    return AuthMessageResponse(message="Logged out")
