import { useEffect, useState } from 'react'
import {
  api,
  minToTimeInput,
  parseTimeToMin,
  WEEKDAYS,
  type AvailabilityWindow,
  type Employee,
  type Level,
  type TimeOff,
} from '../api'

export default function EmployeesPage() {
  const [employees, setEmployees] = useState<Employee[]>([])
  const [levels, setLevels] = useState<Level[]>([])
  const [editing, setEditing] = useState<Employee | null>(null)
  const [adding, setAdding] = useState(false)
  const [error, setError] = useState('')

  const load = () => {
    api<Employee[]>('/api/employees').then(setEmployees).catch((e) => setError(e.message))
    api<Level[]>('/api/levels').then(setLevels).catch(() => {})
  }
  useEffect(load, [])

  return (
    <div>
      <h1>Employees</h1>
      <p className="subtitle">
        Levels and hours sync automatically from timesheet imports; availability is set here or by
        each employee from their own account.
      </p>
      {error && <div className="error">{error}</div>}
      <div className="panel">
        <div className="row" style={{ marginBottom: 10 }}>
          <div className="grow" />
          <button onClick={() => setAdding(true)}>+ Add employee</button>
        </div>
        <table>
          <thead>
            <tr>
              <th>Name</th><th>Level</th><th>Max hrs/wk</th><th>Target hrs/wk</th>
              <th>Availability</th><th>Status</th><th />
            </tr>
          </thead>
          <tbody>
            {employees.map((e) => (
              <tr key={e.id}>
                <td>{e.name}</td>
                <td><span className="pill">{e.level?.name ?? '—'}</span></td>
                <td>{(e.max_week_minutes / 60).toFixed(0)}</td>
                <td>{e.target_week_minutes != null ? (e.target_week_minutes / 60).toFixed(0) : <span className="muted">—</span>}</td>
                <td>
                  {e.availability_confirmed
                    ? <span className="pill good">confirmed</span>
                    : <span className="pill bad" title="Treated as fully available until set">unconfirmed</span>}
                </td>
                <td>{e.active ? 'active' : <span className="muted">inactive</span>}</td>
                <td><button className="small secondary" onClick={() => setEditing(e)}>Edit</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {(editing || adding) && (
        <EmployeeModal
          employee={editing ?? undefined}
          levels={levels}
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
  onClose,
  onSaved,
}: {
  employee?: Employee
  levels: Level[]
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
  const [error, setError] = useState('')

  const save = async () => {
    setError('')
    const body = {
      name,
      level_id: levelId,
      max_week_minutes: Math.round(Number(maxHours) * 60),
      target_week_minutes: targetHours === '' ? null : Math.round(Number(targetHours) * 60),
      active,
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
