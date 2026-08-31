from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String)
    role: Mapped[str] = mapped_column(String, default="employee")  # manager | employee
    employee_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id"), nullable=True)

    employee: Mapped["Employee | None"] = relationship()


class Level(Base):
    __tablename__ = "levels"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True)
    # Higher rank may be suggested to cover a lower-rank slot (never auto-assigned).
    rank: Mapped[int] = mapped_column(Integer, default=0)
    counts_for_rating: Mapped[bool] = mapped_column(Boolean, default=True)


class Skill(Base):
    """Manager-defined skills (dialing, steaming, …) checked off per employee."""

    __tablename__ = "skills"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True)


class EmployeeSkill(Base):
    __tablename__ = "employee_skills"
    __table_args__ = (UniqueConstraint("employee_id", "skill_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"))
    skill_id: Mapped[int] = mapped_column(ForeignKey("skills.id"))


class Location(Base):
    __tablename__ = "locations"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True)


class Employee(Base):
    __tablename__ = "employees"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True, index=True)
    payroll_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    max_week_minutes: Mapped[int] = mapped_column(Integer, default=2400)
    target_week_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    availability_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    location_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id"), nullable=True)

    location: Mapped[Location | None] = relationship()
    level_history: Mapped[list["EmployeeLevel"]] = relationship(
        back_populates="employee", cascade="all, delete-orphan", order_by="EmployeeLevel.effective_from"
    )
    availability: Mapped[list["Availability"]] = relationship(
        back_populates="employee", cascade="all, delete-orphan"
    )
    time_off: Mapped[list["TimeOff"]] = relationship(
        back_populates="employee", cascade="all, delete-orphan"
    )
    skills: Mapped[list[Skill]] = relationship(secondary="employee_skills")

    def level_on(self, on_date: date) -> Level | None:
        current = None
        for el in self.level_history:
            if el.effective_from <= on_date:
                current = el
        return current.level if current else None


