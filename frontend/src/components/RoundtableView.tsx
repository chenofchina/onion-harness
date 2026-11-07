import { useMemo } from 'react'
import { ShieldAlert, Users } from 'lucide-react'
import type { RoundtableState } from '../lib/api'
import { roundtableStatus } from '../lib/roundtable'
import { roundtableSeatGlyph } from './RoundtableAvatars'

type Props = {
  state: RoundtableState
  topic: string
  onSelectSeat: (seatId: string) => void
  selectedSeat: string | null
}

const VIEW = 720
const CENTER = VIEW / 2
const RADIUS = 250
const NODE_W = 132
const NODE_H = 64

/** 2D roundtable: seats evenly spaced on a circle, rebuttals as red curves. */
export function RoundtableView({ state, topic, onSelectSeat, selectedSeat }: Props) {
  const { seats, hostId } = state

  const positions = useMemo(() => {
    const map = new Map<string, { x: number; y: number; angle: number }>()
    seats.forEach((seat, index) => {
      // Start at the top so the host sits at 12 o'clock when it is seat 0.
      const angle = (index / Math.max(1, seats.length)) * Math.PI * 2 - Math.PI / 2
      map.set(seat.id, {
        x: CENTER + Math.cos(angle) * RADIUS,
        y: CENTER + Math.sin(angle) * RADIUS,
        angle,
      })
    })
    return map
  }, [seats])

  const latestBySeat = useMemo(() => {
    const map = new Map<string, RoundtableState['speeches'][number]>()
    for (const speech of state.speeches) map.set(speech.seat_id, speech)
    return map
  }, [state.speeches])

  const activeSpeech = state.activeSeat ? latestBySeat.get(state.activeSeat) : undefined

  return (
    <div className="roundtable-view">
      <div className="roundtable-view__stage">
        <svg
          className="roundtable-svg"
          viewBox={`0 0 ${VIEW} ${VIEW}`}
          role="img"
          aria-label="Roundtable topology"
        >
          <defs>
            <marker
              id="rt-arrow"
              viewBox="0 0 10 10"
              refX="9"
              refY="5"
              markerWidth="6"
              markerHeight="6"
              orient="auto-start-reverse"
            >
              <path d="M0 0 L10 5 L0 10 z" fill="currentColor" />
            </marker>
            <radialGradient id="rt-glow" cx="50%" cy="50%" r="50%">
              <stop offset="0%" stopColor="currentColor" stopOpacity="0.28" />
              <stop offset="70%" stopColor="currentColor" stopOpacity="0.06" />
              <stop offset="100%" stopColor="currentColor" stopOpacity="0" />
            </radialGradient>
          </defs>

          {/* table */}
          <circle
            cx={CENTER}
            cy={CENTER}
            r={RADIUS - 40}
            className="roundtable-table"
          />
          <circle cx={CENTER} cy={CENTER} r={RADIUS - 88} className="roundtable-table-inner" />

          {/* host spoke: a thin guide from centre to the host seat */}
          {hostId && positions.has(hostId) && (
            <line
              x1={CENTER}
              y1={CENTER}
              x2={positions.get(hostId)!.x}
              y2={positions.get(hostId)!.y}
              className="roundtable-host-spoke"
            />
          )}

          {/* rebuttal edges */}
          {state.rebuttals.map((edge, index) => {
            const from = positions.get(edge.from)
            const to = positions.get(edge.to)
            if (!from || !to) return null
            const { c1, c2 } = bezier(from, to)
            return (
              <g key={`${edge.from}-${edge.to}-${index}`} className="roundtable-edge">
                <path
                  d={`M ${from.x} ${from.y} C ${c1.x} ${c1.y} ${c2.x} ${c2.y} ${to.x} ${to.y}`}
                  className="roundtable-edge__line"
                  markerEnd="url(#rt-arrow)"
                />
                <EdgeLabel from={from} to={to} edge={edge} index={index} />
              </g>
            )
          })}

          {/* Dissent pulses are pure CSS (opacity only). SMIL path animation
              stalls the WebView renderer when many edges animate at once. */}
          {state.rebuttals.slice(-8).map((edge, index) => {
            const from = positions.get(edge.from)
            const to = positions.get(edge.to)
            if (!from || !to) return null
            const mid = { x: (from.x + to.x) / 2, y: (from.y + to.y) / 2 }
            return (
              <circle
                key={`pulse-${edge.from}-${edge.to}-${index}`}
                r="2.6"
                className="roundtable-edge__dot"
                cx={mid.x}
                cy={mid.y}
                style={{ animationDelay: `${index * 0.18}s` }}
              />
            )
          })}

          {/* centre: the live turn. A disc keeps the connecting lines from
              running through the text. */}
          <g>
            <circle cx={CENTER} cy={CENTER} r="74" className="roundtable-centre-disc" />
            <text x={CENTER} y={CENTER - 10} className="roundtable-centre__title">
              {state.breaker ? 'Stopped' : state.consensus ? 'Consensus' : `Round ${state.round || 0}`}
            </text>
            <text x={CENTER} y={CENTER + 14} className="roundtable-centre__value">
              {state.round || 0} / {state.maxRounds}
            </text>
            <text x={CENTER} y={CENTER + 38} className="roundtable-centre__hint">
              {activeSpeech ? `${activeSpeech.name} speaking` : roundtableStatus(state)}
            </text>
          </g>
        </svg>

        {/* Seat cards are HTML so text stays crisp and clickable. */}
        <div className="roundtable-nodes">
          {seats.map((seat) => {
            const pos = positions.get(seat.id)
            if (!pos) return null
            const speech = latestBySeat.get(seat.id)
            const speaking = state.activeSeat === seat.id
            const isHost = seat.id === hostId
            const stance = speech?.stance
            return (
              <button
                key={seat.id}
                className="rt-node"
                data-speaking={speaking}
                data-stance={stance}
                data-host={isHost}
                data-selected={selectedSeat === seat.id}
                style={{
                  left: `${(pos.x / VIEW) * 100}%`,
                  top: `${(pos.y / VIEW) * 100}%`,
                  width: NODE_W,
                  minHeight: NODE_H,
                }}
                onClick={() => onSelectSeat(seat.id)}
                title={`${seat.name} · ${seat.label} · ${seat.model}`}
              >
                {speaking && <span className="rt-node__halo" aria-hidden />}
                <span className="rt-node__avatar">{roundtableSeatGlyph(seat.name)}</span>
                <span className="rt-node__body">
                  <span className="rt-node__name">
                    {seat.name}
                    {isHost && <span className="rt-node__host">Host</span>}
                  </span>
                  <span className="rt-node__meta">{seat.label}</span>
                </span>
                {speech && (
                  <span className="rt-node__data">
                    <span className="rt-node__tokens">
                      {(speech.input_tokens + speech.output_tokens).toLocaleString()} tok
                    </span>
                    <span className="rt-node__time">{(speech.elapsed_ms / 1000).toFixed(1)}s</span>
                  </span>
                )}
              </button>
            )
          })}
        </div>
      </div>

      <div className="roundtable-view__footer">
        <span className="roundtable-view__status">
          <Users size={13} />
          {roundtableStatus(state)}
        </span>
        <span className="roundtable-view__topic" title={topic}>
          {topic}
        </span>
        {state.breaker && (
          <span className="roundtable-view__breaker">
            <ShieldAlert size={13} />
            {state.breaker.message}
          </span>
        )}
      </div>
    </div>
  )
}

