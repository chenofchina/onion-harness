import { useMemo, useState } from 'react'
import {
  BrainCircuit,
  ChevronRight,
  CircleAlert,
  CircleCheck,
  Loader,
  RotateCcw,
  ShieldAlert,
  ShieldCheck,
  Sparkles,
  SquareTerminal,
  Undo2,
  Wrench,
  X,
} from 'lucide-react'
import type { ChangeRecord, OnionEvent, Revision } from '../lib/api'
import { Markdown } from './Markdown'
import { ApprovalCard } from './TaskActions'

/* ---------------------------------------------------------------- helpers */

export function executorMeta(name?: string) {
  switch ((name || '').toLowerCase()) {
    case 'python':
      return { label: 'Python', glyph: 'py' }
    case 'powershell':
      return { label: 'PowerShell', glyph: 'ps' }
    case 'cmd':
      return { label: 'Command Prompt', glyph: 'cmd' }
    default:
      return { label: name || 'Tool', glyph: 'tl' }
  }
}

export function ExecutorBadge({ executor }: { executor?: string }) {
  const meta = executorMeta(executor)
  return (
    <span className="exec-badge" title={meta.label} aria-label={meta.label}>
      <ExecutorGlyph glyph={meta.glyph} />
    </span>
  )
}

/** Mono, stroke-drawn executor glyphs (no emoji, no external assets). */
export function ExecutorGlyph({ glyph }: { glyph: string }) {
  if (glyph === 'py') {
    return (
      <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6">
        <path d="M12 3.2c-2.6 0-4.1.9-4.1 2.7v2.2h4.4v.9H6.4c-1.9 0-3.1 1.4-3.1 4s1.1 3.9 3 3.9h1.5v-2.4c0-2 1.6-3.5 3.7-3.5h3.6c1.7 0 3-1.2 3-3V6c0-1.8-1.5-2.8-4.1-2.8h-.9z" strokeLinejoin="round" />
        <path d="M12 20.8c2.6 0 4.1-.9 4.1-2.7v-2.2h-4.4v-.9h5.9c1.9 0 3.1-1.4 3.1-4s-1.1-3.9-3-3.9h-1.5v2.4c0 2-1.6 3.5-3.7 3.5H8.9c-1.7 0-3 1.2-3 3V18c0 1.8 1.5 2.8 4.1 2.8h.9z" strokeLinejoin="round" opacity="0.55" />
        <circle cx="9.6" cy="6.1" r="0.85" fill="currentColor" stroke="none" />
        <circle cx="14.4" cy="17.9" r="0.85" fill="currentColor" stroke="none" />
      </svg>
    )
  }
  if (glyph === 'ps') {
    return (
      <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6">
        <rect x="2.6" y="4.6" width="18.8" height="14.8" rx="2.6" />
        <path d="M6.4 9.6l2.6 2.4-2.6 2.4" strokeLinecap="round" strokeLinejoin="round" />
        <path d="M11.4 14.6h6" strokeLinecap="round" />
        <path d="M17.4 8.2v3.2" strokeLinecap="round" opacity="0.55" />
      </svg>
    )
  }
  if (glyph === 'cmd') {
    return (
      <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6">
        <rect x="2.6" y="4.6" width="18.8" height="14.8" rx="2.6" />
        <path d="M6.2 8.6h4.2M6.2 12h6.4M6.2 15.4h3" strokeLinecap="round" opacity="0.75" />
        <rect x="14.2" y="13.6" width="3.6" height="4" rx="0.7" fill="currentColor" stroke="none" />
      </svg>
    )
  }
  return <Wrench size={16} />
}

/* ------------------------------------------------------------ user bubble */

export function UserCard({ text }: { text: string }) {
  return <div className="card bubble-user">{text}</div>
}

/* -------------------------------------------------------------- assistant */

export function AssistantCard({ text }: { text: string }) {
  return (
    <div className="card card-assistant">
      <Markdown text={text} />
    </div>
  )
}

/* -------------------------------------------------------------- reasoning */

