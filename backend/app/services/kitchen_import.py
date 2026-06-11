"""Importer for Square KDS kitchen report CSV exports.

Expected columns: Device Name, Ticket Name, Order Source, Number of Items,
Items in Ticket, Completion Time (seconds), Time Created, Time Completed,
Time Due, Time Recalled
"""

import csv
import hashlib
import io
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Ticket

REQUIRED_COLUMNS = {"Completion Time (seconds)", "Time Created"}


def _parse_dt(value: str) -> datetime | None:
    value = value.strip()
    if not value:
        return None
    return datetime.strptime(value, "%m/%d/%Y %H:%M")


def import_kitchen_csv(db: Session, data: bytes) -> dict:
    text = data.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None or not REQUIRED_COLUMNS.issubset(reader.fieldnames):
        raise ValueError(
            "Not a kitchen report: missing columns "
            + ", ".join(sorted(REQUIRED_COLUMNS - set(reader.fieldnames or [])))
        )

    existing = set(db.scalars(select(Ticket.row_hash)).all())
    created = skipped = 0
    for row in reader:
        raw = "|".join((row.get(k) or "") for k in reader.fieldnames)
        row_hash = hashlib.sha256(raw.encode()).hexdigest()
        if row_hash in existing:
            skipped += 1
            continue
        created_at = _parse_dt(row["Time Created"])
        completion = row["Completion Time (seconds)"].strip()
        if created_at is None or not completion:
            skipped += 1
            continue
        db.add(
            Ticket(
                station=(row.get("Device Name") or "").strip(),
                source=(row.get("Order Source") or "").strip(),
                items=int(row.get("Number of Items") or 1),
                created_at=created_at,
                completed_at=_parse_dt(row.get("Time Completed") or ""),
                completion_seconds=int(completion),
                recalled=bool((row.get("Time Recalled") or "").strip()),
                row_hash=row_hash,
            )
        )
        existing.add(row_hash)
        created += 1
    db.commit()
    return {"created": created, "skipped": skipped}