function bezier(
  from: { x: number; y: number },
  to: { x: number; y: number },
): { c1: { x: number; y: number }; c2: { x: number; y: number } } {
  // Bow the curve toward the centre so edges never overlap the outer seats.
  const midX = (from.x + to.x) / 2
  const midY = (from.y + to.y) / 2
  const pull = 0.32
  const toCenterX = (CENTER - midX) * pull
  const toCenterY = (CENTER - midY) * pull
  return {
    c1: { x: from.x + (midX - from.x) * 0.6 + toCenterX, y: from.y + (midY - from.y) * 0.6 + toCenterY },
    c2: { x: to.x + (midX - to.x) * 0.6 + toCenterX, y: to.y + (midY - to.y) * 0.6 + toCenterY },
  }
}

function EdgeLabel({
  from,
  to,
  edge,
  index,
}: {
  from: { x: number; y: number }
  to: { x: number; y: number }
  edge: { round: number }
  index: number
}) {
  const x = (from.x + to.x) / 2 + (CENTER - (from.x + to.x) / 2) * 0.28
  const y = (from.y + to.y) / 2 + (CENTER - (from.y + to.y) / 2) * 0.28
  const width = 62
  return (
    <g className="roundtable-edge__tag" transform={`translate(${x}, ${y})`} key={index}>
      <rect x={-width / 2} y={-9} width={width} height={18} rx={9} />
      <text textAnchor="middle" y={3.5} className="roundtable-edge__label">
        R{edge.round} dissent
      </text>
    </g>
  )
}