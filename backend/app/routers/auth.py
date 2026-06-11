from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import schemas
from ..auth import create_token, current_user, hash_password, require_manager, verify_password
from ..db import get_db
from ..models import User

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/register", response_model=schemas.TokenOut)
def register(body: schemas.RegisterIn, db: Session = Depends(get_db)):
    """Bootstrap: the very first account becomes a manager. After that,
    accounts are created by managers via /users."""
    count = db.scalar(select(func.count(User.id)))
    if count:
        raise HTTPException(403, "Registration closed; ask a manager to create your account")
    user = User(email=body.email.lower(), password_hash=hash_password(body.password), role="manager")
    db.add(user)
    db.commit()
    return schemas.TokenOut(token=create_token(user.id), role=user.role, employee_id=None)


@router.post("/login", response_model=schemas.TokenOut)
def login(body: schemas.LoginIn, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == body.email.lower()))
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "Invalid email or password")
    return schemas.TokenOut(token=create_token(user.id), role=user.role, employee_id=user.employee_id)


@router.get("/me", response_model=schemas.UserOut)
def me(user: User = Depends(current_user)):
    return user


@router.post("/users", response_model=schemas.UserOut)
def create_user(body: schemas.RegisterIn, _: User = Depends(require_manager), db: Session = Depends(get_db)):
    if db.scalar(select(User).where(User.email == body.email.lower())):
        raise HTTPException(409, "Email already registered")
    if body.role not in ("manager", "employee"):
        raise HTTPException(422, "Role must be manager or employee")
    user = User(
        email=body.email.lower(),
        password_hash=hash_password(body.password),
        role=body.role,
        employee_id=body.employee_id,
    )
    db.add(user)
    db.commit()
    return user
