import { useEffect, useState } from 'react'
import { api, type Level, type SlaConfig } from '../api'

interface SolverConfig {
  min_rest_minutes: number
  rating_lookback_days: number
  shrinkage_tickets: number
}

interface WhatIf {
  target_seconds: number
  tickets: number
  adherence: number
  start: string
  end: string
}

function secToMinSec(s: number) {
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}

export default function SettingsPage() {
  return (
    <div>
      <h1>Settings</h1>
      <p className="subtitle">Ticket targets, solver rules and level ranks.</p>
      <SlaSection />
      <SolverSection />
      <LevelsSection />
    </div>
  )
}

function SlaSection() {
  const [configs, setConfigs] = useState<SlaConfig[]>([])
  const [target, setTarget] = useState('5:00')
  const [goal, setGoal] = useState(90)
  const [effective, setEffective] = useState('')
  const [whatIf, setWhatIf] = useState<WhatIf | null>(null)
  const [error, setError] = useState('')

  const load = () => api<SlaConfig[]>('/api/settings/sla').then(setConfigs).catch((e) => setError(e.message))
  useEffect(() => { load() }, [])

  const targetSeconds = () => {
    const [m, s] = target.split(':').map(Number)
    return m * 60 + (s || 0)
  }

  const preview = async () => {
    setError('')
    try {
      setWhatIf(await api<WhatIf>(`/api/settings/sla/what-if?target_seconds=${targetSeconds()}`))
    } catch (err) {
      if (err instanceof Error) setError(err.message)
    }
  }

  const save = async () => {
    setError('')
    try {
      await api('/api/settings/sla', {
        method: 'POST',
        body: { target_seconds: targetSeconds(), adherence_goal: goal / 100, effective_from: effective },
      })
      setWhatIf(null)
      load()
    } catch (err) {
      if (err instanceof Error) setError(err.message)
    }
  }

  return (
    <div className="panel">
      <h2 style={{ marginTop: 0 }}>Ticket time targets</h2>
      <p className="muted">
        Targets are effective-dated: every ticket is judged against the target in force when it was
        created, so changing the bar never rewrites history.
      </p>
      <table style={{ marginBottom: 14 }}>
        <thead><tr><th>Effective from</th><th>Close target</th><th>Adherence goal</th></tr></thead>
        <tbody>
          {configs.map((c) => (
            <tr key={c.id}>
              <td>{c.effective_from}</td>
              <td>{secToMinSec(c.target_seconds)}</td>
              <td>{Math.round(c.adherence_goal * 100)}%</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="row">
        <div><label>New target (m:ss)</label><input value={target} onChange={(e) => setTarget(e.target.value)} style={{ width: 80 }} /></div>
        <div><label>Goal %</label><input type="number" min={1} max={100} value={goal} onChange={(e) => setGoal(Number(e.target.value))} style={{ width: 70 }} /></div>
        <div><label>Effective from</label><input type="date" value={effective} onChange={(e) => setEffective(e.target.value)} /></div>
        <button className="secondary" onClick={preview}>Preview last 30 days</button>
        <button disabled={!effective} onClick={save}>Add target</button>
      </div>
      {whatIf && (
        <div className="ok">
          At {secToMinSec(whatIf.target_seconds)}, adherence over {whatIf.start} → {whatIf.end} would
          have been <b>{(whatIf.adherence * 100).toFixed(1)}%</b> ({whatIf.tickets} tickets).
        </div>
      )}
      {error && <div className="error">{error}</div>}
    </div>
  )
}

function SolverSection() {
  const [cfg, setCfg] = useState<SolverConfig | null>(null)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    api<SolverConfig>('/api/settings/solver').then(setCfg).catch((e) => setError(e.message))
  }, [])

  const save = async () => {
    if (!cfg) return
    setSaved(false)
    try {
      setCfg(await api<SolverConfig>('/api/settings/solver', { method: 'PATCH', body: cfg }))
      setSaved(true)
    } catch (err) {
      if (err instanceof Error) setError(err.message)
    }
  }

  if (!cfg) return null
  return (
    <div className="panel">
      <h2 style={{ marginTop: 0 }}>Scheduler rules</h2>
      <div className="row">
        <div>
          <label>Min rest between days (hours)</label>
          <input
            type="number" min={0} max={24} step={0.5}
            value={cfg.min_rest_minutes / 60}
            onChange={(e) => setCfg({ ...cfg, min_rest_minutes: Math.round(Number(e.target.value) * 60) })}
          />
        </div>
        <div>
          <label>Rating lookback (days)</label>
          <input
            type="number" min={7} max={365}
            value={cfg.rating_lookback_days}
            onChange={(e) => setCfg({ ...cfg, rating_lookback_days: Number(e.target.value) })}
          />
        </div>
        <div>
          <label>Shrinkage (tickets to full trust)</label>
          <input
            type="number" min={0} max={5000}
            value={cfg.shrinkage_tickets}
            onChange={(e) => setCfg({ ...cfg, shrinkage_tickets: Number(e.target.value) })}
          />
        </div>
        <button onClick={save}>Save</button>
      </div>
      {saved && <div className="ok">Saved</div>}
      {error && <div className="error">{error}</div>}
    </div>
  )
}

function LevelsSection() {
  const [levels, setLevels] = useState<Level[]>([])
  const [error, setError] = useState('')

  const load = () => api<Level[]>('/api/levels').then(setLevels).catch((e) => setError(e.message))
  useEffect(() => { load() }, [])

  const update = async (l: Level, patch: Partial<Level>) => {
    try {
      await api(`/api/levels/${l.id}`, { method: 'PATCH', body: patch })
      load()
    } catch (err) {
      if (err instanceof Error) setError(err.message)
    }
  }

  return (
    <div className="panel">
      <h2 style={{ marginTop: 0 }}>Levels</h2>
      <p className="muted">
        Rank controls cover-down suggestions: a higher-rank level can be suggested for a lower-rank
        slot (never the reverse, never automatically). "Counts for +/-" off means time worked at
        that level is ignored by ratings (e.g. Training).
      </p>
      <table>
        <thead><tr><th>Level</th><th>Rank</th><th>Counts for +/-</th></tr></thead>
        <tbody>
          {levels.map((l) => (
            <tr key={l.id}>
              <td>{l.name}</td>
              <td>
                <input
                  type="number" style={{ width: 70 }} defaultValue={l.rank}
                  onBlur={(e) => Number(e.target.value) !== l.rank && update(l, { rank: Number(e.target.value) })}
                />
              </td>
              <td>
                <input
                  type="checkbox" checked={l.counts_for_rating}
                  onChange={(e) => update(l, { counts_for_rating: e.target.checked })}
                />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {error && <div className="error">{error}</div>}
    </div>
  )
}
