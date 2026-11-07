import { useMemo, useState } from 'react'
import { BrainCircuit, Search, Trash2, X } from 'lucide-react'
import type { MemoryRecord, MemoryStats } from '../lib/api'
import { Modal } from './Dialogs'

const KIND_LABELS: Record<string, string> = {
  preference: 'Preference',
  fact: 'Environment',
  'code-habit': 'Habit',
  event: 'Past issue',
}

/**
 * What the system remembers.
 *
 * Recall is silent by design, which makes an audit surface non-negotiable: the
 * user must be able to read every note stored about them and delete any of it.
 */
export function MemoryDialog({
  open,
  stats,
  memories,
  onClose,
  onForget,
  onPurge,
  busy,
}: {
  open: boolean
  stats: MemoryStats | null
  memories: MemoryRecord[]
  onClose: () => void
  onForget: (id: string) => void
  onPurge: () => void
  busy?: boolean
}) {
  const [query, setQuery] = useState('')
  const [confirmPurge, setConfirmPurge] = useState(false)

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase()
    if (!needle) return memories
    return memories.filter((memory) => memory.text.toLowerCase().includes(needle))
  }, [memories, query])

  if (!open) return null

  const enabled = stats?.enabled !== false
  const total = stats?.total ?? memories.length

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
              data-variant="text"
              onClick={() => setConfirmPurge(true)}
              disabled={busy || total === 0}
              title="Delete every stored memory"
            >
              <Trash2 size={14} /> Forget all
            </button>
          </div>

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
                ? 'Nothing remembered yet. Preferences and environment facts are picked up as you work.'
                : 'No note matches that filter.'}
            </span>
          ) : (
            <div className="stack" style={{ maxHeight: '46vh', overflowY: 'auto' }}>
              {filtered.map((memory) => (
                <div key={memory.id} className="tool-row" style={{ alignItems: 'flex-start' }}>
                  <span className="badge" data-status="proposed">
                    {KIND_LABELS[memory.kind] || memory.kind}
                  </span>
                  <span className="tool-row__name" style={{ whiteSpace: 'normal' }}>
                    {memory.text}
                  </span>
                  <span className="text-muted" style={{ fontSize: '0.7rem', whiteSpace: 'nowrap' }}>
                    {new Date(memory.last_used_at * 1000).toLocaleDateString()}
                  </span>
                  <button
                    className="onion-iconbtn"
                    onClick={() => onForget(memory.id)}
                    disabled={busy}
                    title="Forget this note"
                    aria-label="Forget this note"
                    style={{ width: 26, height: 26 }}
                  >
                    <X size={13} />
                  </button>
                </div>
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