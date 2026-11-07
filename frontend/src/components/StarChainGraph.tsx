import { useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { ChevronRight } from 'lucide-react'
import type { OnionEvent } from '../lib/api'
import { ExecutorGlyph, executorMeta } from './EventCards'

/**
 * STAR CHAIN GRAPH
 *
 * A minimal topology of one task run. The vertical trunk is the task; each
 * user prompt starts a branch root, and every executor call or self-modification
 * proposal hangs off the trunk as a chip node.
 *
 * Node status colours follow the Material 3 token set:
 *   pending -> tertiary (yellow)   applied/success -> green   failed/timeout -> error
 */

type NodeStatus = 'pending' | 'success' | 'failed' | 'idle'

type ChainNode = {
  key: string
  kind: 'prompt' | 'tool' | 'change'
  title: string
  subtitle: string
  description: string
  reasoning: string
  usage: { prompt: number; completion: number } | null
  estimated: boolean
  remaining: number | null
  window: number
  timestamp: number
  status: NodeStatus
  eventId: string
  executor?: string
  glyph?: string
}

type Props = {
  events: OnionEvent[]
  prompt: string
  onFocusEvent: (eventId: string) => void
}

export function StarChainGraph({ events, prompt, onFocusEvent }: Props) {
  const [expanded, setExpanded] = useState(false)
  const nodes = useMemo(() => buildChain(events, prompt), [events, prompt])

  return (
    <div className="onion-monitor__section star-chain">
      <button
        className="star-chain__header"
        onClick={() => setExpanded((value) => !value)}
        aria-expanded={expanded}
      >
        <span className="onion-monitor__label">Star chain graph</span>
        <span className="star-chain__count">{nodes.length - 1}</span>
        <ChevronRight
          size={14}
          className="card__chevron"
          style={{ transform: expanded ? 'rotate(90deg)' : undefined }}
        />
      </button>

      {expanded && (
        <div className="star-chain__viewport">
          {nodes.length <= 1 && (
            <span className="monitor-empty">No steps recorded yet.</span>
          )}
          <div className="star-chain__tree">
            {nodes.map((node, index) => (
              <ChainRow
                key={node.key}
                node={node}
                isFirst={index === 0}
                isLast={index === nodes.length - 1}
                isRoot={node.kind === 'prompt'}
                onFocusEvent={onFocusEvent}
              />
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

/* ------------------------------------------------------------------- row */

function ChainRow({
  node,
  isFirst,
  isLast,
  isRoot,
  onFocusEvent,
}: {
  node: ChainNode
  isFirst: boolean
  isLast: boolean
  isRoot: boolean
  onFocusEvent: (eventId: string) => void
}) {
  const chipRef = useRef<HTMLButtonElement>(null)
  const [hovered, setHovered] = useState(false)
  const [pinned, setPinned] = useState(false)
  const [anchor, setAnchor] = useState<DOMRect | null>(null)
  const hideTimer = useRef<number | null>(null)

  const open = hovered || pinned

  const measure = () => {
    if (chipRef.current) setAnchor(chipRef.current.getBoundingClientRect())
  }

  useEffect(() => {
    if (open) measure()
  }, [open])

  useEffect(() => {
    if (!open) return
    const onScroll = () => measure()
    window.addEventListener('scroll', onScroll, true)
    window.addEventListener('resize', onScroll)
    return () => {
      window.removeEventListener('scroll', onScroll, true)
      window.removeEventListener('resize', onScroll)
    }
  }, [open])

  useEffect(() => {
    return () => {
      if (hideTimer.current) window.clearTimeout(hideTimer.current)
    }
  }, [])

  const enter = () => {
    if (hideTimer.current) window.clearTimeout(hideTimer.current)
    setHovered(true)
  }

  const leave = () => {
    hideTimer.current = window.setTimeout(() => setHovered(false), 140)
  }

  const handleClick = () => {
    setPinned((value) => !value)
    if (node.eventId) onFocusEvent(node.eventId)
  }

  return (
    <div className="star-chain__row" data-root={isRoot}>
      {/* Trunk + elbow connectors */}
      <span className="star-chain__rail" aria-hidden>
        {!isFirst && <span className="star-chain__line star-chain__line--up" />}
        {!isLast && <span className="star-chain__line star-chain__line--down" />}
        <span className="star-chain__elbow" />
      </span>

      <button
        ref={chipRef}
        className="star-chain__chip"
        data-status={node.status}
        data-root={isRoot}
        data-pinned={pinned}
        onMouseEnter={enter}
        onMouseLeave={leave}
        onClick={handleClick}
        title={node.title}
      >
        <span className="star-chain__dot" data-status={node.status} aria-hidden />
        {node.glyph && (
          <span className="star-chain__glyph">
            <ExecutorGlyph glyph={node.glyph} />
          </span>
        )}
        <span className="star-chain__label">{node.title}</span>
      </button>

      {open && anchor && (
        <NodePopover
          node={node}
          anchor={anchor}
          pinned={pinned}
          onEnter={enter}
          onLeave={leave}
        />
      )}
    </div>
  )
}

/* --------------------------------------------------------------- popover */

function NodePopover({
  node,
  anchor,
  pinned,
  onEnter,
  onLeave,
}: {
  node: ChainNode
  anchor: DOMRect
  pinned: boolean
  onEnter: () => void
  onLeave: () => void
}) {
  const width = 300
  const estimatedHeight = 232
  const gap = 12

  // Prefer opening to the left of the monitor panel; fall back to the right,
  // then clamp both axes into the viewport so the card is never cut off.
  let left = anchor.left - width - gap
  if (left < 8) left = Math.min(anchor.right + gap, window.innerWidth - width - 8)
  left = Math.max(8, Math.min(left, window.innerWidth - width - 8))

  let top = anchor.top - 10
  top = Math.max(8, Math.min(top, window.innerHeight - estimatedHeight - 8))

  return createPortal(
    <div
      className="chain-popover"
      style={{ left, top, width }}
      role="dialog"
      aria-label={`${node.title} details`}
      onMouseEnter={onEnter}
      onMouseLeave={onLeave}
    >
      <div className="chain-popover__head">
        <span className={`chain-popover__dot`} data-status={node.status} />
        <div className="chain-popover__head-text">
          <strong>{node.title}</strong>
          <span>{node.description}</span>
        </div>
        {pinned && <span className="chain-popover__pin">pinned</span>}
      </div>

      <div className="chain-popover__reason">
        <span className="chain-popover__k">Reasoning</span>
        <p>{node.reasoning || 'No reasoning was recorded for this step.'}</p>
      </div>

      <div className="chain-popover__grid">
        <Stat
          label="Input"
          value={node.usage ? fmt(node.usage.prompt) : '—'}
          hint={node.estimated && node.usage ? 'turn est.' : undefined}
        />
        <Stat
          label="Output"
          value={node.usage ? fmt(node.usage.completion) : '—'}
          hint={node.estimated && node.usage ? 'turn est.' : undefined}
        />
        <Stat
          label="Context left"
          value={
            node.remaining === null
              ? '—'
              : `${fmt(node.remaining)}`
          }
          hint={node.remaining === null ? undefined : `of ${fmt(node.window)}`}
        />
      </div>

      <div className="chain-popover__foot">
        <span className="chain-popover__k">Timestamp</span>
        <time className="chain-popover__time">{preciseTime(node.timestamp)}</time>
      </div>
    </div>,
    document.body,
  )
}

function Stat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="chain-stat">
      <span className="chain-stat__k">{label}</span>
      <span className="chain-stat__v">{value}</span>
      {hint && <span className="chain-stat__hint">{hint}</span>}
    </div>
  )
}

/* ---------------------------------------------------------------- builder */

function buildChain(events: OnionEvent[], prompt: string): ChainNode[] {
  const ordered = [...events].sort(
    (a, b) => a.timestamp - b.timestamp || a.seq - b.seq,
  )

  // Usage snapshots give us a running view of context consumption.
  const snapshots: { timestamp: number; total: number; window: number }[] = []
  for (const event of ordered) {
    if (event.type === 'token-usage' || event.type === 'context-usage') {
      const total =
        event.type === 'token-usage'
          ? Number(event.payload.total_tokens ?? 0) ||
            Number(event.payload.prompt_tokens ?? 0) + Number(event.payload.completion_tokens ?? 0)
          : Number(event.payload.prompt_tokens ?? 0)
      snapshots.push({
        timestamp: event.timestamp,
        total,
        window: Number(event.payload.context_window ?? 128_000),
      })
    }
  }

  const usageAt = (timestamp: number) => {
    let latest: { total: number; window: number } | null = null
    for (const snapshot of snapshots) {
      if (snapshot.timestamp <= timestamp) latest = snapshot
      else break
    }
    if (!latest && snapshots.length) latest = snapshots[0]
    return latest
  }

  const lastWindow = snapshots.length ? snapshots[snapshots.length - 1].window : 128_000

  // Fold tool results into their calls; track change lifecycle by change id.
  const resultsByCall = new Map<string, OnionEvent>()
  const changeStatus = new Map<string, { status: string; eventId: string }>()
  for (const event of ordered) {
    if (event.type === 'tool-result') resultsByCall.set(event.payload.call_id, event)
    if (event.type === 'change-proposal' && event.payload.change) {
      changeStatus.set(event.payload.change.id, { status: 'pending', eventId: event.id })
    }
    if (event.type === 'change-applied' && event.payload.revision) {
      const changeId = event.payload.revision.change?.id
      if (changeId) changeStatus.set(changeId, { status: 'applied', eventId: event.id })
    }
    if (event.type === 'change-redone' && event.payload.revision) {
      const changeId = event.payload.revision.change?.id
      if (changeId) changeStatus.set(changeId, { status: 'applied', eventId: event.id })
    }
    if (event.type === 'change-reverted') {
      for (const changeId of changeIdFromRevert(event)) {
        const existing = changeStatus.get(changeId)
        if (existing) changeStatus.set(changeId, { ...existing, status: 'reverted' })
      }
    }
  }

  // Meta tools surface as change nodes, not as generic executor nodes. Pair each
  // proposal with the meta call that produced it so it inherits its cost.
  const metaCalls = ordered.filter(
    (event) => event.type === 'tool-call' && String(event.payload.name || '').startsWith('onion_'),
  )
  const consumedMetaCalls = new Set<string>()
  const metaUse = new Map<string, OnionEvent>() // change id -> originating meta call
  for (const proposal of ordered) {
    if (proposal.type !== 'change-proposal' || !proposal.payload.change) continue
    const changeId = proposal.payload.change.id
    const candidates = metaCalls.filter(
      (call) => call.timestamp <= proposal.timestamp && !consumedMetaCalls.has(call.id),
    )
    const source = candidates[candidates.length - 1]
    if (source) {
      consumedMetaCalls.add(source.id)
      metaUse.set(changeId, source)
    }
  }

  // --- branch root ---
  const rootEvent = ordered.find((event) => event.type === 'user-input')
  const rootTimestamp = rootEvent?.timestamp ?? ordered[0]?.timestamp ?? Date.now() / 1000
  const rootUsage = usageAt(rootTimestamp)
  const nodes: ChainNode[] = [
    {
      key: 'root',
      kind: 'prompt',
      title: 'Prompt',
      subtitle: truncate(prompt, 46),
      description: truncate(prompt, 140),
      reasoning:
        'Branch root of this task. Every executor call and self-modification below descends from this prompt.',
      usage: null,
      estimated: false,
      remaining: rootUsage ? Math.max(0, rootUsage.window - rootUsage.total) : null,
      window: rootUsage?.window ?? lastWindow,
      timestamp: rootTimestamp,
      status: 'idle',
      // The transcript synthesises the user bubble when replaying history, so
      // fall back to that id when no stream event carries it.
      eventId: rootEvent?.id || `user-${ordered[0]?.task_id ?? ''}`,
    },
  ]

  let previousUsageTotal = 0
  const advance = (snapshot: { total: number } | null) => {
    if (snapshot) previousUsageTotal = Math.max(previousUsageTotal, snapshot.total)
  }

  for (const event of ordered) {
    const usageHere = usageAt(event.timestamp)

    if (event.type === 'tool-call') {
      if (consumedMetaCalls.has(event.id)) continue

      const result = resultsByCall.get(event.payload.call_id)
      const payloadUsage = event.payload.usage
      const explicit =
        payloadUsage && (payloadUsage.prompt_tokens || payloadUsage.completion_tokens)
      const snapshotTotal = usageHere?.total ?? previousUsageTotal
      const delta = Math.max(0, snapshotTotal - previousUsageTotal)
      advance(usageHere)

      const meta = executorMeta(event.payload.executor)
      const executorName =
        event.payload.executor || (event.payload.name || '').replace(/^run_/, '')
      nodes.push({
        key: `tool:${event.payload.call_id || event.id}`,
        kind: 'tool',
        title: meta.label,
        subtitle: truncate(event.payload.preview || event.payload.name || '', 44),
        description:
          (event.payload.arguments?.description as string | undefined) ||
          `${meta.label} execution`,
        reasoning: event.payload.reasoning || '',
        usage: explicit
          ? {
              prompt: Number(payloadUsage.prompt_tokens ?? 0),
              completion: Number(payloadUsage.completion_tokens ?? 0),
            }
          : delta > 0
            ? { prompt: delta, completion: 0 }
            : null,
        estimated: !explicit && delta > 0,
        remaining: usageHere ? Math.max(0, usageHere.window - usageHere.total) : null,
        window: usageHere?.window ?? lastWindow,
        timestamp: event.timestamp,
        status: statusOfTool(result),
        eventId: event.id,
        executor: executorName,
        glyph: meta.glyph,
      })
      continue
    }

    if (event.type === 'change-proposal' && event.payload.change) {
      const change = event.payload.change
      const lifecycle = changeStatus.get(change.id)?.status ?? 'pending'
      const source = metaUse.get(change.id)
      const sourceUsage = source?.payload.usage
      const hasSourceUsage =
        sourceUsage && (sourceUsage.prompt_tokens || sourceUsage.completion_tokens)

      const snapshotTotal = usageHere?.total ?? previousUsageTotal
      const delta = Math.max(0, snapshotTotal - previousUsageTotal)
      if (lifecycle !== 'pending') advance(usageHere)

      nodes.push({
        key: `change:${change.id}`,
        kind: 'change',
        title: change.label || 'Self-modification',
        subtitle: truncate(change.target || '', 40),
        description: `${change.label} → ${change.target}`,
        reasoning: change.reason || source?.payload.reasoning || '',
        usage: hasSourceUsage
          ? {
              prompt: Number(sourceUsage.prompt_tokens ?? 0),
              completion: Number(sourceUsage.completion_tokens ?? 0),
            }
          : delta > 0
            ? { prompt: delta, completion: 0 }
            : null,
        estimated: !hasSourceUsage && delta > 0,
        remaining: usageHere ? Math.max(0, usageHere.window - usageHere.total) : null,
        window: usageHere?.window ?? lastWindow,
        timestamp: event.timestamp,
        status: changeNodeStatus(lifecycle),
        eventId: changeStatus.get(change.id)?.eventId || event.id,
        glyph: 'chg',
      })
    }
  }

  return nodes
}

function changeIdFromRevert(event: OnionEvent): string[] {
  const ids = event.payload.change_ids
  return Array.isArray(ids) ? ids : []
}

function statusOfTool(result?: OnionEvent): NodeStatus {
  if (!result) return 'pending'
  const status = result.payload.status
  if (status === 'success') return 'success'
  if (status === 'failed' || status === 'timeout' || status === 'cancelled') return 'failed'
  return 'pending'
}

function changeNodeStatus(lifecycle: string): NodeStatus {
  if (lifecycle === 'applied') return 'success'
  if (lifecycle === 'rejected' || lifecycle === 'reverted') return 'failed'
  return 'pending'
}

/* ----------------------------------------------------------------- helpers */

function fmt(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`
  if (value >= 10_000) return `${(value / 1000).toFixed(1)}k`
  return value.toLocaleString('en-US')
}

function truncate(text: string, limit: number): string {
  const flat = (text || '').replace(/\s+/g, ' ').trim()
  return flat.length <= limit ? flat : `${flat.slice(0, limit - 1)}…`
}

function preciseTime(timestamp: number): string {
  const date = new Date(timestamp * 1000)
  const pad = (value: number, width = 2) => String(value).padStart(width, '0')
  return `${date.toLocaleDateString()} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(
    date.getSeconds(),
  )}.${pad(date.getMilliseconds(), 3)}`
}