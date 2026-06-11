from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .. import schemas
from ..auth import require_manager
from ..db import get_db
from ..models import (
    Shift,
    ShiftBlock,
    ShiftRequirement,
    ShiftTemplate,
    TemplateRequirement,
    User,
)

router = APIRouter(prefix="/api", tags=["pattern"])


# --- shift blocks (reusable time windows) ---

@router.get("/blocks", response_model=list[schemas.BlockOut])
def list_blocks(_: User = Depends(require_manager), db: Session = Depends(get_db)):
    return db.scalars(select(ShiftBlock).order_by(ShiftBlock.start_min)).all()


def _validate_block(body: schemas.BlockIn) -> None:
    if not body.name.strip():
        raise HTTPException(422, "Block name required")
    if not (0 <= body.start_min < body.end_min <= 1440):
        raise HTTPException(422, "Invalid block times")


@router.post("/blocks", response_model=schemas.BlockOut)
def create_block(
    body: schemas.BlockIn, _: User = Depends(require_manager), db: Session = Depends(get_db)
):
    _validate_block(body)
    if db.scalar(select(ShiftBlock).where(ShiftBlock.name == body.name.strip())):
        raise HTTPException(409, "A block with that name already exists")
    block = ShiftBlock(name=body.name.strip(), start_min=body.start_min, end_min=body.end_min)
    db.add(block)
    db.commit()
    return block


@router.put("/blocks/{block_id}", response_model=schemas.BlockOut)
def update_block(
    block_id: int,
    body: schemas.BlockIn,
    _: User = Depends(require_manager),
    db: Session = Depends(get_db),
):
    block = db.get(ShiftBlock, block_id)
    if block is None:
        raise HTTPException(404, "Block not found")
    _validate_block(body)
    block.name, block.start_min, block.end_min = body.name.strip(), body.start_min, body.end_min
    db.commit()
    return block


@router.delete("/blocks/{block_id}")
def delete_block(block_id: int, _: User = Depends(require_manager), db: Session = Depends(get_db)):
    block = db.get(ShiftBlock, block_id)
    if block is None:
        raise HTTPException(404, "Block not found")
    used = db.scalar(select(ShiftTemplate.id).where(ShiftTemplate.block_id == block_id))
    if used is not None:
        raise HTTPException(409, "Block is used in the weekly pattern — remove it there first")
    db.delete(block)
    db.commit()
    return {"ok": True}


# --- weekly pattern (blocks placed on weekdays with level headcounts) ---

def _all_templates(db: Session) -> list[ShiftTemplate]:
    return list(
        db.scalars(
            select(ShiftTemplate)
            .options(
                selectinload(ShiftTemplate.block),
                selectinload(ShiftTemplate.requirements).selectinload(TemplateRequirement.level),
            )
            .join(ShiftBlock, ShiftTemplate.block_id == ShiftBlock.id)
            .order_by(ShiftTemplate.weekday, ShiftBlock.start_min)
        )
    )


def materialize_week(db: Session, week_start: date) -> int:
    """Create the week's shifts from the weekly pattern. Only fully empty
    weeks are materialized: once a week has shifts (from the pattern or by
    hand), the manager's per-week edits — including deletions — stick, and
    pattern changes only affect weeks generated after the change."""
    end = week_start + timedelta(days=7)
    has_shifts = (
        db.scalar(select(Shift.id).where(Shift.date >= week_start, Shift.date < end)) is not None
    )
    if has_shifts:
        return 0
    created = 0
    for t in _all_templates(db):
        shift = Shift(
            date=week_start + timedelta(days=t.weekday),
            start_min=t.block.start_min,
            end_min=t.block.end_min,
            name=t.block.name,
        )
        for r in t.requirements:
            shift.requirements.append(ShiftRequirement(level_id=r.level_id, count=r.count))
        db.add(shift)
        created += 1
    if created:
        db.commit()
    return created


def _validate_template(db: Session, body: schemas.TemplateIn) -> ShiftBlock:
    if not (0 <= body.weekday <= 6):
        raise HTTPException(422, "weekday must be 0 (Monday) to 6 (Sunday)")
    block = db.get(ShiftBlock, body.block_id)
    if block is None:
        raise HTTPException(422, "Unknown shift block")
    return block


@router.get("/templates", response_model=list[schemas.TemplateOut])
def list_templates(_: User = Depends(require_manager), db: Session = Depends(get_db)):
    return _all_templates(db)


@router.post("/templates", response_model=schemas.TemplateOut)
def create_template(
    body: schemas.TemplateIn, _: User = Depends(require_manager), db: Session = Depends(get_db)
):
    _validate_template(db, body)
    t = ShiftTemplate(weekday=body.weekday, block_id=body.block_id)
    for r in body.requirements:
        t.requirements.append(TemplateRequirement(level_id=r.level_id, count=r.count))
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


@router.put("/templates/{template_id}", response_model=schemas.TemplateOut)
def update_template(
    template_id: int,
    body: schemas.TemplateIn,
    _: User = Depends(require_manager),
    db: Session = Depends(get_db),
):
    t = db.get(ShiftTemplate, template_id)
    if t is None:
        raise HTTPException(404, "Template not found")
    _validate_template(db, body)
    t.weekday, t.block_id = body.weekday, body.block_id
    t.requirements.clear()
    db.flush()
    for r in body.requirements:
        t.requirements.append(TemplateRequirement(level_id=r.level_id, count=r.count))
    db.commit()
    db.refresh(t)
    return t


@router.delete("/templates/{template_id}")
def delete_template(
    template_id: int, _: User = Depends(require_manager), db: Session = Depends(get_db)
):
    t = db.get(ShiftTemplate, template_id)
    if t is None:
        raise HTTPException(404, "Template not found")
    db.delete(t)
    db.commit()
    return {"ok": True}
