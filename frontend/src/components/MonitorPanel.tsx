import { Database, MonitorPlay, X } from 'lucide-react'
import type { ChangeRecord, OnionEvent, UsagePayload } from '../lib/api'
import { StatusBadge, ExecutorBadge } from './EventCards'
import { StarChainGraph } from './StarChainGraph'

type Props = {
  open: boolean
  onClose: () => void
  usage: UsagePayload
  events: OnionEvent[]
  proposals: ChangeRecord[]
  onFocusProposal: (id: string) => void
  taskPrompt: string
  onFocusEvent: (eventId: string) => void
  onOpenPreview: (mode: 'web' | 'terminal' | 'diff') => void
}

export function MonitorPanel({
  open,
  onClose,
  usage,
  events,
  proposals,
  onFocusProposal,
  taskPrompt,
  onFocusEvent,
  onOpenPreview,
}: Props) {
  if (!open) return null
  const ratio = Math.max(0, Math.min(1, usage.ratio ?? 0))
  const level = usage.level ?? (ratio >= 0.85 ? 'red' : ratio >= 0.6 ? 'yellow' : 'green')
  const total = usage.total_tokens ?? 0
  const prompt = usage.prompt_tokens ?? 0
  const completion = usage.completion_tokens ?? 0
  const window = usage.context_window ?? 128000

  // Meta tool calls surface as change nodes in the graph, not as tool rows.
  // Meta and memory calls are not executor activity: they are the agent
  // adjusting Onion or its own notes, and they have their own surfaces.
  const toolCalls = events.filter((e) => {
    if (e.type !== 'tool-call') return false
    const name = String(e.payload.name || '')
    return !name.startsWith('onion_') && !name.startsWith('memory_')
  })
  const results = new Map(events.filter((e) => e.type === 'tool-result').map((e) => [e.payload.call_id, e]))
  const recentTools = toolCalls.slice(-6).reverse()

  // Memory is silent in the transcript, so the monitor is where the user sees
  // what was recalled for this run and what was learned from it.
  const recalled = events.filter((e) => e.type === 'memory-recalled').slice(-1)[0]
  const learned = events.filter((e) => e.type === 'memory-learned').slice(-1)[0]
  const memoryCount = Number(recalled?.payload.count ?? 0)
  const learnedKinds: string[] = (learned?.payload.kinds as string[]) || []

  const circumference = 2 * Math.PI * 34
  const offset = circumference * (1 - ratio)

  return (
    <aside className="onion-monitor" aria-label="Task monitor">
      <div className="onion-monitor__header">
        <span className="onion-monitor__title">Monitor</span>
        <span className="spacer" style={{ flex: 1 }} />
        <button
          className="onion-iconbtn"
          onClick={() => onOpenPreview('web')}
          title="Open preview (web / terminal / diff)"
          aria-label="Open preview panel"
          style={{ width: 30, height: 30 }}
        >
          <MonitorPlay size={15} />
        </button>
        <button className="onion-iconbtn" onClick={onClose} title="Hide monitor" aria-label="Hide monitor" style={{ width: 30, height: 30 }}>
          <X size={16} />
        </button>
      </div>

      <div className="onion-monitor__section">
        <span className="onion-monitor__label">Token usage</span>
        <div className="usage-ring">
          <svg className="usage-ring__svg" viewBox="0 0 80 80">
            <circle className="usage-ring__track" cx="40" cy="40" r="34" fill="none" strokeWidth="7" />
            <circle
              className="usage-ring__value"
              data-level={level}
              cx="40"
              cy="40"
              r="34"
              fill="none"
              strokeWidth="7"
              strokeDasharray={circumference}
              strokeDashoffset={offset}
            />
          </svg>
          <div className="usage-ring__meta">
            <span className="usage-ring__pct">{Math.round(ratio * 100)}%</span>
            <span>{total.toLocaleString()} / ~{window.toLocaleString()}</span>
            <span>
              in {prompt.toLocaleString()} · out {completion.toLocaleString()}
            </span>
          </div>
        </div>
      </div>

      <StarChainGraph events={events} prompt={taskPrompt} onFocusEvent={onFocusEvent} />

      <div className="onion-monitor__section">
        <span className="onion-monitor__label">Memory</span>
        <div className="tool-row">
          <Database size={14} className="text-muted" />
          <span className="tool-row__name">
            {memoryCount > 0 ? `Recalled ${memoryCount} note${memoryCount === 1 ? '' : 's'}` : 'Nothing recalled'}
          </span>
          {memoryCount > 0 && <StatusBadge status="success" />}
        </div>
        {learnedKinds.length > 0 && (
          <div className="text-muted" style={{ lineHeight: 1.6, marginTop: 6 }}>
            Learned this run: {learnedKinds.join(', ')}
          </div>
        )}
      </div>

      <div className="onion-monitor__section">
        <span className="onion-monitor__label">Tool activity</span>
        {recentTools.length === 0 && <span className="monitor-empty">No tool calls yet.</span>}
        <div className="stack">
          {recentTools.map((call) => {
            const result = results.get(call.payload.call_id)
            return (
              <div key={call.id} className="tool-row">
                <ExecutorBadge executor={call.payload.executor} />
                <span className="tool-row__name">
                  {(call.payload.arguments?.description as string) || call.payload.preview || call.payload.name}
                </span>
                <StatusBadge status={result ? result.payload.status : call.payload.status} />
              </div>
            )
          })}
        </div>
      </div>

      <div className="onion-monitor__section">
        <span className="onion-monitor__label">Pending confirmations</span>
        {proposals.length === 0 && (
          <span className="monitor-empty">No meta-operation proposals waiting.</span>
        )}
        <div className="stack">
          {proposals.map((proposal) => (
            <button
              key={proposal.id}
              className="tool-row"
              style={{ border: 'none', cursor: 'pointer', textAlign: 'left', width: '100%', color: 'var(--md-sys-color-on-surface)' }}
              onClick={() => onFocusProposal(proposal.id)}
            >
              <span className="tool-row__name">{proposal.label}</span>
              <span className="text-muted" style={{ fontSize: '0.7rem' }}>
                {proposal.target}
              </span>
              <StatusBadge status="proposed" />
            </button>
          ))}
        </div>
      </div>

      <div className="onion-monitor__section">
        <span className="onion-monitor__label">Event stream</span>
        <div className="text-muted" style={{ lineHeight: 1.6 }}>
          {events.length} events · last{' '}
          {events.length ? new Date(events[events.length - 1].timestamp * 1000).toLocaleTimeString() : '—'}
        </div>
      </div>
    </aside>
  )
}