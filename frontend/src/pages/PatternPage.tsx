import { useEffect, useState } from 'react'
import {
  api,
  fmtMin,
  minToTimeInput,
  parseTimeToMin,
  WEEKDAYS,
  type Level,
  type ShiftBlock,
  type ShiftTemplate,
} from '../api'

export default function PatternPage() {
  const [blocks, setBlocks] = useState<ShiftBlock[]>([])
  const [templates, setTemplates] = useState<ShiftTemplate[]>([])
  const [levels, setLevels] = useState<Level[]>([])
  const [blockModal, setBlockModal] = useState<{ block?: ShiftBlock } | null>(null)
  const [placeModal, setPlaceModal] = useState<{ template?: ShiftTemplate; weekday: number } | null>(null)
  const [error, setError] = useState('')

  const load = () => {
    api<ShiftBlock[]>('/api/blocks').then(setBlocks).catch((e) => setError(e.message))
    api<ShiftTemplate[]>('/api/templates').then(setTemplates).catch((e) => setError(e.message))
    api<Level[]>('/api/levels').then(setLevels).catch(() => {})
  }
  useEffect(load, [])

  return (
    <div>
      <h1>Weekly shift pattern</h1>
      <p className="subtitle">
        Define shift blocks (start/end time) once, then place how many of each block every weekday
        needs, with staff counts per level. Future weeks are generated from this pattern until you
        change it — weeks already generated or hand-edited keep their own shifts.
      </p>
      {error && <div className="error">{error}</div>}

      <div className="panel">
        <div className="row" style={{ marginBottom: 8 }}>
          <h2 style={{ margin: 0 }}>Shift blocks</h2>
          <div className="grow" />
          <button className="small" onClick={() => setBlockModal({})}>+ block</button>
        </div>
        <div className="row">
          {blocks.map((b) => (
            <span className="pill" key={b.id} style={{ padding: '6px 12px', cursor: 'pointer' }}
              title="Click to edit"
              onClick={() => setBlockModal({ block: b })}>
              <b>{b.name}</b> {fmtMin(b.start_min)}–{fmtMin(b.end_min)}
            </span>
          ))}
          {blocks.length === 0 && <span className="muted">No blocks yet — create "Open", "Mid", "Close"…</span>}
        </div>
      </div>

      <div className="board">
        {WEEKDAYS.map((day, wd) => (
          <div className="day-col" key={day}>
            <h3>{day}</h3>
            {templates
              .filter((t) => t.weekday === wd)
              .map((t) => (
                <div className="shift-card" key={t.id}>
                  <div className="when">
                    {t.block.name} <span className="muted">{fmtMin(t.block.start_min)}–{fmtMin(t.block.end_min)}</span>
                    <a
                      href="#"
                      style={{ float: 'right', fontWeight: 400 }}
                      onClick={(e) => { e.preventDefault(); setPlaceModal({ template: t, weekday: wd }) }}
                    >
                      edit
                    </a>
                  </div>
                  <div className="muted">
                    {t.requirements.map((r) => `${r.count}× ${r.level.name}`).join(', ') || 'no staff set'}
                  </div>
                </div>
              ))}
            <button className="small secondary" disabled={!blocks.length} onClick={() => setPlaceModal({ weekday: wd })}>
              + block
            </button>
          </div>
        ))}
      </div>

      {blockModal && (
        <BlockModal
          block={blockModal.block}
          onSaved={() => { setBlockModal(null); load() }}
          onClose={() => setBlockModal(null)}
        />
      )}
      {placeModal && (
        <PlaceModal
          template={placeModal.template}
          weekday={placeModal.weekday}
          blocks={blocks}
          levels={levels}
          onSaved={() => { setPlaceModal(null); load() }}
          onClose={() => setPlaceModal(null)}
        />
      )}
    </div>
  )
}

