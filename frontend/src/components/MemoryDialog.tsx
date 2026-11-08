import { useMemo, useState } from 'react'
import { BrainCircuit, Check, Pencil, Plus, Search, Trash2, X } from 'lucide-react'
import type { MemoryRecord, MemoryStats } from '../lib/api'
import { Modal } from './Dialogs'

const KIND_LABELS: Record<string, string> = {
  preference: 'Preference',
  fact: 'Environment',
  'code-habit': 'Habit',
  event: 'Past issue',
}

const KINDS = Object.keys(KIND_LABELS)

/** One note, either displayed or being edited in place. */
function MemoryRow({
  memory,
  editing,
  busy,
  onEdit,
  onCancel,
  onSave,
  onForget,
}: {
  memory: MemoryRecord
  editing: boolean
  busy?: boolean
  onEdit: () => void
  onCancel: () => void
  onSave: (patch: { text: string; kind: string }) => void
  onForget: () => void
}) {
  const [text, setText] = useState(memory.text)
  const [kind, setKind] = useState(memory.kind)

  if (editing) {
    return (
      <div className="memory-row" data-editing>
        <div className="row" style={{ gap: 8, alignItems: 'flex-start' }}>
          <select value={kind} onChange={(event) => setKind(event.target.value)}>
            {KINDS.map((value) => (
              <option key={value} value={value}>
                {KIND_LABELS[value]}
              </option>
            ))}
          </select>
          <textarea
            value={text}
            onChange={(event) => setText(event.target.value)}
            rows={2}
            style={{ flex: 1, resize: 'vertical' }}
            autoFocus
          />
        </div>
        <div className="row" style={{ gap: 6, justifyContent: 'flex-end', marginTop: 6 }}>
          <button className="btn" data-variant="text" onClick={onCancel} disabled={busy}>
            Cancel
          </button>
          <button
            className="btn"
            data-variant="filled"
            onClick={() => onSave({ text: text.trim(), kind })}
            disabled={busy || !text.trim()}
          >
            <Check size={14} /> Save
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="memory-row">
      <span className="badge" data-status="proposed">
        {KIND_LABELS[memory.kind] || memory.kind}
      </span>
      <span className="memory-row__text">{memory.text}</span>
      <span className="text-muted" style={{ fontSize: '0.7rem', whiteSpace: 'nowrap' }}>
        {new Date(memory.last_used_at * 1000).toLocaleDateString()}
      </span>
      <button
        className="onion-iconbtn"
        onClick={onEdit}
        disabled={busy}
        title="Edit this note"
        aria-label="Edit this note"
        style={{ width: 26, height: 26 }}
      >
        <Pencil size={13} />
      </button>
      <button
        className="onion-iconbtn"
        onClick={onForget}
        disabled={busy}
        title="Forget this note"
        aria-label="Forget this note"
        style={{ width: 26, height: 26 }}
      >
        <X size={13} />
      </button>
    </div>
  )
}

/**
 * What the system remembers.
 *
 * Recall is silent by design, which makes an audit surface non-negotiable: the
 * user must be able to read every note stored about them, correct one that is
 * wrong, add one the extractor never noticed, and delete any of them.
 *
 * Memory is injected into the system channel, so an uncorrectable note is worse
 * than useless - it keeps steering the agent with text nobody can fix. Editing is
 * therefore a first-class action here, not a hidden one.
 */
export function MemoryDialog({
  open,
  stats,
  memories,
  onClose,
  onForget,
  onPurge,
  onAdd,
  onUpdate,
  busy,
}: {
  open: boolean
  stats: MemoryStats | null
  memories: MemoryRecord[]
  onClose: () => void
  onForget: (id: string) => void
  onPurge: () => void
  onAdd: (text: string, kind: string) => void
  onUpdate: (id: string, patch: { text: string; kind: string }) => void
  busy?: boolean
}) {
  const [query, setQuery] = useState('')
  const [confirmPurge, setConfirmPurge] = useState(false)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [adding, setAdding] = useState(false)
  const [newText, setNewText] = useState('')
  const [newKind, setNewKind] = useState('preference')

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase()
    if (!needle) return memories
    return memories.filter((memory) => memory.text.toLowerCase().includes(needle))
  }, [memories, query])

  if (!open) return null

  const enabled = stats?.enabled !== false
  const total = stats?.total ?? memories.length

  const submitNew = () => {
    const text = newText.trim()
    if (!text) return
    onAdd(text, newKind)
    setNewText('')
    setAdding(false)
  }

  return (
    <Modal title="Long-term memory" onClose={onClose} wide>
      {!enabled ? (
        <div className="card-change__warn">
          <BrainCircuit size={15} style={{ flexShrink: 0, marginTop: 1 }} />
          <span>
            Long-term memory is disabled: no encryption key is available from the system
            credential manager, and storing memories unencrypted is not offered.
          </span>
        </div>
      ) : (
        <>
          <div className="row" style={{ alignItems: 'center', gap: 8, marginBottom: 12 }}>
            <span className="text-muted">
              {total} note{total === 1 ? '' : 's'} stored, encrypted at rest
              {stats?.by_kind && Object.keys(stats.by_kind).length > 0
                ? ' · ' +
                  Object.entries(stats.by_kind)
                    .map(([kind, count]) => `${KIND_LABELS[kind] || kind} ${count}`)
                    .join(' · ')
                : ''}
            </span>
            <span style={{ flex: 1 }} />
            <button
              className="btn"
              data-variant="tonal"
              onClick={() => {
                setEditingId(null)
                setAdding((current) => !current)
              }}
              disabled={busy}
              title="Add a note by hand"
            >
              <Plus size={14} /> Add
            </button>
            <button
              className="btn"
              data-variant="text"
              onClick={() => setConfirmPurge(true)}
              disabled={busy || total === 0}
              title="Delete every stored memory"
            >
              <Trash2 size={14} /> Forget all
            </button>
          </div>

          {adding && (
            <div className="memory-row" data-editing style={{ marginBottom: 10 }}>
              <div className="row" style={{ gap: 8, alignItems: 'flex-start' }}>
                <select value={newKind} onChange={(event) => setNewKind(event.target.value)}>
                  {KINDS.map((value) => (
                    <option key={value} value={value}>
                      {KIND_LABELS[value]}
                    </option>
                  ))}
                </select>
                <textarea
                  value={newText}
                  onChange={(event) => setNewText(event.target.value)}
                  rows={2}
                  placeholder="What should the agent remember? Write it as a plain sentence."
                  style={{ flex: 1, resize: 'vertical' }}
                  autoFocus
                />
              </div>
              <div className="row" style={{ gap: 6, justifyContent: 'flex-end', marginTop: 6 }}>
                <button className="btn" data-variant="text" onClick={() => setAdding(false)}>
                  Cancel
                </button>
                <button
                  className="btn"
                  data-variant="filled"
                  onClick={submitNew}
                  disabled={busy || !newText.trim()}
                >
                  <Check size={14} /> Save note
                </button>
              </div>
            </div>
          )}

          <div className="field">
            <div className="row" style={{ alignItems: 'center', gap: 8 }}>
              <Search size={14} className="text-muted" />
              <input
                type="text"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Filter stored notes"
                style={{ flex: 1 }}
              />
            </div>
          </div>

          {filtered.length === 0 ? (
            <span className="monitor-empty">
              {memories.length === 0
                ? 'Nothing remembered yet. Preferences and environment facts are picked up as you work, and you can add one above.'
                : 'No note matches that filter.'}
            </span>
          ) : (
            <div className="stack" style={{ maxHeight: '46vh', overflowY: 'auto' }}>
              {filtered.map((memory) => (
                <MemoryRow
                  key={memory.id}
                  memory={memory}
                  busy={busy}
                  editing={editingId === memory.id}
                  onEdit={() => {
                    setAdding(false)
                    setEditingId(memory.id)
                  }}
                  onCancel={() => setEditingId(null)}
                  onSave={(patch) => {
                    onUpdate(memory.id, patch)
                    setEditingId(null)
                  }}
                  onForget={() => onForget(memory.id)}
                />
              ))}
            </div>
          )}
        </>
      )}

      {confirmPurge && (
        <div className="card-change__warn">
          <span>Delete every stored memory? This cannot be undone.</span>
          <span style={{ flex: 1 }} />
          <button className="btn" data-variant="text" onClick={() => setConfirmPurge(false)}>
            Cancel
          </button>
          <button
            className="btn"
            data-variant="filled"
            onClick={() => {
              setConfirmPurge(false)
              onPurge()
            }}
            disabled={busy}
          >
            Delete all
          </button>
        </div>
      )}
    </Modal>
  )
}