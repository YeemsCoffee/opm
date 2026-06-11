from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import schemas
from ..auth import require_manager
from ..db import get_db
from ..models import SlaConfig, SolverConfig, User
from ..services.ratings import adherence_what_if

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("/sla", response_model=list[schemas.SlaOut])
def list_sla(_: User = Depends(require_manager), db: Session = Depends(get_db)):
    return db.scalars(select(SlaConfig).order_by(SlaConfig.effective_from)).all()


@router.post("/sla", response_model=schemas.SlaOut)
def add_sla(body: schemas.SlaIn, _: User = Depends(require_manager), db: Session = Depends(get_db)):
    if body.target_seconds <= 0 or not (0 < body.adherence_goal <= 1):
        raise HTTPException(422, "Invalid target")
    existing = db.scalar(select(SlaConfig).where(SlaConfig.effective_from == body.effective_from))
    if existing:
        existing.target_seconds = body.target_seconds
        existing.adherence_goal = body.adherence_goal
        db.commit()
        return existing
    row = SlaConfig(**body.model_dump())
    db.add(row)
    db.commit()
    return row


@router.get("/sla/what-if", response_model=schemas.WhatIfOut)
def what_if(
    target_seconds: int,
    start: date | None = None,
    end: date | None = None,
    _: User = Depends(require_manager),
    db: Session = Depends(get_db),
):
    if end is None:
        end = date.today()
    if start is None:
        start = end - timedelta(days=30)
    return adherence_what_if(db, target_seconds, start, end)


@router.get("/solver", response_model=schemas.SolverConfigOut)
def get_solver(_: User = Depends(require_manager), db: Session = Depends(get_db)):
    return db.scalar(select(SolverConfig))


@router.patch("/solver", response_model=schemas.SolverConfigOut)
def update_solver(
    body: schemas.SolverConfigIn, _: User = Depends(require_manager), db: Session = Depends(get_db)
):
    cfg = db.scalar(select(SolverConfig))
    for attr in ("min_rest_minutes", "rating_lookback_days", "shrinkage_tickets"):
        val = getattr(body, attr)
        if val is not None:
            setattr(cfg, attr, val)
    db.commit()
    return cfg
