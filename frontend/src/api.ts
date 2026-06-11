const TOKEN_KEY = 'opm_token'
const ROLE_KEY = 'opm_role'
const EMP_KEY = 'opm_employee_id'

export function getToken() {
  return localStorage.getItem(TOKEN_KEY)
}
export function getRole() {
  return localStorage.getItem(ROLE_KEY)
}
export function getEmployeeId(): number | null {
  const v = localStorage.getItem(EMP_KEY)
  return v ? Number(v) : null
}
export function setSession(token: string, role: string, employeeId: number | null) {
  localStorage.setItem(TOKEN_KEY, token)
  localStorage.setItem(ROLE_KEY, role)
  if (employeeId != null) localStorage.setItem(EMP_KEY, String(employeeId))
  else localStorage.removeItem(EMP_KEY)
}
export function clearSession() {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(ROLE_KEY)
  localStorage.removeItem(EMP_KEY)
}

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

export async function api<T = unknown>(
  path: string,
  opts: { method?: string; body?: unknown; form?: FormData } = {},
): Promise<T> {
  const headers: Record<string, string> = {}
  const token = getToken()
  if (token) headers['Authorization'] = `Bearer ${token}`
  let body: BodyInit | undefined
  if (opts.form) {
    body = opts.form
  } else if (opts.body !== undefined) {
    headers['Content-Type'] = 'application/json'
    body = JSON.stringify(opts.body)
  }
  const res = await fetch(path, { method: opts.method ?? 'GET', headers, body })
  if (!res.ok) {
    let detail = res.statusText
    try {
      const data = await res.json()
      detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail)
    } catch {
      /* keep statusText */
    }
    if (res.status === 401) {
      clearSession()
      window.location.href = '/'
    }
    throw new ApiError(res.status, detail)
  }
  return res.json()
}

// --- shared types mirroring backend schemas ---

export interface Level {
  id: number
  name: string
  rank: number
  counts_for_rating: boolean
}

export interface Employee {
  id: number
  name: string
  payroll_id: string | null
  active: boolean
  max_week_minutes: number
  target_week_minutes: number | null
  availability_confirmed: boolean
  level: Level | null
}

export interface AvailabilityWindow {
  id?: number
  weekday: number
  start_min: number
  end_min: number
}

export interface TimeOff {
  id: number
  start_date: string
  end_date: string
  reason: string
}

export interface Requirement {
  level_id: number
  count: number
  level: Level
}

export interface Shift {
  id: number
  date: string
  start_min: number
  end_min: number
  name: string
  requirements: Requirement[]
}

export interface Assignment {
  id: number
  shift_id: number
  employee_id: number
  fills_level_id: number
  manual: boolean
  employee: Employee
}

export interface UnfilledSlot {
  shift_id: number
  level_id: number
  level_name: string
  missing: number
}

export interface ScheduleDetail {
  schedule: {
    id: number
    week_start: string
    status: string
    assignments: Assignment[]
  }
  shifts: Shift[]
  unfilled: UnfilledSlot[]
}

export interface Suggestion {
  employee_id: number
  employee_name: string
  level_name: string
  rating: number | null
  tickets: number
  reason: string
  softness: number
}

export interface Rating {
  employee_id: number
  employee_name: string
  level_name: string
  tickets: number
  on_floor_adherence: number
  expected_adherence: number
  raw_plus_minus: number
  plus_minus: number
  shifts_hit_target: number
  shifts_total: number
}

export interface SlaConfig {
  id: number
  target_seconds: number
  adherence_goal: number
  effective_from: string
}

// --- small helpers ---

export function fmtMin(min: number): string {
  const h = Math.floor(min / 60)
  const m = min % 60
  const ampm = h < 12 ? 'am' : 'pm'
  const hh = h % 12 === 0 ? 12 : h % 12
  return m ? `${hh}:${String(m).padStart(2, '0')}${ampm}` : `${hh}${ampm}`
}

export function parseTimeToMin(value: string): number {
  const [h, m] = value.split(':').map(Number)
  return h * 60 + (m || 0)
}

export function minToTimeInput(min: number): string {
  return `${String(Math.floor(min / 60)).padStart(2, '0')}:${String(min % 60).padStart(2, '0')}`
}

export function mondayOf(d: Date): string {
  const copy = new Date(d)
  copy.setDate(copy.getDate() - ((copy.getDay() + 6) % 7))
  return copy.toISOString().slice(0, 10)
}

export function addDays(iso: string, days: number): string {
  const d = new Date(iso + 'T00:00:00')
  d.setDate(d.getDate() + days)
  return d.toISOString().slice(0, 10)
}

export const WEEKDAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