export function ReasoningCard({
  text,
  step,
  live,
}: {
  text: string
  step?: number
  live?: boolean
}) {
  const [open, setOpen] = useState(false)
  const label = live ? 'Thinking' : step ? `Reasoning · step ${step}` : 'Reasoning'

  return (
    <div className="card card-reasoning">
      <button className="card__header" data-open={open} onClick={() => setOpen((v) => !v)}>
        <span className="card__header-icon">
          <BrainCircuit size={16} />
        </span>
        <span className="card__header-label">
          {live ? (
            <>
              Thinking
              <span className="onion-thinking" aria-hidden>
                <i />
                <i />
                <i />
              </span>
            </>
          ) : (
            label
          )}
        </span>
        <span className="card__header-meta">{text.length} chars</span>
        <ChevronRight size={15} className="card__chevron" />
      </button>
      {open && <div className="card__body">{text}</div>}
    </div>
  )
}

/* -------------------------------------------------------------- tool call */

type ToolCallCardProps = {
  name: string
  executor?: string
  preview: string
  status: string
  arguments?: Record<string, any>
  result?: { content: string; status: string; is_error?: boolean; verified?: boolean } | null
  defaultOpen?: boolean
}

export function ToolCallCard({
  name,
  executor,
  preview,
  status,
  arguments: args,
  result,
  defaultOpen = false,
}: ToolCallCardProps) {
  const [open, setOpen] = useState(defaultOpen)
  const meta = executorMeta(executor)
  const effectiveStatus = result?.status || status
  const code = typeof args?.code === 'string' ? args.code : ''
  const summary = preview || (code ? code.split('\n')[0] : name)

  return (
    <div className="card card-tool">
      <button className="card-tool__head" onClick={() => setOpen((v) => !v)} aria-expanded={open}>
        <ExecutorBadge executor={executor} />
        <span className="card-tool__main">
          <span className="card-tool__title">
            {meta.label}
            <StatusBadge status={effectiveStatus} />
            {result?.verified && <VerifiedBadge />}
          </span>
          <span className="card-tool__sub">{summary}</span>
        </span>
        <ChevronRight
          size={15}
          className="card__chevron"
          style={{ transform: open ? 'rotate(90deg)' : undefined }}
        />
      </button>

      {open && (
        <div style={{ borderTop: '1px solid var(--md-sys-color-outline-variant)' }}>
          {code && (
            <div className="card-tool__output" style={{ maxHeight: 220 }}>
              <div className="text-muted" style={{ marginBottom: 4 }}>
                command
              </div>
              <pre className="code-block">{code}</pre>
            </div>
          )}
          {result ? (
            <div
              className="card-tool__output"
              data-error={result.status === 'failed' ? 'true' : 'false'}
            >
              <pre className="code-block">{result.content || '(no output)'}</pre>
            </div>
          ) : (
            <div className="card-tool__output">
              <span className="row" style={{ gap: 6 }}>
                <Loader size={13} className="spin" /> running…
              </span>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

/** Strict Coding badge: this tool result is verified evidence. */
export function VerifiedBadge() {
  return (
    <span className="badge badge--verified" title="Verified by a tool call (Strict Coding)">
      <ShieldCheck size={10} />
      Verified
    </span>
  )
}

export function StatusBadge({ status }: { status: string }) {
  const label =
    status === 'running'
      ? 'running'
      : status === 'success'
        ? 'success'
        : status === 'failed'
          ? 'failed'
          : status === 'timeout'
            ? 'timeout'
            : status === 'cancelled'
              ? 'cancelled'
              : status === 'proposed'
                ? 'pending'
                : status
  const icon =
    status === 'running' ? (
      <Loader size={10} className="spin" />
    ) : status === 'success' ? (
      <CircleCheck size={10} />
    ) : status === 'failed' || status === 'timeout' ? (
      <CircleAlert size={10} />
    ) : (
      <Sparkles size={10} />
    )
  return (
    <span className="badge" data-status={status}>
      {icon}
      {label}
    </span>
  )
}

/* ---------------------------------------------------------- tool result */

export function ToolResultCard({
  name,
  executor,
  content,
  status,
}: {
  name: string
  executor?: string
  content: string
  status: string
}) {
  const lines = useMemo(() => content.split('\n'), [content])
  const preview = lines.slice(0, 3).join('\n')
  const truncated = lines.length > 3
  const [open, setOpen] = useState(false)
  const isError = status === 'failed'

  return (
    <div className="card card-tool">
      <button className="card-tool__head" onClick={() => setOpen((v) => !v)}>
        <ExecutorBadge executor={executor} />
        <span className="card-tool__main">
          <span className="card-tool__title">
            Result · {name}
            <StatusBadge status={status} />
          </span>
          <span className="card-tool__sub">
            {lines.filter((l) => l.trim()).length} lines
            {truncated && !open ? ' · click to expand' : ''}
          </span>
        </span>
        <ChevronRight
          size={15}
          className="card__chevron"
          style={{ transform: open ? 'rotate(90deg)' : undefined }}
        />
      </button>
      <div
        className="card-tool__output"
        data-error={isError ? 'true' : 'false'}
        style={{ maxHeight: open ? 460 : 92, overflow: 'auto', borderTop: '1px solid var(--md-sys-color-outline-variant)' }}
      >
        <pre className="code-block">{open ? content : preview || '(no output)'}</pre>
      </div>
    </div>
  )
}

/* --------------------------------------------------------- change cards */

export function ChangeProposalCard({
  change,
  onApply,
  onReject,
  busy,
}: {
  change: ChangeRecord
  onApply: (id: string, opts: { acceptIrreversible: boolean; params: Record<string, any> }) => void
  onReject: (id: string) => void
  busy?: boolean
}) {
  const [acceptAck, setAcceptAck] = useState(false)
  const [editing, setEditing] = useState(false)
  const [paramsText, setParamsText] = useState(() => JSON.stringify(change.params, null, 2))
  const [paramError, setParamError] = useState<string | null>(null)

  const blocked = change.requires_ack && !acceptAck

  const handleApply = () => {
    let params: Record<string, any> = {}
    if (editing) {
      try {
        params = JSON.parse(paramsText)
      } catch {
        setParamError('Parameters must be valid JSON')
        return
      }
      if (typeof params !== 'object' || params === null || Array.isArray(params)) {
        setParamError('Parameters must be a JSON object')
        return
      }
    }
    setParamError(null)
    onApply(change.id, { acceptIrreversible: acceptAck, params })
  }

  return (
    <div className="card card-change" data-status={change.status}>
      <div className="card-change__head">
        <span className="card__header-icon">
          <Wrench size={17} />
        </span>
        <span className="card-change__title">{change.label}</span>
        <span className="card-change__target">{change.target}</span>
        <span className="spacer" />
        <StatusBadge status="proposed" />
      </div>

      <div className="card-change__body">
        {change.reason && <div className="card-change__reason">{change.reason}</div>}

        {change.diff.map((hunk, index) => (
          <div key={index} className="stack" style={{ gap: 4 }}>
            <span className="text-muted" style={{ fontFamily: 'var(--onion-mono)' }}>
              {hunk.path}
            </span>
            <DiffBlock before={hunk.before} after={hunk.after} />
          </div>
        ))}

        {change.impact.length > 0 && (
          <div className="stack" style={{ gap: 5 }}>
            <span className="text-muted">Affects</span>
            <div className="impact-list">
              {change.impact.map((item) => (
                <span key={item} className="impact-chip">
                  {item}
                </span>
              ))}
            </div>
          </div>
        )}

        {editing && (
          <div className="field">
            <label>Parameters (JSON)</label>
            <textarea
              className="code-block"
              style={{
                minHeight: 130,
                borderRadius: 10,
                border: '1px solid var(--md-sys-color-outline-variant)',
                background: 'var(--md-sys-color-surface-container-lowest)',
                color: 'var(--md-sys-color-on-surface)',
                padding: 10,
                resize: 'vertical',
              }}
              value={paramsText}
              onChange={(e) => setParamsText(e.target.value)}
            />
            {paramError && <span className="field__hint" style={{ color: 'var(--md-sys-color-error)' }}>{paramError}</span>}
          </div>
        )}

        {!change.reversible && (
          <div className="card-change__warn">
            <ShieldAlert size={15} style={{ flexShrink: 0, marginTop: 1 }} />
            <span>
              This change has external side effects and cannot be rolled back automatically.
            </span>
          </div>
        )}
      </div>

      <div className="card-change__actions">
        {change.requires_ack && (
          <label className="checkbox-row">
            <input
              type="checkbox"
              checked={acceptAck}
              onChange={(e) => setAcceptAck(e.target.checked)}
            />
            Accept non-reversible change
          </label>
        )}
        <span className="spacer" style={{ flex: 1 }} />
        <button className="btn" data-variant="text" onClick={() => setEditing((v) => !v)} disabled={busy}>
          {editing ? 'Discard edits' : 'Modify'}
        </button>
        <button className="btn" data-variant="outlined" onClick={() => onReject(change.id)} disabled={busy}>
          <X size={14} /> Reject
        </button>
        <button className="btn" data-variant="filled" onClick={handleApply} disabled={busy || blocked}>
          <CircleCheck size={14} /> Confirm & apply
        </button>
      </div>
    </div>
  )
}

export function ChangeAppliedCard({
  revision,
  onRevert,
  busy,
}: {
  revision: Revision
  onRevert: (version: number) => void
  busy?: boolean
}) {
  const [open, setOpen] = useState(false)
  const change = revision.change
  const reverted = revision.reverted_at !== null

  return (
    <div className="card card-change" data-status={reverted ? 'reverted' : 'applied'}>
      <button className="card-change__head" style={{ width: '100%', border: 'none', background: 'transparent', cursor: 'pointer', textAlign: 'left' }} onClick={() => setOpen((v) => !v)}>
        <span className="card__header-icon" style={{ color: reverted ? 'var(--md-sys-color-on-surface-variant)' : 'var(--onion-level-green)' }}>
          {reverted ? <Undo2 size={17} /> : <CircleCheck size={17} />}
        </span>
        <span className="card-change__title">{change.label}</span>
        <span className="card-change__target">{change.target}</span>
        <span className="spacer" style={{ flex: 1 }} />
        <span className="version-tag">v{revision.version}</span>
        {reverted && <span className="impact-chip">reverted</span>}
        <ChevronRight size={15} className="card__chevron" style={{ transform: open ? 'rotate(90deg)' : undefined }} />
      </button>
      {open && (
        <div className="card-change__body">
          <div className="text-muted">{change.reason}</div>
          {change.diff.map((hunk, index) => (
            <DiffBlock key={index} before={hunk.before} after={hunk.after} />
          ))}
          {!reverted && (
            <div className="row">
              <span className="spacer" style={{ flex: 1 }} />
              <button className="btn" data-variant="outlined" onClick={() => onRevert(revision.version)} disabled={busy}>
                <RotateCcw size={14} /> Roll back from v{revision.version}
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export function ChangeNoticeCard({ change, reason }: { change?: ChangeRecord; reason?: string }) {
  return (
    <div className="card card-change" data-status="rejected">
      <div className="card-change__head">
        <span className="card__header-icon" style={{ color: 'var(--md-sys-color-on-surface-variant)' }}>
          <Undo2 size={17} />
        </span>
        <span className="card-change__title">Change {change ? change.label : 'rejected'}</span>
        {change && <span className="card-change__target">{change.target}</span>}
        <span className="spacer" style={{ flex: 1 }} />
        <StatusBadge status="cancelled" />
      </div>
      <div className="card-change__body">
        <div className="text-muted">{reason || 'The user declined this proposal.'}</div>
      </div>
    </div>
  )
}

export function ChangeRevertedCard({ versions }: { versions: number[] }) {
  return (
    <div className="card card-change" data-status="reverted">
      <div className="card-change__head">
        <span className="card__header-icon" style={{ color: 'var(--md-sys-color-on-surface-variant)' }}>
          <RotateCcw size={17} />
        </span>
        <span className="card-change__title">Rolled back</span>
        <span className="spacer" style={{ flex: 1 }} />
        <span className="version-tag">{versions.map((v) => `v${v}`).join(', ')}</span>
      </div>
      <div className="card-change__body">
        <div className="text-muted">
          Chain rollback removed {versions.length} change{versions.length === 1 ? '' : 's'}. They
          stay in history and can be redone.
        </div>
      </div>
    </div>
  )
}

export function ErrorCard({ message, code }: { message: string; code?: string }) {
  return (
    <div className="card card-change" data-status="rejected" style={{ borderColor: 'color-mix(in srgb, var(--md-sys-color-error) 45%, transparent)' }}>
      <div className="card-change__head">
        <span className="card__header-icon" style={{ color: 'var(--md-sys-color-error)' }}>
          <CircleAlert size={17} />
        </span>
        <span className="card-change__title">Runtime error</span>
        {code && <span className="impact-chip">{code}</span>}
      </div>
      <div className="card-change__body">
        <div className="card-change__reason">{message}</div>
      </div>
    </div>
  )
}

/* -------------------------------------------------------------- diff view */

function DiffBlock({ before, after }: { before: any; after: any }) {
  const lines = useMemo(() => buildDiffLines(before, after), [before, after])
  return (
    <div className="diff">
      {lines.map((line, index) => (
        <div key={index} className="diff__row" data-kind={line.kind}>
          <span className="diff__sign">
            {line.kind === 'add' ? '+' : line.kind === 'del' ? '−' : ' '}
          </span>
          <span>{line.text}</span>
        </div>
      ))}
    </div>
  )
}

function buildDiffLines(before: any, after: any): { kind: 'add' | 'del' | 'ctx'; text: string }[] {
  // For objects that carry a text payload (prompt segments, skill bodies),
  // diff the text itself — line-level diffs of escaped JSON are unreadable.
  const beforeText = extractText(before)
  const afterText = extractText(after)
  if (beforeText !== null && afterText !== null && (beforeText || afterText)) {
    return lineDiff(beforeText.split('\n'), afterText.split('\n'))
  }

  const beforeLines = prettyLines(before)
  const afterLines = prettyLines(after)
  if (beforeLines.length === 0 && afterLines.length === 0) {
    return [{ kind: 'ctx', text: '(no structured change)' }]
  }
  if (beforeLines.length === 0) {
    return afterLines.map((text) => ({ kind: 'add' as const, text }))
  }
  if (afterLines.length === 0) {
    return beforeLines.map((text) => ({ kind: 'del' as const, text }))
  }

  return lineDiff(beforeLines, afterLines)
}

/** Returns the diffable text of a change target, or null when it has none. */
function extractText(value: any): string | null {
  if (!value || typeof value !== 'object') return null
  for (const key of ['content', 'text', 'description']) {
    if (typeof value[key] === 'string') {
      const siblings = Object.entries(value)
        .filter(([name, item]) => name !== key && typeof item !== 'object')
        .map(([name, item]) => `${name}: ${item}`)
      return siblings.length ? `${siblings.join(' · ')}\n\n${value[key]}` : value[key]
    }
  }
  return null
}

function lineDiff(
  beforeLines: string[],
  afterLines: string[],
): { kind: 'add' | 'del' | 'ctx'; text: string }[] {
  // Simple LCS keeps the preview readable without pulling in a diff library.
  const n = beforeLines.length
  const m = afterLines.length
  const table: number[][] = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0))
  for (let i = n - 1; i >= 0; i -= 1) {
    for (let j = m - 1; j >= 0; j -= 1) {
      table[i][j] =
        beforeLines[i] === afterLines[j]
          ? table[i + 1][j + 1] + 1
          : Math.max(table[i + 1][j], table[i][j + 1])
    }
  }
  const out: { kind: 'add' | 'del' | 'ctx'; text: string }[] = []
  let i = 0
  let j = 0
  while (i < n && j < m) {
    if (beforeLines[i] === afterLines[j]) {
      out.push({ kind: 'ctx', text: beforeLines[i] })
      i += 1
      j += 1
    } else if (table[i + 1][j] >= table[i][j + 1]) {
      out.push({ kind: 'del', text: beforeLines[i] })
      i += 1
    } else {
      out.push({ kind: 'add', text: afterLines[j] })
      j += 1
    }
  }
  while (i < n) {
    out.push({ kind: 'del', text: beforeLines[i] })
    i += 1
  }
  while (j < m) {
    out.push({ kind: 'add', text: afterLines[j] })
    j += 1
  }
  const limit = 40
  if (out.length > limit) {
    return [...out.slice(0, limit), { kind: 'ctx', text: `… ${out.length - limit} more lines` }]
  }
  return out
}

function prettyLines(value: any): string[] {
  if (value === null || value === undefined) return []
  if (typeof value === 'string') {
    return value.split('\n')
  }
  if (typeof value === 'number' || typeof value === 'boolean') {
    return [String(value)]
  }
  try {
    return JSON.stringify(value, null, 2).split('\n')
  } catch {
    return [String(value)]
  }
}

/* ------------------------------------------------------- event dispatcher */

export type EventActions = {
  onApply: (id: string, opts: { acceptIrreversible: boolean; params: Record<string, any> }) => void
  onReject: (id: string) => void
  onRevert: (version: number) => void
  onResolveApproval?: (approvalId: string, approved: boolean) => void
  busyChangeId?: string | null
}

export function renderEvent(
  event: OnionEvent,
  resultsByCall: Map<string, OnionEvent>,
  callIds: Set<string>,
  actions: EventActions,
): React.ReactNode | null {
  const p = event.payload || {}
  switch (event.type) {
    case 'user-input':
      return <UserCard text={p.text || ''} />
    case 'assistant':
      return <AssistantCard text={p.text || ''} />
    case 'reasoning':
      return <ReasoningCard text={p.text || ''} step={p.step} />
    case 'tool-call': {
      const result = resultsByCall.get(p.call_id)
      return (
        <ToolCallCard
          name={p.name}
          executor={p.executor}
          preview={p.preview}
          status={p.status}
          arguments={p.arguments}
          result={
            result
              ? {
                  content: result.payload.content,
                  status: result.payload.status,
                  is_error: result.payload.is_error,
                  verified: result.payload.verified,
                }
              : null
          }
        />
      )
    }
    case 'tool-approval': {
      if (!actions.onResolveApproval) return null
      return (
        <ApprovalCard
          event={event}
          onResolve={(approved) => actions.onResolveApproval!(p.call_id, approved)}
        />
      )
    }
    case 'tool-result': {
      // Folded into the originating tool-call card. Render standalone only
      // when that card is absent (e.g. a truncated or mid-stream history).
      if (callIds.has(p.call_id)) return null
      return <ToolResultCard name={p.name} executor={p.executor} content={p.content || ''} status={p.status} />
    }
    case 'change-proposal':
      return p.change ? (
        <ChangeProposalCard
          change={p.change}
          onApply={actions.onApply}
          onReject={actions.onReject}
          busy={actions.busyChangeId === p.change.id}
        />
      ) : null
    case 'change-applied':
      return p.revision ? (
        <ChangeAppliedCard
          revision={p.revision}
          onRevert={actions.onRevert}
          busy={Boolean(actions.busyChangeId)}
        />
      ) : null
    case 'change-rejected':
      return <ChangeNoticeCard change={p.change} reason={p.reason} />
    case 'change-reverted':
      return <ChangeRevertedCard versions={p.versions || []} />
    case 'change-redone':
      return p.revision ? <ChangeAppliedCard revision={p.revision} onRevert={actions.onRevert} /> : null
    case 'error':
      return <ErrorCard message={p.message || 'Unknown error'} code={p.code} />
    case 'task-done':
      // A failed run already emitted an `error` event with the same message;
      // rendering it again here produced two identical error cards. The task
      // record still carries the failure for the monitor and history.
      return null
    default:
      return null
  }
}

export function ToolMonitorIcon({ executor }: { executor?: string }) {
  return <SquareTerminal size={14} />
}