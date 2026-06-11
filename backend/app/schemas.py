from datetime import date, datetime

from pydantic import BaseModel, ConfigDict


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- auth ---

class RegisterIn(BaseModel):
    email: str
    password: str
    role: str = "employee"
    employee_id: int | None = None


class LoginIn(BaseModel):
    email: str
    password: str


class TokenOut(BaseModel):
    token: str
    role: str
    employee_id: int | None


class UserOut(ORMModel):
    id: int
    email: str
    role: str
    employee_id: int | None


# --- levels ---

class LevelOut(ORMModel):
    id: int
    name: str
    rank: int
    counts_for_rating: bool


class LevelUpdate(BaseModel):
    rank: int | None = None
    counts_for_rating: bool | None = None


# --- skills ---

class SkillOut(ORMModel):
    id: int
    name: str


class SkillIn(BaseModel):
    name: str


# --- employees ---

class AvailabilityIn(BaseModel):
    weekday: int
    start_min: int
    end_min: int


class AvailabilityOut(ORMModel):
    id: int
    weekday: int
    start_min: int
    end_min: int


class TimeOffIn(BaseModel):
    start_date: date
    end_date: date
    reason: str = ""


class TimeOffOut(ORMModel):
    id: int
    start_date: date
    end_date: date
    reason: str


class EmployeeIn(BaseModel):
    name: str
    level_id: int
    payroll_id: str | None = None
    max_week_minutes: int = 2400
    target_week_minutes: int | None = None
    active: bool = True
    skill_ids: list[int] = []


class EmployeeUpdate(BaseModel):
    name: str | None = None
    level_id: int | None = None
    max_week_minutes: int | None = None
    target_week_minutes: int | None = None
    active: bool | None = None
    skill_ids: list[int] | None = None


class EmployeeOut(ORMModel):
    id: int
    name: str
    payroll_id: str | None
    active: bool
    max_week_minutes: int
    target_week_minutes: int | None
    availability_confirmed: bool
    level: LevelOut | None = None
    skills: list[SkillOut] = []
    availability: list[AvailabilityOut] = []


# --- shifts ---

class RequirementIn(BaseModel):
    level_id: int
    count: int


class RequirementOut(ORMModel):
    level_id: int
    count: int
    level: LevelOut


class ShiftIn(BaseModel):
    date: date
    start_min: int
    end_min: int
    name: str = ""
    requirements: list[RequirementIn] = []


class ShiftOut(ORMModel):
    id: int
    date: date
    start_min: int
    end_min: int
    name: str
    requirements: list[RequirementOut]


# --- shift blocks & weekly pattern ---

class BlockIn(BaseModel):
    name: str
    start_min: int
    end_min: int


class BlockOut(ORMModel):
    id: int
    name: str
    start_min: int
    end_min: int


class TemplateIn(BaseModel):
    weekday: int
    block_id: int
    requirements: list[RequirementIn] = []


class TemplateOut(ORMModel):
    id: int
    weekday: int
    block: BlockOut
    requirements: list[RequirementOut]


# --- schedules ---

class AssignmentOut(ORMModel):
    id: int
    shift_id: int
    employee_id: int
    fills_level_id: int
    manual: bool
    employee: EmployeeOut


class UnfilledSlot(BaseModel):
    shift_id: int
    level_id: int
    level_name: str
    missing: int


class ScheduleOut(ORMModel):
    id: int
    week_start: date
    status: str
    assignments: list[AssignmentOut]


class ScheduleWarning(BaseModel):
    employee_id: int
    employee_name: str
    kind: str  # overtime_day | overtime_week | consecutive_days
    message: str


class ScheduleDetail(BaseModel):
    schedule: ScheduleOut
    shifts: list[ShiftOut]
    unfilled: list[UnfilledSlot]
    warnings: list[ScheduleWarning] = []


class ManualAssignIn(BaseModel):
    shift_id: int
    employee_id: int
    fills_level_id: int


class SuggestionOut(BaseModel):
    employee_id: int
    employee_name: str
    level_name: str
    rating: float | None
    tickets: int
    reason: str
    softness: int


# --- imports ---

class ImportResult(BaseModel):
    created: int
    skipped: int
    details: dict = {}


# --- ratings ---

class RatingOut(BaseModel):
    employee_id: int
    employee_name: str
    level_name: str
    tickets: int
    on_floor_adherence: float
    expected_adherence: float
    raw_plus_minus: float
    plus_minus: float  # shrunk toward 0 for small samples
    shifts_hit_target: int
    shifts_total: int


# --- settings ---

class SlaIn(BaseModel):
    target_seconds: int
    adherence_goal: float = 0.9
    effective_from: date


class SlaOut(ORMModel):
    id: int
    target_seconds: int
    adherence_goal: float
    effective_from: date


class WhatIfOut(BaseModel):
    target_seconds: int
    start: date
    end: date
    tickets: int
    adherence: float


class SolverConfigOut(ORMModel):
    min_rest_minutes: int
    rating_lookback_days: int
    shrinkage_tickets: int
    max_day_minutes: int
    max_consecutive_days: int


class SolverConfigIn(BaseModel):
    min_rest_minutes: int | None = None
    rating_lookback_days: int | None = None
    shrinkage_tickets: int | None = None
    max_day_minutes: int | None = None
    max_consecutive_days: int | None = None
