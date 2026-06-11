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


class Employee(Base):
    __tablename__ = "employees"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True, index=True)
    payroll_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    max_week_minutes: Mapped[int] = mapped_column(Integer, default=2400)
    target_week_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    availability_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)

    level_history: Mapped[list["EmployeeLevel"]] = relationship(
        back_populates="employee", cascade="all, delete-orphan", order_by="EmployeeLevel.effective_from"
    )
    availability: Mapped[list["Availability"]] = relationship(
        back_populates="employee", cascade="all, delete-orphan"
    )
    time_off: Mapped[list["TimeOff"]] = relationship(
        back_populates="employee", cascade="all, delete-orphan"
    )

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


class BreakInterval(Base):
    __tablename__ = "break_intervals"

    id: Mapped[int] = mapped_column(primary_key=True)
    work_session_id: Mapped[int] = mapped_column(ForeignKey("work_sessions.id"))
    start: Mapped[datetime] = mapped_column(DateTime)
    end: Mapped[datetime] = mapped_column(DateTime)
    paid: Mapped[bool] = mapped_column(Boolean, default=True)

    session: Mapped[WorkSession] = relationship(back_populates="breaks")


class NoShow(Base):
    __tablename__ = "no_shows"
    __table_args__ = (UniqueConstraint("employee_id", "date", "reason"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"))
    date: Mapped[date] = mapped_column(Date)
    reason: Mapped[str] = mapped_column(String, default="")

    employee: Mapped[Employee] = relationship()
