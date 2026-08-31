import { useEffect, useState } from 'react'
import {
  api,
  minToTimeInput,
  parseTimeToMin,
  WEEKDAYS,
  type AvailabilityWindow,
  type Employee,
  type HomebaseStatus,
  type HoursSnapshot,
  type Level,
  type Skill,
  type TimeOff,
} from '../api'

function HomebaseSyncBanner({ status }: { status: HomebaseStatus | null }) {
  if (!status || !status.last_attempt_at) return null
  if (!status.session_valid) {
    return (
      <div className="panel" style={{ background: 'var(--warn-bg)', borderColor: 'var(--warn-line)' }}>
        <b>⚠ Homebase session expired.</b> Hours won't update until someone re-runs the login
        script on the machine that syncs daily.
      </div>
    )
  }
  const synced = status.last_success_at ? new Date(status.last_success_at).toLocaleString() : 'never'
  return (
    <div className="panel muted" style={{ fontSize: 13 }}>
      Homebase hours last synced {synced} ({status.hours_rows_last_sync} employees)
      {status.last_error && <span style={{ color: 'var(--bad)' }}> · {status.last_error}</span>}
    </div>
  )
}

function DaysAvailable({ e }: { e: Employee }) {
  if (!e.availability.length) return <span className="muted">any day</span>
  const days = new Set(e.availability.map((w) => w.weekday))
  return (
    <span title="Days with availability windows">
      {['M', 'T', 'W', 'T', 'F', 'S', 'S'].map((c, i) => (
        <span key={i} style={{ opacity: days.has(i) ? 1 : 0.2, fontWeight: 700, marginRight: 3 }}>
          {c}
        </span>
      ))}
    </span>
  )
}

