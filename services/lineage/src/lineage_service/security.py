"""Passwords (argon2), API keys (stored as SHA-256), and signed session cookies."""

import base64
import hashlib
import hmac
import json
import secrets
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from lineage_service.models import ApiKey, User

_ph = PasswordHasher()
KEY_PREFIX = "lk_"


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(hashed: str, password: str) -> bool:
    try:
        return _ph.verify(hashed, password)
    except VerificationError:
        return False


def _key_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


async def create_api_key(session: AsyncSession, name: str, raw: str | None = None) -> tuple[ApiKey, str]:
    raw = raw or KEY_PREFIX + secrets.token_urlsafe(32)
    key = ApiKey(id=str(uuid.uuid4()), name=name, prefix=raw[:10], hash=_key_hash(raw))
    session.add(key)
    await session.commit()
    return key, raw


async def verify_api_key(session: AsyncSession, raw: str) -> ApiKey | None:
    key = (await session.execute(select(ApiKey).where(
        ApiKey.hash == _key_hash(raw), ApiKey.revoked_at.is_(None)))).scalar_one_or_none()
    if key:
        await session.execute(update(ApiKey).where(ApiKey.id == key.id).values(last_used_at=datetime.now(UTC)))
        await session.commit()
    return key


async def authenticate(session: AsyncSession, email: str, password: str) -> User | None:
    user = (await session.execute(select(User).where(User.email == email.lower().strip()))).scalar_one_or_none()
    if user is None:
        verify_password(_DUMMY_HASH, password)  # equalise timing for unknown emails
        return None
    return user if verify_password(user.password_hash, password) else None


async def ensure_user(session: AsyncSession, email: str, password: str, role: str = "admin") -> User:
    email = email.lower().strip()
    user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is None:
        user = User(id=str(uuid.uuid4()), email=email, password_hash=hash_password(password), role=role)
        session.add(user)
        await session.commit()
    return user


def sign_session(user_id: str, secret: str, ttl_s: int) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"uid": user_id, "exp": int(time.time()) + ttl_s}).encode())
    sig = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return f"{payload.decode()}.{sig}"


def read_session(token: str, secret: str) -> dict[str, Any] | None:
    try:
        payload, sig = token.rsplit(".", 1)
    except ValueError:
        return None
    expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    data: dict[str, Any] = json.loads(base64.urlsafe_b64decode(payload))
    return data if data.get("exp", 0) > time.time() else None


def tokens_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


_DUMMY_HASH = _ph.hash("timing-equaliser")
