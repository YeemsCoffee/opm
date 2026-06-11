import { useEffect, useState } from 'react'
import { api, type Rating } from '../api'

export default function RatingsPage() {
  const [ratings, setRatings] = useState<Rating[]>([])
  const [start, setStart] = useState('')
  const [end, setEnd] = useState('')
  const [error, setError] = useState('')

  const load = () => {
    setError('')
    const params = new URLSearchParams()
    if (start) params.set('start', start)
    if (end) params.set('end', end)
    api<Rating[]>(`/api/ratings?${params}`).then(setRatings).catch((e) => setError(e.message))
  }
  useEffect(load, []) // eslint-disable-line react-hooks/exhaustive-deps

  const maxAbs = Math.max(1, ...ratings.map((r) => Math.abs(r.plus_minus)))

  return (
    <div>
      <h1>Plus / Minus</h1>
      <p className="subtitle">
        How ticket-time adherence moves when each person is on the floor, vs. what's expected for
        those hours. Small samples are shrunk toward 0 — trust the number more as tickets grow.
      </p>
      <div className="row panel">
        <div><label>From</label><input type="date" value={start} onChange={(e) => setStart(e.target.value)} /></div>
        <div><label>To</label><input type="date" value={end} onChange={(e) => setEnd(e.target.value)} /></div>
        <button onClick={load}>Refresh</button>
        <span className="muted">Defaults to the configured lookback window ending today.</span>
      </div>
      {error && <div className="error">{error}</div>}
      <div className="panel">
        <table>
          <thead>
            <tr>
              <th>Employee</th><th>Level</th><th>+/-</th><th></th><th>On-floor</th>
              <th>Expected</th><th>Tickets</th><th>Shifts hit 90%</th>
            </tr>
          </thead>
          <tbody>
            {ratings.map((r) => (
              <tr key={r.employee_id}>
                <td>{r.employee_name}</td>
                <td><span className="pill">{r.level_name}</span></td>
                <td style={{ fontWeight: 700, color: r.plus_minus >= 0 ? 'var(--good)' : 'var(--bad)' }}>
                  {r.plus_minus > 0 ? '+' : ''}{r.plus_minus.toFixed(1)}
                </td>
                <td>
                  <div className="bar-track">
                    <div
                      className="bar"
                      style={{
                        background: r.plus_minus >= 0 ? 'var(--good)' : 'var(--bad)',
                        left: r.plus_minus >= 0 ? '50%' : `${50 - (Math.abs(r.plus_minus) / maxAbs) * 50}%`,
                        width: `${(Math.abs(r.plus_minus) / maxAbs) * 50}%`,
                      }}
                    />
                  </div>
                </td>
                <td>{(r.on_floor_adherence * 100).toFixed(1)}%</td>
                <td className="muted">{(r.expected_adherence * 100).toFixed(1)}%</td>
                <td className={r.tickets < 500 ? 'muted' : ''} title={r.tickets < 500 ? 'Small sample — low confidence' : ''}>
                  {r.tickets}{r.tickets < 500 ? ' ⚠' : ''}
                </td>
                <td>{r.shifts_hit_target}/{r.shifts_total}</td>
              </tr>
            ))}
            {ratings.length === 0 && !error && (
              <tr><td colSpan={8} className="muted">No data — import a kitchen report and timesheets first.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}
