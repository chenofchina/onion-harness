import { Plus, Users } from 'lucide-react'
import type { RoundtableSeatPublic } from '../lib/api'
import { roundtableSeatGlyph } from './RoundtableAvatars'

type Props = {
  seats: RoundtableSeatPublic[]
  hostId: string
  activeSeat: string | null
  countBySeat: Record<string, number>
  onSelectSeat: (seatId: string) => void
  onAddSeat: () => void
  canAdd: boolean
}

/**
 * Debater roster shown beside the hall. Every seat is listed with its stance
 * tally so the sidebar reads as the meeting's cast, not a tool list.
 */
export function DebaterList({
  seats,
  hostId,
  activeSeat,
  countBySeat,
  onSelectSeat,
  onAddSeat,
  canAdd,
}: Props) {
  return (
    <div className="debater-list">
      <div className="debater-list__head">
        <span className="onion-monitor__label">
          <Users size={11} style={{ verticalAlign: -1, marginRight: 4 }} />
          Debaters ({seats.length})
        </span>
        <button
          className="onion-iconbtn"
          onClick={onAddSeat}
          disabled={!canAdd}
          title={canAdd ? 'Add a debater' : 'Seat limit reached'}
          aria-label="Add a debater"
          style={{ width: 26, height: 26 }}
        >
          <Plus size={14} />
        </button>
      </div>

      <div className="debater-list__items">
        {seats.map((seat) => (
          <button
            key={seat.id}
            className="debater-item"
            data-active={activeSeat === seat.id}
            data-host={seat.id === hostId}
            onClick={() => onSelectSeat(seat.id)}
            title={`${seat.name} · ${seat.model}`}
          >
            <span className="debater-item__avatar">{roundtableSeatGlyph(seat.name)}</span>
            <span className="debater-item__body">
              <span className="debater-item__name">
                {seat.name}
                {seat.id === hostId && <span className="rt-node__host">Host</span>}
              </span>
              <span className="debater-item__meta">{seat.model}</span>
            </span>
            <span className="debater-item__count">{countBySeat[seat.id] || 0}</span>
          </button>
        ))}
      </div>
    </div>
  )
}