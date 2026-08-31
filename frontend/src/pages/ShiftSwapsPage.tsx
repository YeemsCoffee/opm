import { useEffect, useState } from 'react'
import { api, type HomebaseStatus, type ShiftSwap } from '../api'

export default function ShiftSwapsPage() {
  const [swaps, setSwaps] = useState<ShiftSwap[]>([])
  const [status, setStatus] = useState<HomebaseStatus | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const load = () => {
    setError('')
    api<ShiftSwap[]>('/api/homebase-sync/swaps').then(setSwaps).catch((e) => setError(e.message))
    api<HomebaseStatus>('/api/homebase-sync/status').then(setStatus).catch(() => {})
  }
  useEffect(load, [])

  const syncNow = async () => {
    setBusy(true)
    setError('')
    try {
      await api('/api/homebase-sync/run', { method: 'POST' })
      load()
    } catch (err) {
      if (err instanceof Error) setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div>
      <h1>Shift Swaps</h1>
      <p className="subtitle">
        Shifts that were put out for coverage and picked up, pulled from Homebase's schedule grid
        (the "Open Shift approved" flag) — updated automatically once a day.
      </p>
      {status && !status.session_valid && status.last_attempt_at && (
        <div className="panel" style={{ background: 'var(--warn-bg)', borderColor: 'var(--warn-line)' }}>
          <b>⚠ Homebase session expired.</b> Re-run the login script on the machine that syncs daily.
        </div>
      )}
      <div className="row" style={{ marginBottom: 12 }}>
        <button className="secondary" disabled={busy} onClick={syncNow}>
          {busy ? 'Syncing…' : 'Sync now'}
        </button>
        {status?.last_success_at && (
          <span className="muted">Last synced {new Date(status.last_success_at).toLocaleString()}</span>
        )}
      </div>
      {error && <div className="error">{error}</div>}
      <div className="panel">
        <table>
          <thead>
            <tr><th>Date</th><th>Picked up by</th><th>Status</th></tr>
          </thead>
          <tbody>
            {swaps.map((s) => (
              <tr key={s.id}>
                <td>{s.shift_date}</td>
                <td>
                  {s.covered_by}
                  {!s.covered_by_employee_id && (
                    <span className="pill bad" style={{ marginLeft: 6 }} title="No employee record matches this name">
                      unmatched
                    </span>
                  )}
                </td>
                <td className="muted">{s.status}</td>
              </tr>
            ))}
            {swaps.length === 0 && !error && (
              <tr><td colSpan={3} className="muted">No shift pickups synced yet.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}