class EmployeeLevel(Base):
    __tablename__ = "employee_levels"
    __table_args__ = (UniqueConstraint("employee_id", "effective_from"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"))
    level_id: Mapped[int] = mapped_column(ForeignKey("levels.id"))
    effective_from: Mapped[date] = mapped_column(Date)

    employee: Mapped[Employee] = relationship(back_populates="level_history")
    level: Mapped[Level] = relationship()


class Availability(Base):
    """Recurring weekly availability window. No rows for an employee means
    'fully available' until they confirm their real availability."""

    __tablename__ = "availability"

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"))
    weekday: Mapped[int] = mapped_column(Integer)  # 0=Monday .. 6=Sunday
    start_min: Mapped[int] = mapped_column(Integer)  # minutes from midnight
    end_min: Mapped[int] = mapped_column(Integer)

    employee: Mapped[Employee] = relationship(back_populates="availability")


class TimeOff(Base):
    __tablename__ = "time_off"

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    reason: Mapped[str] = mapped_column(String, default="")

    employee: Mapped[Employee] = relationship(back_populates="time_off")


class ShiftBlock(Base):
    """Manager-defined reusable shift window (e.g. 'Open' 6:30–14:30).
    The weekly pattern places blocks on weekdays with level headcounts."""

    __tablename__ = "shift_blocks"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True)
    start_min: Mapped[int] = mapped_column(Integer)
    end_min: Mapped[int] = mapped_column(Integer)


class ShiftTemplate(Base):
    """One block placed on one weekday of the recurring pattern, with the
    level headcounts needed. Applied to every future week when its schedule
    is generated, until the manager changes the pattern."""

    __tablename__ = "shift_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    weekday: Mapped[int] = mapped_column(Integer)  # 0=Monday .. 6=Sunday
    block_id: Mapped[int] = mapped_column(ForeignKey("shift_blocks.id"))

    block: Mapped[ShiftBlock] = relationship()
    requirements: Mapped[list["TemplateRequirement"]] = relationship(
        back_populates="template", cascade="all, delete-orphan"
    )


class TemplateRequirement(Base):
    __tablename__ = "template_requirements"
    __table_args__ = (UniqueConstraint("template_id", "level_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    template_id: Mapped[int] = mapped_column(ForeignKey("shift_templates.id"))
    level_id: Mapped[int] = mapped_column(ForeignKey("levels.id"))
    count: Mapped[int] = mapped_column(Integer, default=1)

    template: Mapped[ShiftTemplate] = relationship(back_populates="requirements")
    level: Mapped[Level] = relationship()


class Shift(Base):
    __tablename__ = "shifts"

    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    start_min: Mapped[int] = mapped_column(Integer)
    end_min: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String, default="")

    requirements: Mapped[list["ShiftRequirement"]] = relationship(
        back_populates="shift", cascade="all, delete-orphan"
    )


class ShiftRequirement(Base):
    __tablename__ = "shift_requirements"
    __table_args__ = (UniqueConstraint("shift_id", "level_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    shift_id: Mapped[int] = mapped_column(ForeignKey("shifts.id"))
    level_id: Mapped[int] = mapped_column(ForeignKey("levels.id"))
    count: Mapped[int] = mapped_column(Integer, default=1)

    shift: Mapped[Shift] = relationship(back_populates="requirements")
    level: Mapped[Level] = relationship()


class Schedule(Base):
    __tablename__ = "schedules"

    id: Mapped[int] = mapped_column(primary_key=True)
    week_start: Mapped[date] = mapped_column(Date, unique=True, index=True)
    status: Mapped[str] = mapped_column(String, default="draft")  # draft | published
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    assignments: Mapped[list["Assignment"]] = relationship(
        back_populates="schedule", cascade="all, delete-orphan"
    )


class Assignment(Base):
    __tablename__ = "assignments"
    __table_args__ = (UniqueConstraint("schedule_id", "shift_id", "employee_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    schedule_id: Mapped[int] = mapped_column(ForeignKey("schedules.id"))
    shift_id: Mapped[int] = mapped_column(ForeignKey("shifts.id"))
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"))
    # The requirement slot this assignment covers (may differ from the
    # employee's own level for manual higher-level-covers-lower overrides).
    fills_level_id: Mapped[int] = mapped_column(ForeignKey("levels.id"))
    manual: Mapped[bool] = mapped_column(Boolean, default=False)

    schedule: Mapped[Schedule] = relationship(back_populates="assignments")
    shift: Mapped[Shift] = relationship()
    employee: Mapped[Employee] = relationship()
    fills_level: Mapped[Level] = relationship()


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(primary_key=True)
    station: Mapped[str] = mapped_column(String, default="")
    source: Mapped[str] = mapped_column(String, default="")
    items: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completion_seconds: Mapped[int] = mapped_column(Integer)
    recalled: Mapped[bool] = mapped_column(Boolean, default=False)
    row_hash: Mapped[str] = mapped_column(String, unique=True, index=True)


class SlaConfig(Base):
    """Effective-dated ticket close target. Each ticket is judged against the
    target in force when it was created."""

    __tablename__ = "sla_configs"

    id: Mapped[int] = mapped_column(primary_key=True)
    target_seconds: Mapped[int] = mapped_column(Integer)
    adherence_goal: Mapped[float] = mapped_column(Float, default=0.9)
    effective_from: Mapped[date] = mapped_column(Date, unique=True)


class SolverConfig(Base):
    __tablename__ = "solver_config"

    id: Mapped[int] = mapped_column(primary_key=True)
    min_rest_minutes: Mapped[int] = mapped_column(Integer, default=480)
    rating_lookback_days: Mapped[int] = mapped_column(Integer, default=90)
    shrinkage_tickets: Mapped[int] = mapped_column(Integer, default=300)
    # overridable limits: the solver never crosses these on its own; manual
    # assignments may, and get flagged on the schedule
    max_day_minutes: Mapped[int] = mapped_column(Integer, default=480)
    max_consecutive_days: Mapped[int] = mapped_column(Integer, default=6)


class WorkSession(Base):
    __tablename__ = "work_sessions"
    __table_args__ = (UniqueConstraint("employee_id", "clock_in"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"))
    level_id: Mapped[int] = mapped_column(ForeignKey("levels.id"))
    clock_in: Mapped[datetime] = mapped_column(DateTime, index=True)
    clock_out: Mapped[datetime] = mapped_column(DateTime)

    employee: Mapped[Employee] = relationship()
    level: Mapped[Level] = relationship()
    breaks: Mapped[list["BreakInterval"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )

    def on_floor(self, t: datetime) -> bool:
        if not (self.clock_in <= t < self.clock_out):
            return False
        return not any(b.start <= t < b.end for b in self.breaks)


class BreakConfig(Base):
    """Single-row knobs for the break scheduler."""

    __tablename__ = "break_config"

    id: Mapped[int] = mapped_column(primary_key=True)
    rest_minutes: Mapped[int] = mapped_column(Integer, default=10)
    meal_minutes: Mapped[int] = mapped_column(Integer, default=30)
    edge_pad_minutes: Mapped[int] = mapped_column(Integer, default=45)  # no breaks near shift edges
    min_gap_minutes: Mapped[int] = mapped_column(Integer, default=90)  # between one person's breaks
    max_concurrent: Mapped[int] = mapped_column(Integer, default=1)  # on break at the same time
    meal_by_minute: Mapped[int] = mapped_column(Integer, default=300)  # meal starts before 5th hour ends


class BreakRule(Base):
    """Break entitlement by shift length: the longest matching rule wins."""

    __tablename__ = "break_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    min_shift_minutes: Mapped[int] = mapped_column(Integer, unique=True)
    rest_breaks: Mapped[int] = mapped_column(Integer, default=0)
    meal_breaks: Mapped[int] = mapped_column(Integer, default=0)


class RosterEntry(Base):
    """One person working one shift on one date, for break planning. Comes
    from Homebase, from this app's schedule, or typed in by the manager."""

    __tablename__ = "roster_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    name: Mapped[str] = mapped_column(String)
    role: Mapped[str] = mapped_column(String, default="")
    start_min: Mapped[int] = mapped_column(Integer)
    end_min: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String, default="manual")  # homebase | internal | manual

    breaks: Mapped[list["BreakPlanItem"]] = relationship(
        back_populates="entry", cascade="all, delete-orphan", order_by="BreakPlanItem.start_min"
    )


class BreakPlanItem(Base):
    __tablename__ = "break_plan_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    roster_entry_id: Mapped[int] = mapped_column(ForeignKey("roster_entries.id"))
    kind: Mapped[str] = mapped_column(String)  # rest | meal
    start_min: Mapped[int] = mapped_column(Integer)
    end_min: Mapped[int] = mapped_column(Integer)
    paid: Mapped[bool] = mapped_column(Boolean, default=True)

    entry: Mapped[RosterEntry] = relationship(back_populates="breaks")


class BreakInterval(Base):
    __tablename__ = "break_intervals"

    id: Mapped[int] = mapped_column(primary_key=True)
    work_session_id: Mapped[int] = mapped_column(ForeignKey("work_sessions.id"))
    start: Mapped[datetime] = mapped_column(DateTime)
    end: Mapped[datetime] = mapped_column(DateTime)
    paid: Mapped[bool] = mapped_column(Boolean, default=True)

    session: Mapped[WorkSession] = relationship(back_populates="breaks")


class HomebaseSyncStatus(Base):
    """Single-row status of the scheduled Homebase browser sync — surfaced
    in the UI so a stale/failed sync is visible, not silent."""

    __tablename__ = "homebase_sync_status"

    id: Mapped[int] = mapped_column(primary_key=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(String, default="")
    session_valid: Mapped[bool] = mapped_column(Boolean, default=False)
    hours_rows_last_sync: Mapped[int] = mapped_column(Integer, default=0)
    swaps_rows_last_sync: Mapped[int] = mapped_column(Integer, default=0)


class HoursSnapshot(Base):
    """One employee's worked-hours total for a period, as scraped from the
    Homebase hours report. Re-synced daily; latest row per (employee,
    period) wins, so the employee profile always shows a live number."""

    __tablename__ = "hours_snapshots"
    __table_args__ = (UniqueConstraint("employee_name", "period_start", "period_end"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_name: Mapped[str] = mapped_column(String, index=True)
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    hours: Mapped[float] = mapped_column(Float)
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ShiftSwap(Base):
    """One shift that was released for coverage and picked up by someone,
    as scraped from Homebase's trade/coverage board."""

    __tablename__ = "shift_swaps"
    __table_args__ = (UniqueConstraint("shift_date", "start_min", "end_min", "released_by", "covered_by"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    shift_date: Mapped[date] = mapped_column(Date, index=True)
    start_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    released_by: Mapped[str] = mapped_column(String, default="")
    covered_by: Mapped[str] = mapped_column(String, default="")
    role: Mapped[str] = mapped_column(String, default="")
    status: Mapped[str] = mapped_column(String, default="")  # raw status text from Homebase
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class NoShow(Base):
    __tablename__ = "no_shows"
    __table_args__ = (UniqueConstraint("employee_id", "date", "reason"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"))
    date: Mapped[date] = mapped_column(Date)
    reason: Mapped[str] = mapped_column(String, default="")

    employee: Mapped[Employee] = relationship()
