import { useCallback, useEffect, useState } from 'react'
import {
  addDays,
  api,
  fmtMin,
  minToTimeInput,
  parseTimeToMin,
  type BreakDay,
  type RosterEntry,
} from '../api'

function today(): string {
  return new Date().toISOString().slice(0, 10)
}

export default function BreaksPage() {
  const [day, setDay] = useState(today())
  const [data, setData] = useState<BreakDay | null>(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const [newName, setNewName] = useState('')
  const [newRole, setNewRole] = useState('')
  const [newStart, setNewStart] = useState('06:30')
  const [newEnd, setNewEnd] = useState('14:30')

  const load = useCallback(() => {
    setError('')
    api<BreakDay>(`/api/breaks?date=${day}`).then(setData).catch((e) => setError(e.message))
  }, [day])
  useEffect(load, [load])

  const act = async (fn: () => Promise<BreakDay>, okMsg = '') => {
    setBusy(true)
    setError('')
    setNotice('')
    try {
      setData(await fn())
      if (okMsg) setNotice(okMsg)
    } catch (err) {
      if (err instanceof Error) setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  const importHomebase = () =>
    act(
      () => api<BreakDay>(`/api/breaks/roster/homebase?date=${day}`, { method: 'POST' }),
      'Pulled the day from Homebase',
    )
  const importInternal = () =>
    act(
      () => api<BreakDay>(`/api/breaks/roster/internal?date=${day}`, { method: 'POST' }),
      "Loaded the day from this app's schedule",
    )
  const generate = () =>
    act(
      () => api<BreakDay>(`/api/breaks/generate?date=${day}`, { method: 'POST' }),
      'Break schedule generated',
    )
  const addPerson = () =>
    act(() =>
      api<BreakDay>('/api/breaks/roster/manual', {
        method: 'POST',
        body: {
          date: day,
          name: newName,
          role: newRole,
          start_min: parseTimeToMin(newStart),
          end_min: parseTimeToMin(newEnd),
        },
      }),
    ).then(() => setNewName(''))
  const removePerson = (id: number) =>
    act(() => api<BreakDay>(`/api/breaks/roster/${id}`, { method: 'DELETE' }))
  const moveBreak = (id: number, startMin: number) =>
    act(() => api<BreakDay>(`/api/breaks/items/${id}`, { method: 'PATCH', body: { start_min: startMin } }))

  const roster = data?.roster ?? []
  const dayLabel = new Date(day + 'T00:00:00').toLocaleDateString(undefined, {
    weekday: 'long', month: 'long', day: 'numeric',
  })

  return (
    <div>
      <div className="no-print">
        <h1>Break schedule</h1>
        <p className="subtitle">
          Pull the day's roster, then generate staggered breaks — paid 10s and unpaid 30s per your
          rules, never overlapping coverage, meals before the 5th hour, timed into demand lulls.
        </p>
        <div className="row" style={{ marginBottom: 12 }}>
          <button className="secondary" onClick={() => setDay(addDays(day, -1))}>← Prev day</button>
          <button className="secondary" onClick={() => setDay(today())}>Today</button>
          <button className="secondary" onClick={() => setDay(addDays(day, 1))}>Next day →</button>
          <input type="date" value={day} onChange={(e) => setDay(e.target.value)} />
          <div className="grow" />
          <button
            className="secondary"
            disabled={busy || !data?.homebase_configured}
            title={data?.homebase_configured ? '' : 'Set HOMEBASE_API_KEY and HOMEBASE_LOCATION_UUID (Enterprise plan) to enable'}
            onClick={importHomebase}
          >
            Pull from Homebase{data?.homebase_configured ? '' : ' (not configured)'}
          </button>
          <button className="secondary" disabled={busy} onClick={importInternal}>From app schedule</button>
          <button disabled={busy || roster.length === 0} onClick={generate}>
            {busy ? 'Working…' : 'Generate breaks'}
          </button>
          <button className="secondary" disabled={roster.length === 0} onClick={() => window.print()}>Print</button>
        </div>
        {error && <div className="error">{error}</div>}
        {notice && <div className="ok">{notice}</div>}
        <div className="row panel" style={{ marginBottom: 16 }}>
          <b style={{ fontSize: 13 }}>Add person:</b>
          <input value={newName} onChange={(e) => setNewName(e.target.value)} placeholder="name" style={{ width: 140 }} />
          <input value={newRole} onChange={(e) => setNewRole(e.target.value)} placeholder="role" style={{ width: 90 }} />
          <input type="time" value={newStart} onChange={(e) => setNewStart(e.target.value)} />
          –
          <input type="time" value={newEnd} onChange={(e) => setNewEnd(e.target.value)} />
          <button className="small" disabled={!newName.trim() || busy} onClick={addPerson}>Add</button>
        </div>
      </div>

      <div className="panel print-area">
        <h2 style={{ marginTop: 0 }}>Breaks · {dayLabel}</h2>
        {roster.length === 0 && (
          <p className="muted">No roster for this day yet — pull from Homebase, load the app schedule, or add people above.</p>
        )}
        {roster.length > 0 && <Timeline roster={roster} onMove={moveBreak} onRemove={removePerson} />}
      </div>
    </div>
  )
}

function Timeline({
  roster,
  onMove,
  onRemove,
}: {
  roster: RosterEntry[]
  onMove: (id: number, startMin: number) => void
  onRemove: (id: number) => void
}) {
  const lo = Math.min(...roster.map((r) => r.start_min))
  const hi = Math.max(...roster.map((r) => r.end_min))
  const span = hi - lo
  const pct = (m: number) => `${((m - lo) / span) * 100}%`
  const width = (a: number, b: number) => `${((b - a) / span) * 100}%`
  const hours: number[] = []
  for (let h = Math.ceil(lo / 60); h * 60 <= hi; h++) hours.push(h)

  return (
    <div>
      <div style={{ position: 'relative', height: 18, marginLeft: 190, marginBottom: 4 }}>
        {hours.map((h) => (
          <span key={h} className="muted" style={{ position: 'absolute', left: pct(h * 60), fontSize: 10 }}>
            {fmtMin(h * 60)}
          </span>
        ))}
      </div>
      {roster.map((r) => (
        <div key={r.id} style={{ display: 'flex', alignItems: 'center', marginBottom: 10 }}>
          <div style={{ width: 190, fontSize: 13, paddingRight: 8 }}>
            <b>{r.name}</b> {r.role && <span className="muted">({r.role})</span>}
            <div className="muted" style={{ fontSize: 11 }}>
              {fmtMin(r.start_min)}–{fmtMin(r.end_min)}
              <button
                className="small danger no-print"
                style={{ marginLeft: 6, padding: '0 5px' }}
                onClick={() => onRemove(r.id)}
                title="Remove from roster"
              >
                ×
              </button>
            </div>
          </div>
          <div style={{ flex: 1 }}>
            <div style={{ position: 'relative', height: 26, background: 'var(--bg)', borderRadius: 6 }}>
              <div
                style={{
                  position: 'absolute', left: pct(r.start_min), width: width(r.start_min, r.end_min),
                  top: 3, bottom: 3, background: '#e2d9cd', borderRadius: 5,
                }}
              />
              {r.breaks.map((b) => (
                <div
                  key={b.id}
                  title={`${b.kind === 'meal' ? 'Meal (unpaid 30)' : 'Rest (paid 10)'} ${fmtMin(b.start_min)}–${fmtMin(b.end_min)}`}
                  style={{
                    position: 'absolute', left: pct(b.start_min), width: width(b.start_min, b.end_min),
                    top: 0, bottom: 0, borderRadius: 5,
                    background: b.kind === 'meal' ? 'var(--ink)' : 'var(--accent)',
                  }}
                />
              ))}
            </div>
            <div className="row" style={{ gap: 14, marginTop: 2 }}>
              {r.breaks.map((b) => (
                <span key={b.id} style={{ fontSize: 12 }}>
                  <span
                    style={{
                      display: 'inline-block', width: 9, height: 9, borderRadius: 2, marginRight: 4,
                      background: b.kind === 'meal' ? 'var(--ink)' : 'var(--accent)',
                    }}
                  />
                  {b.kind === 'meal' ? 'meal 30' : 'rest 10'}
                  <input
                    className="no-print"
                    type="time"
                    value={minToTimeInput(b.start_min)}
                    onChange={(e) => onMove(b.id, parseTimeToMin(e.target.value))}
                    style={{ marginLeft: 5, padding: '1px 4px', fontSize: 12 }}
                  />
                  <span className="print-only"> {fmtMin(b.start_min)}–{fmtMin(b.end_min)}</span>
                </span>
              ))}
              {r.breaks.length === 0 && <span className="muted" style={{ fontSize: 12 }}>no breaks entitled</span>}
            </div>
          </div>
        </div>
      ))}
    </div>
  )
}
