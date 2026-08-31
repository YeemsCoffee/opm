from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy.orm import Session

from .. import schemas
from ..auth import require_manager
from ..db import get_db
from ..models import User
from ..services.kitchen_import import import_kitchen_csv
from ..services.team_import import import_team_csv
from ..services.timesheet_import import import_timesheet_csv

router = APIRouter(prefix="/api/imports", tags=["imports"])


@router.post("/kitchen", response_model=schemas.ImportResult)
async def upload_kitchen(
    file: UploadFile, _: User = Depends(require_manager), db: Session = Depends(get_db)
):
    data = await file.read()
    try:
        result = import_kitchen_csv(db, data)
    except (ValueError, KeyError) as exc:
        raise HTTPException(422, str(exc))
    return schemas.ImportResult(created=result["created"], skipped=result["skipped"])


@router.post("/timesheets", response_model=schemas.ImportResult)
async def upload_timesheets(
    file: UploadFile, _: User = Depends(require_manager), db: Session = Depends(get_db)
):
    data = await file.read()
    try:
        result = import_timesheet_csv(db, data)
    except (ValueError, KeyError) as exc:
        raise HTTPException(422, str(exc))
    return schemas.ImportResult(
        created=result["created"],
        skipped=result["skipped"],
        details={"no_shows": result["no_shows"], "levels_updated": result["levels_updated"]},
    )


@router.post("/team", response_model=schemas.ImportResult)
async def upload_team(
    file: UploadFile, _: User = Depends(require_manager), db: Session = Depends(get_db)
):
    data = await file.read()
    try:
        result = import_team_csv(db, data)
    except (ValueError, KeyError) as exc:
        raise HTTPException(422, str(exc))
    return schemas.ImportResult(
        created=result["created"],
        skipped=result["updated"],
        details={
            "new_levels": result["new_levels"],
            "additional_locations_skipped": result["additional_locations_skipped"],
            "levels_kept_unchanged": result["levels_kept_unchanged"],
        },
    )