export default function EmployeesPage() {
  const [employees, setEmployees] = useState<Employee[]>([])
  const [levels, setLevels] = useState<Level[]>([])
  const [skills, setSkills] = useState<Skill[]>([])
  const [homebaseStatus, setHomebaseStatus] = useState<HomebaseStatus | null>(null)
  const [hours, setHours] = useState<HoursSnapshot[]>([])
  const [editing, setEditing] = useState<Employee | null>(null)
  const [adding, setAdding] = useState(false)
  const [error, setError] = useState('')

  const load = () => {
    api<Employee[]>('/api/employees').then(setEmployees).catch((e) => setError(e.message))
    api<Level[]>('/api/levels').then(setLevels).catch(() => {})
    api<Skill[]>('/api/skills').then(setSkills).catch(() => {})
    api<HomebaseStatus>('/api/homebase-sync/status').then(setHomebaseStatus).catch(() => {})
    api<HoursSnapshot[]>('/api/homebase-sync/hours').then(setHours).catch(() => {})
  }
  useEffect(load, [])

  const hoursFor = (name: string) => {
    const rows = hours.filter((h) => h.employee_name.toLowerCase() === name.toLowerCase())
    return rows[0] // already sorted newest period first by the API
  }

  return (
    <div>
      <h1>Employees</h1>
      <p className="subtitle">
        Levels and hours sync automatically from timesheet imports; availability is set here or by
        each employee from their own account.
      </p>
      {error && <div className="error">{error}</div>}
      <HomebaseSyncBanner status={homebaseStatus} />
      <div className="panel">
        <div className="row" style={{ marginBottom: 10 }}>
          <div className="grow" />
          <button onClick={() => setAdding(true)}>+ Add employee</button>
        </div>
        <table>
          <thead>
            <tr>
              <th>Name</th><th>Level</th><th>Skills</th><th>Hours (Homebase)</th>
              <th>Max hrs/wk</th><th>Target hrs/wk</th>
              <th>Days available</th><th>Availability</th><th>Status</th><th />
            </tr>
          </thead>
          <tbody>
            {employees.map((e) => {
              const h = hoursFor(e.name)
              return (
              <tr key={e.id}>
                <td>{e.name}</td>
                <td><span className="pill">{e.level?.name ?? '—'}</span></td>
                <td>
                  {e.skills.length
                    ? e.skills.map((s) => <span className="pill" key={s.id} style={{ marginRight: 3 }}>{s.name}</span>)
                    : <span className="muted">—</span>}
                </td>
                <td>
                  {h
                    ? <span title={`Period ${h.period_start} – ${h.period_end}, synced ${new Date(h.synced_at).toLocaleString()}`}>{h.hours.toFixed(1)}</span>
                    : <span className="muted">—</span>}
                </td>
                <td>{(e.max_week_minutes / 60).toFixed(0)}</td>
                <td>{e.target_week_minutes != null ? (e.target_week_minutes / 60).toFixed(0) : <span className="muted">—</span>}</td>
                <td><DaysAvailable e={e} /></td>
                <td>
                  {e.availability_confirmed
                    ? <span className="pill good">confirmed</span>
                    : <span className="pill bad" title="Treated as fully available until set">unconfirmed</span>}
                </td>
                <td>{e.active ? 'active' : <span className="muted">inactive</span>}</td>
                <td><button className="small secondary" onClick={() => setEditing(e)}>Edit</button></td>
              </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      {(editing || adding) && (
        <EmployeeModal
          employee={editing ?? undefined}
          levels={levels}
          allSkills={skills}
          onSkillsChanged={() => api<Skill[]>('/api/skills').then(setSkills).catch(() => {})}
          onClose={() => { setEditing(null); setAdding(false) }}
          onSaved={() => { setEditing(null); setAdding(false); load() }}
        />
      )}
    </div>
  )
}

function EmployeeModal({
  employee,
  levels,
  allSkills,
  onSkillsChanged,
  onClose,
  onSaved,
}: {
  employee?: Employee
  levels: Level[]
  allSkills: Skill[]
  onSkillsChanged: () => void
  onClose: () => void
  onSaved: () => void
}) {
  const [name, setName] = useState(employee?.name ?? '')
  const [levelId, setLevelId] = useState(employee?.level?.id ?? levels[0]?.id ?? 0)
  const [maxHours, setMaxHours] = useState(employee ? employee.max_week_minutes / 60 : 40)
  const [targetHours, setTargetHours] = useState(
    employee?.target_week_minutes != null ? employee.target_week_minutes / 60 : '',
  )
  const [active, setActive] = useState(employee?.active ?? true)
  const [skillIds, setSkillIds] = useState<number[]>(employee?.skills.map((s) => s.id) ?? [])
  const [newSkill, setNewSkill] = useState('')
  const [error, setError] = useState('')

  const addSkill = async () => {
    if (!newSkill.trim()) return
    try {
      const created = await api<Skill>('/api/skills', { method: 'POST', body: { name: newSkill.trim() } })
      setSkillIds([...skillIds, created.id])
      setNewSkill('')
      onSkillsChanged()
    } catch (err) {
      if (err instanceof Error) setError(err.message)
    }
  }

  const save = async () => {
    setError('')
    const body = {
      name,
      level_id: levelId,
      max_week_minutes: Math.round(Number(maxHours) * 60),
      target_week_minutes: targetHours === '' ? null : Math.round(Number(targetHours) * 60),
      active,
      skill_ids: skillIds,
    }
    try {
      if (employee) await api(`/api/employees/${employee.id}`, { method: 'PATCH', body })
      else await api('/api/employees', { method: 'POST', body })
      onSaved()
    } catch (err) {
      if (err instanceof Error) setError(err.message)
    }
  }

  return (
    <div className="overlay" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h2>{employee ? `Edit ${employee.name}` : 'New employee'}</h2>
        <div className="row">
          <div className="grow"><label>Name</label><input value={name} onChange={(e) => setName(e.target.value)} /></div>
          <div>
            <label>Level</label>
            <select value={levelId} onChange={(e) => setLevelId(Number(e.target.value))}>
              {levels.map((l) => <option key={l.id} value={l.id}>{l.name}</option>)}
            </select>
          </div>
        </div>
        <div className="row" style={{ marginTop: 10 }}>
          <div><label>Max hours/week</label><input type="number" min={0} max={80} value={maxHours} onChange={(e) => setMaxHours(Number(e.target.value))} /></div>
          <div><label>Target hours/week (fairness)</label><input type="number" min={0} max={80} value={targetHours} onChange={(e) => setTargetHours(e.target.value)} placeholder="optional" /></div>
          <div>
            <label>Active</label>
            <input type="checkbox" checked={active} onChange={(e) => setActive(e.target.checked)} />
          </div>
        </div>
        <h2>Skills</h2>
        <div className="row">
          {allSkills.map((s) => (
            <label key={s.id} style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 13, color: 'var(--ink)' }}>
              <input
                type="checkbox"
                checked={skillIds.includes(s.id)}
                onChange={(e) =>
                  setSkillIds(e.target.checked ? [...skillIds, s.id] : skillIds.filter((id) => id !== s.id))
                }
              />
              {s.name}
            </label>
          ))}
          <input
            value={newSkill}
            onChange={(e) => setNewSkill(e.target.value)}
            placeholder="new skill (e.g. dialing)"
            style={{ width: 170 }}
            onKeyDown={(e) => e.key === 'Enter' && addSkill()}
          />
          <button className="small secondary" onClick={addSkill} disabled={!newSkill.trim()}>+ skill</button>
        </div>
        {employee && <AvailabilityEditor employeeId={employee.id} />}
        {employee && <TimeOffEditor employeeId={employee.id} />}
        {error && <div className="error">{error}</div>}
        <div className="row" style={{ marginTop: 14 }}>
          <div className="grow" />
          <button className="secondary" onClick={onClose}>Cancel</button>
          <button onClick={save} disabled={!name}>Save</button>
        </div>
      </div>
    </div>
  )
}

export function AvailabilityEditor({ employeeId }: { employeeId: number }) {
  const [windows, setWindows] = useState<AvailabilityWindow[]>([])
  const [loaded, setLoaded] = useState(false)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    api<AvailabilityWindow[]>(`/api/employees/${employeeId}/availability`)
      .then((w) => { setWindows(w); setLoaded(true) })
      .catch((e) => setError(e.message))
  }, [employeeId])

  const save = async () => {
    setError('')
    setSaved(false)
    try {
      await api(`/api/employees/${employeeId}/availability`, { method: 'PUT', body: windows })
      setSaved(true)
    } catch (err) {
      if (err instanceof Error) setError(err.message)
    }
  }

  if (!loaded) return <p className="muted">Loading availability…</p>
  return (
    <div>
      <h2>Weekly availability</h2>
      {windows.length === 0 && (
        <p className="muted">
          No windows set — treated as <b>fully available</b> (unconfirmed). Add windows to restrict.
        </p>
      )}
      {WEEKDAYS.map((day, wd) => (
        <div className="row" key={day} style={{ marginBottom: 4 }}>
          <span style={{ width: 90, fontSize: 13 }}>{day}</span>
          {windows.filter((w) => w.weekday === wd).map((w, i) => (
            <span className="row" key={i}>
              <input
                type="time"
                value={minToTimeInput(w.start_min)}
                onChange={(e) => setWindows(windows.map((x) => (x === w ? { ...x, start_min: parseTimeToMin(e.target.value) } : x)))}
              />
              –
              <input
                type="time"
                value={minToTimeInput(w.end_min)}
                onChange={(e) => setWindows(windows.map((x) => (x === w ? { ...x, end_min: parseTimeToMin(e.target.value) } : x)))}
              />
              <button className="small danger" onClick={() => setWindows(windows.filter((x) => x !== w))}>×</button>
            </span>
          ))}
          <button
            className="small secondary"
            onClick={() => setWindows([...windows, { weekday: wd, start_min: 390, end_min: 1080 }])}
          >
            + window
          </button>
        </div>
      ))}
      {error && <div className="error">{error}</div>}
      {saved && <div className="ok">Availability saved</div>}
      <button className="small" style={{ marginTop: 6 }} onClick={save}>Save availability</button>
    </div>
  )
}

