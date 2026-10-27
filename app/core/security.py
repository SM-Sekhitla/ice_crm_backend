"""Adapted from PRISM: Argon2 credentials, short-lived JWTs, hashed opaque refresh tokens."""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from jose import JWTError, jwt

from app.core.config import get_settings

hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)


def now():
    return datetime.now(timezone.utc)


def hash_password(password: str):
    return hasher.hash(password)


def verify_password(password: str, encoded: str):
    try:
        return hasher.verify(encoded, password)
    except (VerificationError, InvalidHashError):
        return False


def opaque_token():
    return secrets.token_urlsafe(48)


def token_hash(token: str):
    return hashlib.sha256(token.encode()).hexdigest()


def access_token(user):
    settings = get_settings()
    issued = now()
    return jwt.encode(
        {
            "sub": user.id,
            "iss": settings.token_issuer,
            "type": "access",
            "ver": user.token_version,
            "iat": issued,
            "exp": issued + timedelta(minutes=settings.access_token_minutes),
            "jti": str(uuid4()),
        },
        settings.secret_key,
        algorithm="HS256",
    )


def decode_token(token: str):
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.secret_key,
            algorithms=["HS256"],
            issuer=settings.token_issuer,
            options={"require_exp": True, "require_sub": True, "require_iat": True},
        )
        if payload.get("type") != "access" or not isinstance(payload.get("ver"), int):
            raise ValueError("Invalid token")
        return payload
    except JWTError as exc:
        raise ValueError("Invalid token") from exc
