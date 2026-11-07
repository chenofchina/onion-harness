import { useState } from 'react'
import { Archive, ArrowUp, Check, Pencil, ShieldCheck, X } from 'lucide-react'
import type { OnionEvent, TaskRecord } from '../lib/api'

/** Header row for a user task block, with edit + archive actions. */
export function TaskCardHeader({
  task,
  onEdit,
  onArchive,
  archived,
}: {
  task: TaskRecord
  onEdit: () => void
  onArchive: () => void
  archived?: boolean
}) {
  return (
    <div className="task-card-header">
      <span className="task-card-header__meta">
        {task.mode && <span className="task-card-header__mode">{task.mode}</span>}
        {Boolean(task.edit_count) && (
          <span className="task-card-header__edited">edited {task.edit_count}×</span>
        )}
      </span>
      <span className="spacer" style={{ flex: 1 }} />
      <button
        className="task-card-action"
        onClick={onEdit}
        title="Edit prompt and re-run"
        aria-label="Edit prompt and re-run"
      >
        <Pencil size={13} />
      </button>
      <button
        className="task-card-action"
        onClick={onArchive}
        title={archived ? 'Restore from archive' : 'Archive this task'}
        aria-label={archived ? 'Restore from archive' : 'Archive this task'}
      >
        <Archive size={13} />
      </button>
    </div>
  )
}

/** Inline editor shown instead of the bubble when the user edits a prompt. */
export function TaskEditor({
  initial,
  onCancel,
  onSubmit,
  busy,
}: {
  initial: string
  onCancel: () => void
  onSubmit: (prompt: string) => void
  busy?: boolean
}) {
  const [value, setValue] = useState(initial)
  return (
    <div className="task-editor">
      <textarea
        value={value}
        onChange={(event) => setValue(event.target.value)}
        rows={Math.min(10, Math.max(3, value.split('\n').length))}
        autoFocus
      />
      <div className="task-editor__actions">
        <span className="text-muted">Editing re-runs this task with a new turn.</span>
        <span className="spacer" style={{ flex: 1 }} />
        <button className="btn" data-variant="text" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
        <button
          className="btn"
          data-variant="filled"
          onClick={() => onSubmit(value)}
          disabled={busy || !value.trim()}
        >
          <ArrowUp size={14} /> Re-run
        </button>
      </div>
    </div>
  )
}

/** Approval card: Standard mode asks before a state-changing tool call. */
export function ApprovalCard({
  event,
  onResolve,
}: {
  event: OnionEvent
  onResolve: (approved: boolean) => void
}) {
  const payload = event.payload
  const pending = payload.status === 'awaiting'
  return (
    <div className="card card-approval" data-status={payload.status}>
      <div className="card-approval__head">
        <span className="card__header-icon" style={{ color: 'var(--md-sys-color-tertiary)' }}>
          <ShieldCheck size={17} />
        </span>
        <span className="card-change__title">Approval required</span>
        <span className="impact-chip">{payload.label || payload.name}</span>
        <span className="spacer" style={{ flex: 1 }} />
        {!pending && (
          <span className="badge" data-status={payload.status === 'approved' ? 'success' : 'failed'}>
            {payload.status}
          </span>
        )}
      </div>
      <div className="card-approval__body">
        <div className="card-change__reason">{payload.preview || payload.name}</div>
        {payload.code && (
          <pre className="code-block card-approval__code">{payload.code}</pre>
        )}
      </div>
      {pending && (
        <div className="card-change__actions">
          <span className="spacer" style={{ flex: 1 }} />
          <button className="btn" data-variant="outlined" onClick={() => onResolve(false)}>
            <X size={14} /> Deny
          </button>
          <button className="btn" data-variant="filled" onClick={() => onResolve(true)}>
            <Check size={14} /> Approve & run
          </button>
        </div>
      )}
    </div>
  )
}