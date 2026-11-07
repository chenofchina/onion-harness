import { AlertTriangle, CheckCircle2, ScrollText, X } from 'lucide-react'
import type { RoundtableState } from '../lib/api'
import { roundtableStatus } from '../lib/roundtable'

type Props = {
  open: boolean
  onClose: () => void
  state: RoundtableState
  onSelectSeat: (seatId: string) => void
}

/** Collapse repeated points (a seat often restates itself across rounds). */
function dedupeByPoint<T extends { point: string }>(items: T[]): T[] {
  const seen = new Set<string>()
  const out: T[] = []
  for (const item of items) {
    const key = `${item.point.trim().toLowerCase()}`
    if (key && seen.has(key)) continue
    if (key) seen.add(key)
    out.push(item)
  }
  return out
}

/** The Monitor panel in roundtable mode: live minutes, consensus and divergence. */
export function MinutesPanel({ open, onClose, state, onSelectSeat }: Props) {
  if (!open) return null

  const agreements = dedupeByPoint(state.agreements)
  const divergences = dedupeByPoint(state.divergences)

  return (
    <aside className="onion-monitor" aria-label="Meeting minutes">
      <div className="onion-monitor__header">
        <span className="onion-monitor__title">Minutes</span>
        <span className="spacer" style={{ flex: 1 }} />
        <button
          className="onion-iconbtn"
          onClick={onClose}
          title="Hide minutes"
          aria-label="Hide minutes"
          style={{ width: 30, height: 30 }}
        >
          <X size={16} />
        </button>
      </div>

      <div className="onion-monitor__section">
        <span className="onion-monitor__label">Session</span>
        <div className="minutes-session">
          <span className="minutes-session__round">
            {state.round > 0 ? `Round ${state.round}` : 'Round —'}
            <small> / {state.maxRounds}</small>
          </span>
          <span className="minutes-session__status">{roundtableStatus(state)}</span>
          <span className="minutes-session__tokens">
            {state.totalTokens.toLocaleString()} tokens · {state.speeches.length} statements
          </span>
        </div>
      </div>

      {state.breaker && (
        <div className="onion-monitor__section">
          <span className="onion-monitor__label">Circuit breaker</span>
          <div className="minutes-breaker">
            <AlertTriangle size={15} style={{ flexShrink: 0, marginTop: 1 }} />
            <div>
              <strong>{state.breaker.message}</strong>
              <span>
                Stopped after {state.round} round{state.round === 1 ? '' : 's'}
                {state.breaker.reason === 'token-budget'
                  ? ' — token budget exhausted'
                  : ' — round limit reached'}
                .
              </span>
            </div>
          </div>
        </div>
      )}

      <div className="onion-monitor__section">
        <span className="onion-monitor__label">
          Consensus ({agreements.length})
        </span>
        {agreements.length === 0 && (
          <span className="monitor-empty">
            {state.consensus ? state.consensus : 'Nothing agreed yet.'}
          </span>
        )}
        <div className="stack">
          {agreements.slice(0, 6).map((item, index) => (
            <div key={`${item.seat_id}-${index}`} className="minutes-item" data-kind="agree">
              <CheckCircle2 size={12} className="minutes-item__icon" />
              <div>
                <strong>{item.name}</strong>
                <span>{item.point}</span>
              </div>
            </div>
          ))}
        </div>
      </div>

      <div className="onion-monitor__section">
        <span className="onion-monitor__label">
          Divergence ({divergences.length})
        </span>
        {divergences.length === 0 && (
          <span className="monitor-empty">No open disagreement.</span>
        )}
        <div className="stack">
          {divergences.slice(0, 8).map((item, index) => (
            <button
              key={`${item.seat_id}-${index}`}
              className="minutes-item minutes-item--button"
              data-kind="reject"
              onClick={() => onSelectSeat(item.seat_id)}
              title={item.reasoning || item.point}
            >
              <AlertTriangle size={12} className="minutes-item__icon" />
              <div>
                <strong>
                  {item.name} · R{item.round}
                </strong>
                <span>{item.point}</span>
                {item.rebuts.length > 0 && (
                  <span className="minutes-item__rebuts">
                    rebuts {item.rebuts.join(', ')}
                  </span>
                )}
              </div>
            </button>
          ))}
        </div>
      </div>

      <div className="onion-monitor__section">
        <span className="onion-monitor__label">Statements</span>
        {state.speeches.length === 0 && <span className="monitor-empty">Nobody has spoken yet.</span>}
        <div className="stack">
          {[...state.speeches].reverse().slice(0, 8).map((speech, index) => (
            <button
              key={`${speech.seat_id}-${speech.round}-${index}`}
              className="minutes-item minutes-item--button"
              data-kind={speech.stance}
              onClick={() => onSelectSeat(speech.seat_id)}
            >
              <ScrollText size={12} className="minutes-item__icon" />
              <div>
                <strong>
                  {speech.name} · R{speech.round}
                </strong>
                <span>{speech.content.slice(0, 160)}</span>
                <span className="minutes-item__rebuts">
                  {(speech.input_tokens + speech.output_tokens).toLocaleString()} tok ·{' '}
                  {(speech.elapsed_ms / 1000).toFixed(1)}s
                </span>
              </div>
            </button>
          ))}
        </div>
      </div>
    </aside>
  )
}