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


# --- locations ---

class LocationOut(ORMModel):
    id: int
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
    location_id: int | None = None


class EmployeeUpdate(BaseModel):
    name: str | None = None
    level_id: int | None = None
    max_week_minutes: int | None = None
    target_week_minutes: int | None = None
    active: bool | None = None
    skill_ids: list[int] | None = None
    location_id: int | None = None


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
    location: LocationOut | None = None


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


# --- breaks ---

class BreakItemOut(ORMModel):
    id: int
    kind: str
    start_min: int
    end_min: int
    paid: bool


class RosterEntryOut(ORMModel):
    id: int
    date: date
    name: str
    role: str
    start_min: int
    end_min: int
    source: str
    breaks: list[BreakItemOut]


class RosterEntryIn(BaseModel):
    date: date
    name: str
    role: str = ""
    start_min: int
    end_min: int


class BreakDayOut(BaseModel):
    date: date
    roster: list[RosterEntryOut]
    homebase_configured: bool


class BreakItemMove(BaseModel):
    start_min: int


class BreakConfigOut(ORMModel):
    rest_minutes: int
    meal_minutes: int
    edge_pad_minutes: int
    min_gap_minutes: int
    max_concurrent: int
    meal_by_minute: int


class BreakConfigIn(BaseModel):
    rest_minutes: int | None = None
    meal_minutes: int | None = None
    edge_pad_minutes: int | None = None
    min_gap_minutes: int | None = None
    max_concurrent: int | None = None
    meal_by_minute: int | None = None


class BreakRuleOut(ORMModel):
    id: int
    min_shift_minutes: int
    rest_breaks: int
    meal_breaks: int


class BreakRuleIn(BaseModel):
    min_shift_minutes: int
    rest_breaks: int
    meal_breaks: int


# --- Homebase browser sync ---

class HomebaseStatusOut(ORMModel):
    last_attempt_at: datetime | None
    last_success_at: datetime | None
    last_error: str
    session_valid: bool
    hours_rows_last_sync: int
    swaps_rows_last_sync: int


class HoursSnapshotOut(ORMModel):
    employee_name: str
    period_start: date
    period_end: date
    hours: float
    synced_at: datetime
    matched_employee_id: int | None = None


class ShiftSwapOut(ORMModel):
    id: int
    shift_date: date
    start_min: int | None
    end_min: int | None
    released_by: str
    covered_by: str
    role: str
    status: str
    synced_at: datetime
    covered_by_employee_id: int | None = None
    released_by_employee_id: int | None = None


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
