import os
from datetime import date

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from starlette.exceptions import HTTPException as StarletteHTTPException


class SPAStaticFiles(StaticFiles):
    """Serve index.html for client-side routes like /schedule."""

    async def get_response(self, path, scope):
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404:
                raise
            return await super().get_response("index.html", scope)
        if response.status_code == 404:
            return await super().get_response("index.html", scope)
        return response

from . import models
from .db import Base, SessionLocal, engine
from .routers import auth, employees, imports, ratings, schedules, settings, shifts

DEFAULT_LEVELS = [
    # (name, rank, counts_for_rating) — ranks are editable in Settings
    ("Manager", 100, True),
    ("Shift Lead", 90, True),
    ("AM in Training", 80, True),
    ("B2", 70, True),
    ("B1", 60, True),
    ("K1", 50, True),
    ("T2", 40, True),
    ("Training", 10, False),
]

DEFAULT_SLA_TARGET_SECONDS = 300


def seed_defaults() -> None:
    db = SessionLocal()
    try:
        if not db.scalar(select(models.Level)):
            for name, rank, counts in DEFAULT_LEVELS:
                db.add(models.Level(name=name, rank=rank, counts_for_rating=counts))
        if not db.scalar(select(models.SlaConfig)):
            db.add(
                models.SlaConfig(
                    target_seconds=DEFAULT_SLA_TARGET_SECONDS,
                    adherence_goal=0.9,
                    effective_from=date(2000, 1, 1),
                )
            )
        if not db.scalar(select(models.SolverConfig)):
            db.add(models.SolverConfig())
        db.commit()
    finally:
        db.close()


def create_app() -> FastAPI:
    Base.metadata.create_all(engine)
    seed_defaults()

    app = FastAPI(title="OPM — Yeems Coffee scheduling")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    for router in (auth, employees, shifts, schedules, imports, ratings, settings):
        app.include_router(router.router)

    dist = os.path.join(os.path.dirname(__file__), "..", "..", "frontend", "dist")
    if os.path.isdir(dist):
        app.mount("/", SPAStaticFiles(directory=dist, html=True), name="frontend")
    return app


app = create_app()