export function TimeOffEditor({ employeeId }: { employeeId: number }) {
  const [rows, setRows] = useState<TimeOff[]>([])
  const [start, setStart] = useState('')
  const [end, setEnd] = useState('')
  const [reason, setReason] = useState('')
  const [error, setError] = useState('')

  const load = () => {
    api<TimeOff[]>(`/api/employees/${employeeId}/time-off`).then(setRows).catch((e) => setError(e.message))
  }
  useEffect(load, [employeeId])

  const add = async () => {
    setError('')
    try {
      await api(`/api/employees/${employeeId}/time-off`, {
        method: 'POST',
        body: { start_date: start, end_date: end || start, reason },
      })
      setStart(''); setEnd(''); setReason('')
      load()
    } catch (err) {
      if (err instanceof Error) setError(err.message)
    }
  }

  const remove = async (id: number) => {
    await api(`/api/employees/${employeeId}/time-off/${id}`, { method: 'DELETE' })
    load()
  }

  return (
    <div>
      <h2>Time off</h2>
      {rows.map((t) => (
        <div className="row" key={t.id} style={{ marginBottom: 4, fontSize: 13 }}>
          <span>{t.start_date}{t.end_date !== t.start_date ? ` → ${t.end_date}` : ''}</span>
          <span className="muted">{t.reason}</span>
          <button className="small danger" onClick={() => remove(t.id)}>×</button>
        </div>
      ))}
      <div className="row">
        <input type="date" value={start} onChange={(e) => setStart(e.target.value)} />
        <input type="date" value={end} onChange={(e) => setEnd(e.target.value)} title="end (optional)" />
        <input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="reason" />
        <button className="small" disabled={!start} onClick={add}>Add</button>
      </div>
      {error && <div className="error">{error}</div>}
    </div>
  )
}
