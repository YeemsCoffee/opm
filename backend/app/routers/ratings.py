from datetime import date, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import schemas
from ..auth import require_manager
from ..db import get_db
from ..models import SolverConfig, User
from ..services.ratings import compute_ratings

router = APIRouter(prefix="/api/ratings", tags=["ratings"])


@router.get("", response_model=list[schemas.RatingOut])
def ratings(
    start: date | None = None,
    end: date | None = None,
    _: User = Depends(require_manager),
    db: Session = Depends(get_db),
):
    if end is None:
        end = date.today()
    if start is None:
        cfg = db.scalar(select(SolverConfig))
        lookback = cfg.rating_lookback_days if cfg else 90
        start = end - timedelta(days=lookback)
    return compute_ratings(db, start, end)
