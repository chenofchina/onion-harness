import { useMemo, useState } from 'react'
import { CornerDownRight, Lightbulb, Send } from 'lucide-react'
import type { OnionEvent, RoundtableMessage, RoundtableSeatPublic, RoundtableSpeech } from '../lib/api'
import { roundtableSeatGlyph } from './RoundtableAvatars'
import { Markdown } from './Markdown'

type Props = {
  events: OnionEvent[]
  seats: RoundtableSeatPublic[]
  hostId: string
  onSelectSeat: (seatId: string) => void
  onSend: (text: string, toSeat: string) => void
  busy?: boolean
}

type HallItem =
  | { kind: 'round'; round: number; max: number; at: number; key: string }
  | { kind: 'speech'; speech: RoundtableSpeech; at: number; key: string }
  | { kind: 'human'; message: RoundtableMessage; at: number; key: string }
  | { kind: 'notice'; text: string; at: number; key: string }

/**
 * The main hall: every roundtable statement appears inline as a conversation,
 * interleaved with the human's own messages, in strict chronological order.
 */
export function RoundtableHall({ events, seats, hostId, onSelectSeat, onSend }: Props) {
  const [draft, setDraft] = useState('')
  const [target, setTarget] = useState('all')

  const seatMap = useMemo(() => {
    const map: Record<string, RoundtableSeatPublic> = {}
    for (const seat of seats) map[seat.id] = seat
    return map
  }, [seats])

  const items = useMemo<HallItem[]>(() => {
    const ordered = [...events].sort((a, b) => a.timestamp - b.timestamp || a.seq - b.seq)
    const out: HallItem[] = []
    for (const event of ordered) {
      const p = event.payload || {}
      if (event.type === 'roundtable-round') {
        out.push({
          kind: 'round',
          round: Number(p.round || 0),
          max: Number(p.max_rounds || 0),
          at: event.timestamp,
          key: event.id,
        })
      } else if (event.type === 'roundtable-speech') {
        out.push({
          kind: 'speech',
          speech: p as unknown as RoundtableSpeech,
          at: event.timestamp,
          key: event.id,
        })
      } else if (event.type === 'roundtable-message') {
        out.push({
          kind: 'human',
          message: p as unknown as RoundtableMessage,
          at: event.timestamp,
          key: event.id,
        })
      } else if (event.type === 'roundtable-breaker') {
        out.push({
          kind: 'notice',
          text: String(p.message || 'Consensus not reached.'),
          at: event.timestamp,
          key: event.id,
        })
      } else if (event.type === 'roundtable-consensus' && p.reached) {
        out.push({
          kind: 'notice',
          text: `Consensus: ${p.summary || 'all seats agreed.'}`,
          at: event.timestamp,
          key: event.id,
        })
      } else if (event.type === 'roundtable-seat-added' && p.seat) {
        out.push({
          kind: 'notice',
          text: `${p.seat.name} joined the roundtable.`,
          at: event.timestamp,
          key: event.id,
        })
      }
    }
    return out
  }, [events])

  const send = () => {
    const text = draft.trim()
    if (!text) return
    onSend(text, target)
    setDraft('')
  }

  return (
    <div className="rt-hall">
      <div className="rt-hall__stream">
        <div className="rt-hall__inner">
          {items.map((item) => {
            if (item.kind === 'round') {
              return (
                <div key={item.key} className="rt-hall__divider">
                  <span>Round {item.round}</span>
                  {item.max > 0 && <small>of {item.max}</small>}
                </div>
              )
            }
            if (item.kind === 'notice') {
              return (
                <div key={item.key} className="rt-hall__notice">
                  {item.text}
                </div>
              )
            }
            if (item.kind === 'human') {
              const toName =
                item.message.to && item.message.to !== 'all'
                  ? seatMap[item.message.to]?.name || item.message.to
                  : 'everyone'
              return (
                <div key={item.key} className="rt-hall__human">
                  <div className="rt-hall__human-bubble">
                    <span className="rt-hall__human-to">to {toName}</span>
                    {item.message.text}
                  </div>
                </div>
              )
            }
            const speech = item.speech
            const seat = seatMap[speech.seat_id]
            return (
              <RoundtableSpeechCard
                key={item.key}
                speech={speech}
                seat={seat}
                isHost={speech.seat_id === hostId}
                seatNames={Object.fromEntries(seats.map((s) => [s.id, s.name]))}
                onOpen={() => onSelectSeat(speech.seat_id)}
              />
            )
          })}
          <div style={{ height: 8 }} />
        </div>
      </div>

      <div className="rt-hall__composer">
        <select
          value={target}
          onChange={(event) => setTarget(event.target.value)}
          aria-label="Message recipient"
          className="rt-hall__recipient"
        >
          <option value="all">Everyone</option>
          {seats.map((seat) => (
            <option key={seat.id} value={seat.id}>
              {seat.name}
            </option>
          ))}
        </select>
        <input
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && !event.nativeEvent.isComposing) send()
          }}
          placeholder="Speak into the roundtable…"
          aria-label="Message the roundtable"
          className="rt-hall__input"
        />
        <button
          className="send-btn"
          onClick={send}
          disabled={!draft.trim()}
          title="Send"
          aria-label="Send message"
        >
          <Send size={16} />
        </button>
      </div>
    </div>
  )
}

function RoundtableSpeechCard({
  speech,
  seat,
  isHost,
  seatNames,
  onOpen,
}: {
  speech: RoundtableSpeech
  seat?: RoundtableSeatPublic
  isHost: boolean
  seatNames: Record<string, string>
  onOpen: () => void
}) {
  return (
    <div className="rt-speech" data-stance={speech.stance}>
      <button className="rt-speech__avatar" onClick={onOpen} title={`Open ${speech.name}'s chain of thought`}>
        {roundtableSeatGlyph(speech.name)}
      </button>
      <div className="rt-speech__body">
        <div className="rt-speech__head">
          <button className="rt-speech__name" onClick={onOpen}>
            {speech.name}
            {isHost && <span className="rt-node__host">Host</span>}
          </button>
          <span className="rt-speech__stance">{speech.stance}</span>
          {seat && <span className="rt-speech__model">{seat.model}</span>}
          <span className="spacer" style={{ flex: 1 }} />
          <span className="rt-speech__stats">
            {(speech.input_tokens + speech.output_tokens).toLocaleString()} tok ·{' '}
            {(speech.elapsed_ms / 1000).toFixed(1)}s
          </span>
        </div>

        <div className="rt-speech__content"><Markdown text={speech.content} /></div>

        {speech.rebuts.length > 0 && (
          <div className="rt-speech__rebuts">
            <CornerDownRight size={11} />
            <span>
              challenged {speech.rebuts.map((id) => seatNames[id] || id).join(', ')}
            </span>
          </div>
        )}

        {speech.reasoning && (
          <div className="rt-speech__reason">
            <Lightbulb size={11} />
            <span>{speech.reasoning}</span>
          </div>
        )}

        <button className="rt-speech__more" onClick={onOpen}>
          Chain of thought →
        </button>
      </div>
    </div>
  )
}