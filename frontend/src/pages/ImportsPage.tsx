import { useState } from 'react'
import { api } from '../api'

interface ImportResult {
  created: number
  skipped: number
  details: Record<string, number>
}

function Uploader({ title, hint, endpoint }: { title: string; hint: string; endpoint: string }) {
  const [result, setResult] = useState<ImportResult | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const upload = async (file: File) => {
    setBusy(true)
    setError('')
    setResult(null)
    const form = new FormData()
    form.append('file', file)
    try {
      setResult(await api<ImportResult>(endpoint, { method: 'POST', form }))
    } catch (err) {
      if (err instanceof Error) setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="panel">
      <h2 style={{ marginTop: 0 }}>{title}</h2>
      <p className="muted">{hint}</p>
      <input
        type="file"
        accept=".csv"
        disabled={busy}
        onChange={(e) => e.target.files?.[0] && upload(e.target.files[0])}
      />
      {busy && <p className="muted">Importing…</p>}
      {result && (
        <div className="ok">
          Imported {result.created} new row(s), skipped {result.skipped} duplicate(s)
          {Object.entries(result.details ?? {}).map(([k, v]) => ` · ${k.replace('_', ' ')}: ${v}`)}
        </div>
      )}
      {error && <div className="error">{error}</div>}
    </div>
  )
}

interface TeamImportResult {
  created: number
  skipped: number // "updated" — existing employees matched and refreshed
  details: {
    new_levels?: string[]
    additional_locations_skipped?: number
    levels_kept_unchanged?: number
  }
}

function TeamUploader() {
  const [result, setResult] = useState<TeamImportResult | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const upload = async (file: File) => {
    setBusy(true)
    setError('')
    setResult(null)
    const form = new FormData()
    form.append('file', file)
    try {
      setResult(await api<TeamImportResult>('/api/imports/team', { method: 'POST', form }))
    } catch (err) {
      if (err instanceof Error) setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="panel">
      <h2 style={{ marginTop: 0 }}>Team roster (employees + locations)</h2>
      <p className="muted">
        Homebase's Team export. Creates/updates employees and assigns each one's location. A
        level/role is only set for employees who don't already have one — an existing level from
        timesheet history is never overwritten, since this file's Role Name can be stale.
      </p>
      <input
        type="file"
        accept=".csv"
        disabled={busy}
        onChange={(e) => e.target.files?.[0] && upload(e.target.files[0])}
      />
      {busy && <p className="muted">Importing…</p>}
      {result && (
        <div className="ok">
          <div>{result.created} new employee(s) created, {result.skipped} existing updated.</div>
          {!!result.details.new_levels?.length && (
            <div>
              New level(s) created: {result.details.new_levels.join(', ')} — check these aren't
              duplicates of an existing level under a different name (e.g. "Barista 1" vs "B1").
            </div>
          )}
          {!!result.details.levels_kept_unchanged && (
            <div>{result.details.levels_kept_unchanged} employee(s) already had a level — left unchanged.</div>
          )}
          {!!result.details.additional_locations_skipped && (
            <div>
              {result.details.additional_locations_skipped} additional-location row(s) skipped —
              only each employee's first-listed location was applied.
            </div>
          )}
        </div>
      )}
      {error && <div className="error">{error}</div>}
    </div>
  )
}

export default function ImportsPage() {
  return (
    <div>
      <h1>Imports</h1>
      <p className="subtitle">
        Re-importing the same file is safe — duplicates are skipped automatically.
      </p>
      <Uploader
        title="Kitchen report (tickets)"
        hint="Square KDS export with ticket open/close times. Feeds adherence and plus/minus."
        endpoint="/api/imports/kitchen"
      />
      <Uploader
        title="Timesheets (who worked when)"
        hint="Square Team timesheet export. Creates/updates employees and their levels, work sessions, breaks and no-shows."
        endpoint="/api/imports/timesheets"
      />
      <TeamUploader />
    </div>
  )
}
