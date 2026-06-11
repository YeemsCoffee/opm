import base64
import hashlib
import hmac
import json
import os
import secrets
import time

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from .db import get_db
from .models import User

_SECRET_FILE = os.path.join(os.path.dirname(__file__), "..", ".secret")


def _secret() -> bytes:
    env = os.environ.get("OPM_SECRET")
    if env:
        return env.encode()
    if os.path.exists(_SECRET_FILE):
        with open(_SECRET_FILE, "rb") as f:
            return f.read()
    key = secrets.token_bytes(32)
    with open(_SECRET_FILE, "wb") as f:
        f.write(key)
    return key


# --- passwords ---

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return salt.hex() + ":" + dk.hex()


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, dk_hex = stored.split(":")
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 200_000)
    return hmac.compare_digest(dk.hex(), dk_hex)


# --- tokens (HMAC-signed, 7 day expiry) ---

TOKEN_TTL = 7 * 24 * 3600


def create_token(user_id: int) -> str:
    payload = json.dumps({"uid": user_id, "exp": int(time.time()) + TOKEN_TTL}).encode()
    body = base64.urlsafe_b64encode(payload).rstrip(b"=")
    sig = hmac.new(_secret(), body, hashlib.sha256).hexdigest()
    return body.decode() + "." + sig


def decode_token(token: str) -> int | None:
    try:
        body, sig = token.split(".")
        expected = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if payload["exp"] < time.time():
            return None
        return payload["uid"]
    except Exception:
        return None


_bearer = HTTPBearer(auto_error=False)


def current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    if creds is None:
        raise HTTPException(401, "Not authenticated")
    uid = decode_token(creds.credentials)
    if uid is None:
        raise HTTPException(401, "Invalid or expired token")
    user = db.get(User, uid)
    if user is None:
        raise HTTPException(401, "User not found")
    return user


def require_manager(user: User = Depends(current_user)) -> User:
    if user.role != "manager":
        raise HTTPException(403, "Manager access required")
    return user
