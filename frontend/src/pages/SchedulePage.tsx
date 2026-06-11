import { useCallback, useEffect, useState } from 'react'
import {
  addDays,
  api,
  ApiError,
  fmtMin,
  getRole,
  mondayOf,
  minToTimeInput,
  parseTimeToMin,
  WEEKDAYS,
  type Employee,
  type Level,
  type ScheduleDetail,
  type Shift,
  type Suggestion,
  type UnfilledSlot,
} from '../api'

export default function SchedulePage() {
  const isManager = getRole() === 'manager'
  const [week, setWeek] = useState(() => mondayOf(new Date()))
  const [detail, setDetail] = useState<ScheduleDetail | null>(null)
  const [shifts, setShifts] = useState<Shift[]>([])
  const [levels, setLevels] = useState<Level[]>([])
  const [employees, setEmployees] = useState<Employee[]>([])
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const [slotModal, setSlotModal] = useState<{ slot: UnfilledSlot; shift: Shift } | null>(null)
  const [shiftModal, setShiftModal] = useState<{ shift?: Shift; date: string } | null>(null)

  const load = useCallback(async () => {
    setError('')
    setNotice('')
    try {
      const s = await api<Shift[]>(`/api/shifts?week_start=${week}`)
      setShifts(s)
    } catch (err) {
      if (err instanceof Error) setError(err.message)
      return
    }
    try {
      setDetail(await api<ScheduleDetail>(`/api/schedules/week/${week}`))
    } catch (err) {
      setDetail(null)
      if (err instanceof ApiError && err.status !== 404 && err.status !== 403) setError(err.message)
    }
  }, [week])

  useEffect(() => {
    load()
    if (isManager) {
      api<Level[]>('/api/levels').then(setLevels).catch(() => {})
      api<Employee[]>('/api/employees').then(setEmployees).catch(() => {})
    }
  }, [load, isManager])

  const generate = async () => {
    setBusy(true)
    setError('')
    try {
      const d = await api<ScheduleDetail>(`/api/schedules/generate?week_start=${week}`, {
        method: 'POST',
      })
      setDetail(d)
      setNotice(
        d.unfilled.length
          ? `Draft generated — ${d.unfilled.length} slot(s) need attention (highlighted below)`
          : 'Draft generated — every slot filled',
      )
    } catch (err) {
      if (err instanceof Error) setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  const publish = async () => {
    if (!detail) return
    if (detail.unfilled.length && !confirm('There are unfilled slots. Publish anyway?')) return
    try {
      setDetail(await api<ScheduleDetail>(`/api/schedules/${detail.schedule.id}/publish`, { method: 'POST' }))
      setNotice('Schedule published — employees can now see it')
    } catch (err) {
      if (err instanceof Error) setError(err.message)
    }
  }

  const copyLastWeek = async () => {
    try {
      await api(`/api/shifts/copy-week`, {
        method: 'POST',
        body: { from_week: addDays(week, -7), to_week: week },
      })
      await load()
    } catch (err) {
      if (err instanceof Error) setError(err.message)
    }
  }

  const removeAssignment = async (assignmentId: number) => {
    if (!detail) return
    try {
      setDetail(
        await api<ScheduleDetail>(
          `/api/schedules/${detail.schedule.id}/assignments/${assignmentId}`,
          { method: 'DELETE' },
        ),
      )
    } catch (err) {
      if (err instanceof Error) setError(err.message)
    }
  }

  const days = Array.from({ length: 7 }, (_, i) => addDays(week, i))
  const assignmentsByShift = new Map<number, ScheduleDetail['schedule']['assignments']>()
  detail?.schedule.assignments.forEach((a) => {
    const list = assignmentsByShift.get(a.shift_id) ?? []
    list.push(a)
    assignmentsByShift.set(a.shift_id, list)
  })
  const unfilledByShift = new Map<number, UnfilledSlot[]>()
  detail?.unfilled.forEach((u) => {
    const list = unfilledByShift.get(u.shift_id) ?? []
    list.push(u)
    unfilledByShift.set(u.shift_id, list)
  })

  return (
    <div>
      <h1>Schedule</h1>
      <p className="subtitle">
        Week of {week}
        {detail && (
          <span className={`pill ${detail.schedule.status === 'published' ? 'good' : ''}`} style={{ marginLeft: 8 }}>
            {detail.schedule.status}
          </span>
        )}
      </p>
      <div className="row" style={{ marginBottom: 14 }}>
        <button className="secondary" onClick={() => setWeek(addDays(week, -7))}>← Prev</button>
        <button className="secondary" onClick={() => setWeek(mondayOf(new Date()))}>Today</button>
        <button className="secondary" onClick={() => setWeek(addDays(week, 7))}>Next →</button>
        <div className="grow" />
        {isManager && (
          <>
            <button className="secondary" onClick={copyLastWeek}>Copy last week's shifts</button>
            <button onClick={generate} disabled={busy}>
              {busy ? 'Solving…' : detail ? 'Re-generate' : 'Generate schedule'}
            </button>
            {detail && detail.schedule.status !== 'published' && (
              <button onClick={publish}>Publish</button>
            )}
          </>
        )}
      </div>
      {error && <div className="error">{error}</div>}
      {notice && <div className="ok">{notice}</div>}
      {isManager && detail && detail.warnings.length > 0 && (
        <div className="panel" style={{ background: 'var(--warn-bg)', borderColor: 'var(--warn-line)' }}>
          <b>⚠ Limit overrides on this schedule</b>
          {detail.warnings.map((w, i) => (
            <div key={i} style={{ fontSize: 13, marginTop: 4 }}>{w.message}</div>
          ))}
        </div>
      )}
      {!isManager && !detail && <div className="panel muted">No published schedule for this week yet.</div>}

      <div className="board">
        {days.map((d, i) => (
          <div className="day-col" key={d}>
            <h3>
              {WEEKDAYS[i].slice(0, 3)} {d.slice(5)}
            </h3>
            {shifts
              .filter((s) => s.date === d)
              .map((s) => (
                <div className="shift-card" key={s.id}>
                  <div className="when">
                    {fmtMin(s.start_min)}–{fmtMin(s.end_min)} {s.name && <span className="muted">{s.name}</span>}
                    {isManager && (
                      <a
                        href="#"
                        style={{ float: 'right', fontWeight: 400 }}
                        onClick={(e) => { e.preventDefault(); setShiftModal({ shift: s, date: d }) }}
                      >
                        edit
                      </a>
                    )}
                  </div>
                  <div className="muted" style={{ marginBottom: 4 }}>
                    needs: {s.requirements.map((r) => `${r.count}× ${r.level.name}`).join(', ') || '—'}
                  </div>
                  {(assignmentsByShift.get(s.id) ?? []).map((a) => (
                    <div className={`assn ${a.manual ? 'manual' : ''}`} key={a.id} title={a.manual ? 'Manual assignment' : 'Auto-assigned'}>
                      <span className="who">{a.employee.name}</span>
                      <span className="lvl">{a.employee.level?.name}</span>
                      {isManager && (
                        <button className="small danger" onClick={() => removeAssignment(a.id)}>×</button>
                      )}
                    </div>
                  ))}
                  {(unfilledByShift.get(s.id) ?? []).map((u) => (
                    <div
                      className="slot-empty"
                      key={u.level_id}
                      onClick={() => isManager && setSlotModal({ slot: u, shift: s })}
                      title={isManager ? 'Click for suggestions' : undefined}
                    >
                      {u.missing}× {u.level_name} unfilled{isManager ? ' — suggest' : ''}
                    </div>
                  ))}
                </div>
              ))}
            {isManager && (
              <button className="small secondary" onClick={() => setShiftModal({ date: d })}>
                + shift
              </button>
            )}
          </div>
        ))}
      </div>

      {slotModal && detail && (
        <SuggestionModal
          scheduleId={detail.schedule.id}
          slot={slotModal.slot}
          shift={slotModal.shift}
          employees={employees}
          onAssigned={(d) => { setDetail(d); setSlotModal(null) }}
          onClose={() => setSlotModal(null)}
        />
      )}
      {shiftModal && (
        <ShiftModal
          shift={shiftModal.shift}
          date={shiftModal.date}
          levels={levels}
          onSaved={() => { setShiftModal(null); load() }}
          onClose={() => setShiftModal(null)}
        />
      )}
    </div>
  )
}

function SuggestionModal({
  scheduleId,
  slot,
  shift,
  employees,
  onAssigned,
  onClose,
}: {
  scheduleId: number
  slot: UnfilledSlot
  shift: Shift
  employees: Employee[]
  onAssigned: (d: ScheduleDetail) => void
  onClose: () => void
}) {
  const [suggestions, setSuggestions] = useState<Suggestion[] | null>(null)
  const [error, setError] = useState('')
  const [manualId, setManualId] = useState('')

  useEffect(() => {
    api<Suggestion[]>(
      `/api/schedules/${scheduleId}/suggestions?shift_id=${slot.shift_id}&level_id=${slot.level_id}`,
    )
      .then(setSuggestions)
      .catch((e) => setError(e.message))
  }, [scheduleId, slot])

  const assign = async (employeeId: number) => {
    setError('')
    try {
      onAssigned(
        await api<ScheduleDetail>(`/api/schedules/${scheduleId}/assignments`, {
          method: 'POST',
          body: { shift_id: slot.shift_id, employee_id: employeeId, fills_level_id: slot.level_id },
        }),
      )
    } catch (err) {
      if (err instanceof Error) setError(err.message)
    }
  }

  return (
    <div className="overlay" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h2>
          Fill {slot.level_name} · {shift.date} {fmtMin(shift.start_min)}–{fmtMin(shift.end_min)}
        </h2>
        <p className="muted">
          The solver couldn't fill this slot within the rules. Each suggestion below bends exactly
          the rule shown — assigning is your call.
        </p>
        {error && <div className="error">{error}</div>}
        {suggestions === null && <p className="muted">Loading…</p>}
        {suggestions !== null && suggestions.length === 0 && (
          <p className="muted">No viable candidates — every option is on time off or has a conflicting shift.</p>
        )}
        {suggestions !== null && suggestions.length > 0 && (
          <table>
            <thead>
              <tr><th>Employee</th><th>Level</th><th>+/-</th><th>Why they're not auto-assigned</th><th /></tr>
            </thead>
            <tbody>
              {suggestions.map((s) => (
                <tr key={s.employee_id}>
                  <td>{s.employee_name}</td>
                  <td>{s.level_name}</td>
                  <td>{s.rating == null ? <span className="muted">no data</span> : (s.rating > 0 ? '+' : '') + s.rating.toFixed(1)}</td>
                  <td className="muted">{s.reason}</td>
                  <td><button className="small" onClick={() => assign(s.employee_id)}>Assign</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <h2>Or assign anyone</h2>
        <div className="row">
          <select value={manualId} onChange={(e) => setManualId(e.target.value)}>
            <option value="">choose employee…</option>
            {employees.filter((e) => e.active).map((e) => (
              <option key={e.id} value={e.id}>{e.name} ({e.level?.name ?? '—'})</option>
            ))}
          </select>
          <button disabled={!manualId} onClick={() => assign(Number(manualId))}>Assign</button>
        </div>
      </div>
    </div>
  )
}

function ShiftModal({
  shift,
  date,
  levels,
  onSaved,
  onClose,
}: {
  shift?: Shift
  date: string
  levels: Level[]
  onSaved: () => void
  onClose: () => void
}) {
  const [start, setStart] = useState(shift ? minToTimeInput(shift.start_min) : '06:30')
  const [end, setEnd] = useState(shift ? minToTimeInput(shift.end_min) : '14:30')
  const [name, setName] = useState(shift?.name ?? '')
  const [counts, setCounts] = useState<Record<number, number>>(() => {
    const c: Record<number, number> = {}
    shift?.requirements.forEach((r) => { c[r.level_id] = r.count })
    return c
  })
  const [error, setError] = useState('')

  const save = async () => {
    setError('')
    const requirements = Object.entries(counts)
      .filter(([, n]) => n > 0)
      .map(([level_id, count]) => ({ level_id: Number(level_id), count }))
    const body = { date, start_min: parseTimeToMin(start), end_min: parseTimeToMin(end), name, requirements }
    try {
      if (shift) await api(`/api/shifts/${shift.id}`, { method: 'PUT', body })
      else await api('/api/shifts', { method: 'POST', body })
      onSaved()
    } catch (err) {
      if (err instanceof Error) setError(err.message)
    }
  }

  const remove = async () => {
    if (!shift || !confirm('Delete this shift?')) return
    await api(`/api/shifts/${shift.id}`, { method: 'DELETE' })
    onSaved()
  }

  return (
    <div className="overlay" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h2>{shift ? 'Edit shift' : 'New shift'} · {date}</h2>
        <div className="row">
          <div><label>Start</label><input type="time" value={start} onChange={(e) => setStart(e.target.value)} /></div>
          <div><label>End</label><input type="time" value={end} onChange={(e) => setEnd(e.target.value)} /></div>
          <div className="grow"><label>Label (optional)</label><input value={name} onChange={(e) => setName(e.target.value)} placeholder="open / close…" /></div>
        </div>
        <h2>Staff needed per level</h2>
        <table>
          <tbody>
            {levels.map((l) => (
              <tr key={l.id}>
                <td>{l.name}</td>
                <td style={{ width: 90 }}>
                  <input
                    type="number"
                    min={0}
                    max={20}
                    style={{ width: 70 }}
                    value={counts[l.id] ?? 0}
                    onChange={(e) => setCounts({ ...counts, [l.id]: Number(e.target.value) })}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {error && <div className="error">{error}</div>}
        <div className="row" style={{ marginTop: 14 }}>
          {shift && <button className="danger" onClick={remove}>Delete shift</button>}
          <div className="grow" />
          <button className="secondary" onClick={onClose}>Cancel</button>
          <button onClick={save}>Save</button>
        </div>
      </div>
    </div>
  )
}