function BlockModal({
  block,
  onSaved,
  onClose,
}: {
  block?: ShiftBlock
  onSaved: () => void
  onClose: () => void
}) {
  const [name, setName] = useState(block?.name ?? '')
  const [start, setStart] = useState(block ? minToTimeInput(block.start_min) : '06:30')
  const [end, setEnd] = useState(block ? minToTimeInput(block.end_min) : '14:30')
  const [error, setError] = useState('')

  const save = async () => {
    setError('')
    const body = { name, start_min: parseTimeToMin(start), end_min: parseTimeToMin(end) }
    try {
      if (block) await api(`/api/blocks/${block.id}`, { method: 'PUT', body })
      else await api('/api/blocks', { method: 'POST', body })
      onSaved()
    } catch (err) {
      if (err instanceof Error) setError(err.message)
    }
  }

  const remove = async () => {
    if (!block || !confirm('Delete this block?')) return
    try {
      await api(`/api/blocks/${block.id}`, { method: 'DELETE' })
      onSaved()
    } catch (err) {
      if (err instanceof Error) setError(err.message)
    }
  }

  return (
    <div className="overlay" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h2>{block ? 'Edit block' : 'New shift block'}</h2>
        <div className="row">
          <div className="grow"><label>Name</label><input value={name} onChange={(e) => setName(e.target.value)} placeholder="Open / Mid / Close" /></div>
          <div><label>Start</label><input type="time" value={start} onChange={(e) => setStart(e.target.value)} /></div>
          <div><label>End</label><input type="time" value={end} onChange={(e) => setEnd(e.target.value)} /></div>
        </div>
        <p className="muted" style={{ marginTop: 8 }}>
          Editing a block changes future weeks generated from the pattern; already-generated weeks
          keep their shifts.
        </p>
        {error && <div className="error">{error}</div>}
        <div className="row" style={{ marginTop: 14 }}>
          {block && <button className="danger" onClick={remove}>Delete</button>}
          <div className="grow" />
          <button className="secondary" onClick={onClose}>Cancel</button>
          <button onClick={save} disabled={!name.trim()}>Save</button>
        </div>
      </div>
    </div>
  )
}

function PlaceModal({
  template,
  weekday,
  blocks,
  levels,
  onSaved,
  onClose,
}: {
  template?: ShiftTemplate
  weekday: number
  blocks: ShiftBlock[]
  levels: Level[]
  onSaved: () => void
  onClose: () => void
}) {
  const [blockId, setBlockId] = useState(template?.block.id ?? blocks[0]?.id ?? 0)
  const [counts, setCounts] = useState<Record<number, number>>(() => {
    const c: Record<number, number> = {}
    template?.requirements.forEach((r) => { c[r.level_id] = r.count })
    return c
  })
  const [error, setError] = useState('')

  const save = async () => {
    setError('')
    const requirements = Object.entries(counts)
      .filter(([, n]) => n > 0)
      .map(([level_id, count]) => ({ level_id: Number(level_id), count }))
    const body = { weekday, block_id: blockId, requirements }
    try {
      if (template) await api(`/api/templates/${template.id}`, { method: 'PUT', body })
      else await api('/api/templates', { method: 'POST', body })
      onSaved()
    } catch (err) {
      if (err instanceof Error) setError(err.message)
    }
  }

  const remove = async () => {
    if (!template || !confirm(`Remove this block from ${WEEKDAYS[weekday]}?`)) return
    await api(`/api/templates/${template.id}`, { method: 'DELETE' })
    onSaved()
  }

  return (
    <div className="overlay" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h2>{WEEKDAYS[weekday]} · place a block</h2>
        <label>Shift block</label>
        <select value={blockId} onChange={(e) => setBlockId(Number(e.target.value))}>
          {blocks.map((b) => (
            <option key={b.id} value={b.id}>
              {b.name} ({fmtMin(b.start_min)}–{fmtMin(b.end_min)})
            </option>
          ))}
        </select>
        <h2>Staff needed per level</h2>
        <table>
          <tbody>
            {levels.map((l) => (
              <tr key={l.id}>
                <td>{l.name}</td>
                <td style={{ width: 90 }}>
                  <input
                    type="number" min={0} max={20} style={{ width: 70 }}
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
          {template && <button className="danger" onClick={remove}>Remove from {WEEKDAYS[weekday]}</button>}
          <div className="grow" />
          <button className="secondary" onClick={onClose}>Cancel</button>
          <button onClick={save}>Save</button>
        </div>
      </div>
    </div>
  )
}
