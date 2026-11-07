import { ChevronRight, CornerDownRight, Lightbulb, MessageSquare, Send, User, X } from 'lucide-react'
import type { RoundtableMessage, RoundtableSeatPublic, RoundtableSpeech, ThoughtStep } from '../lib/api'
import { roundtableSeatGlyph } from './RoundtableAvatars'
import { Markdown } from './Markdown'

type Props = {
  seat: RoundtableSeatPublic | null
  speeches: RoundtableSpeech[]
  messages: RoundtableMessage[]
  seatNames: Record<string, string>
  onClose: () => void
  onSend: (text: string, toSeat: string) => void
  busy?: boolean
}

/**
 * Seat inspector: the full chain of thought for one debater, plus a composer to
 * address that seat directly. Opened by clicking its node in the topology.
 */
export function SeatInspector({
  seat,
  speeches,
  messages,
  seatNames,
  onClose,
  onSend,
}: Props) {
  if (!seat) return null

  const mine = speeches.filter((speech) => speech.seat_id === seat.id)
  const last = mine[mine.length - 1]
  const addressed = messages.filter((message) => message.to === seat.id || message.to === 'all')

  return (
    <aside className="seat-inspector" aria-label={`${seat.name} details`}>
      <div className="seat-inspector__head">
        <span className="seat-inspector__avatar">{roundtableSeatGlyph(seat.name)}</span>
        <div className="seat-inspector__id">
          <strong>{seat.name}</strong>
          <span>
            {seat.label} · {seat.model}
          </span>
        </div>
        <button
          className="onion-iconbtn"
          onClick={onClose}
          title="Close"
          aria-label="Close seat details"
          style={{ width: 28, height: 28 }}
        >
          <X size={15} />
        </button>
      </div>

      <div className="seat-inspector__body">
        {mine.length === 0 && (
          <span className="monitor-empty">This seat has not spoken yet.</span>
        )}

        {mine.map((speech, index) => (
          <div key={`${speech.round}-${index}`} className="seat-thought" data-stance={speech.stance}>
            <div className="seat-thought__head">
              <span className="seat-thought__round">R{speech.round}</span>
              <span className="seat-thought__stance">{speech.stance}</span>
              <span className="spacer" style={{ flex: 1 }} />
              <span className="seat-thought__meta">
                {(speech.input_tokens + speech.output_tokens).toLocaleString()} tok ·{' '}
                {(speech.elapsed_ms / 1000).toFixed(1)}s
              </span>
            </div>

            <div className="seat-thought__content"><Markdown text={speech.content} /></div>

            {speech.rebuts.length > 0 && (
              <div className="seat-thought__rebuts">
                <CornerDownRight size={11} />
                <span>challenged {speech.rebuts.map((id) => seatNames[id] || id).join(', ')}</span>
              </div>
            )}

            <ThoughtChain steps={speech.thought || []} fallback={speech.reasoning} />
          </div>
        ))}

        {addressed.length > 0 && (
          <div className="seat-inspector__messages">
            <span className="onion-monitor__label">Your messages</span>
            {addressed.slice(-4).map((message, index) => (
              <div key={index} className="seat-message">
                <User size={11} />
                <span>{message.text}</span>
              </div>
            ))}
          </div>
        )}
      </div>

      <SeatComposer seat={seat} onSend={onSend} last={last} />
    </aside>
  )
}

function ThoughtChain({ steps, fallback }: { steps: ThoughtStep[]; fallback: string }) {
  if (steps.length === 0 && !fallback) return null
  const rendered =
    steps.length > 0 ? steps : [{ kind: 'reason', label: 'Reasoning', text: fallback }]
  return (
    <div className="thought-chain">
      {rendered.map((step, index) => (
        <div key={index} className="thought-step" data-kind={step.kind}>
          <span className="thought-step__rail" aria-hidden>
            <span className="thought-step__dot" />
            {index < rendered.length - 1 && <span className="thought-step__line" />}
          </span>
          <div className="thought-step__body">
            <span className="thought-step__label">
              {step.kind === 'human' ? <MessageSquare size={10} /> : null}
              {step.kind === 'reason' ? <Lightbulb size={10} /> : null}
              {step.label}
            </span>
            <span className="thought-step__text">{step.text}</span>
          </div>
        </div>
      ))}
    </div>
  )
}

function SeatComposer({
  seat,
  onSend,
  last,
}: {
  seat: RoundtableSeatPublic
  onSend: (text: string, toSeat: string) => void
  last?: RoundtableSpeech
}) {
  return (
    <div className="seat-composer">
      <input
        className="seat-composer__input"
        placeholder={`Message ${seat.name}…`}
        onKeyDown={(event) => {
          if (event.key !== 'Enter' || event.nativeEvent.isComposing) return
          const value = (event.target as HTMLInputElement).value.trim()
          if (!value) return
          onSend(value, seat.id)
          ;(event.target as HTMLInputElement).value = ''
        }}
        aria-label={`Message ${seat.name}`}
      />
      <button
        className="onion-iconbtn"
        title="Send"
        aria-label="Send message"
        onClick={(event) => {
          const input = (event.currentTarget.parentElement?.querySelector(
            '.seat-composer__input',
          ) as HTMLInputElement | null)
          const value = input?.value.trim()
          if (!input || !value) return
          onSend(value, seat.id)
          input.value = ''
        }}
      >
        <Send size={15} />
      </button>
      {last && (
        <span className="seat-composer__hint">
          <ChevronRight size={10} />
          answered on its next turn
        </span>
      )}
    </div>
  )
}