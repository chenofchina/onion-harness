import { useEffect, useMemo, useState } from 'react'
import { Check, Minus, Plus, ShieldAlert, Sparkles, Users } from 'lucide-react'
import type { RoundtableRosterEntry, Settings } from '../lib/api'
import { LobeIcon } from './LobeIcon'
import { Modal } from './Dialogs'

type SeatDraft = {
  id: string
  name: string
  provider_id: string
  model: string
  thinking: string
  /** Set when the seat uses a free-form endpoint ("Others"). */
  custom?: boolean
  base_url?: string
  wire_format?: string
  api_key?: string
}

type Props = {
  open: boolean
  roster: RoundtableRosterEntry[]
  settings: Settings
  thinkingLevels: string[]
  onClose: () => void
  onStart: (payload: {
    topic: string
    seats: {
      id: string
      name: string
      provider_id: string
      model: string
      thinking: string
      wire_format: string
      base_url: string
      label: string
      custom?: boolean
      api_key?: string
    }[]
    host_id: string
    max_rounds: number
    dissent: string
    thinking: string
  }) => void
  busy?: boolean
  error?: string | null
}

const MIN_SEATS = 2
const MAX_SEATS = 6
const DEFAULT_ROUNDS = 3
const MAX_ROUNDS = 100

const DISSENT = [
  { id: 'low', label: 'Low', hint: 'Seats concede readily' },
  { id: 'medium', label: 'Medium', hint: 'Balanced pushback' },
  { id: 'high', label: 'High', hint: 'Challenge every claim' },
]

const NAME_POOL = ['Alice', 'Bob', 'Chen', 'Dana', 'Eli', 'Farah']

export function RoundtableDialog({
  open,
  roster,
  settings,
  thinkingLevels,
  onClose,
  onStart,
  busy,
  error,
}: Props) {
  const [topic, setTopic] = useState('')
  const [seats, setSeats] = useState<SeatDraft[]>([])
  const [hostId, setHostId] = useState('')
  const [rounds, setRounds] = useState(DEFAULT_ROUNDS)
  const [dissent, setDissent] = useState('medium')
  const [thinking, setThinking] = useState('medium')
  const [localError, setLocalError] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    const keyed = roster.filter((entry) => entry.has_api_key && !entry.custom)
    const known = roster.filter((entry) => !entry.custom)
    const seed = (keyed.length ? keyed : known).slice(0, 3)
    const drafts: SeatDraft[] = seed.map((entry, index) => ({
      id: `seat${index + 1}`,
      name: NAME_POOL[index] || `Seat ${index + 1}`,
      provider_id: entry.id,
      model: entry.models[0] || '',
      thinking,
    }))
    while (drafts.length < MIN_SEATS) {
      const fallback = known[0] || roster[0]
      drafts.push({
        id: `seat${drafts.length + 1}`,
        name: NAME_POOL[drafts.length] || `Seat ${drafts.length + 1}`,
        provider_id: fallback?.id || '',
        model: fallback?.models[0] || '',
        thinking,
      })
    }
    setSeats(drafts)
    setHostId(drafts[0]?.id || '')
    setRounds(DEFAULT_ROUNDS)
    setLocalError(null)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  const presetFor = (providerId: string) => roster.find((entry) => entry.id === providerId)

  const usable = useMemo(
    () =>
      seats.filter((seat) => {
        if (!seat.provider_id) return false
        if (seat.custom) return Boolean((seat.base_url || '').trim() && seat.model.trim())
        return Boolean(seat.model)
      }),
    [seats],
  )

  if (!open) return null

  const updateSeat = (id: string, patch: Partial<SeatDraft>) => {
    setSeats((prev) => prev.map((seat) => (seat.id === id ? { ...seat, ...patch } : seat)))
  }

  const addSeat = () => {
    if (seats.length >= MAX_SEATS) return
    const index = seats.length
    const entry = roster.find((item) => !item.custom) || roster[0]
    setSeats((prev) => [
      ...prev,
      {
        id: `seat${index + 1}`,
        name: NAME_POOL[index] || `Seat ${index + 1}`,
        provider_id: entry?.id || '',
        model: entry?.models[0] || '',
        thinking,
      },
    ])
  }

  const removeSeat = (id: string) => {
    if (seats.length <= MIN_SEATS) return
    setSeats((prev) => {
      const next = prev.filter((seat) => seat.id !== id)
      if (hostId === id && next.length) setHostId(next[0].id)
      return next
    })
  }

  const missingKeys = usable.filter((seat) => {
    if (seat.custom) return false
    const preset = presetFor(seat.provider_id)
    return preset && !preset.has_api_key
  })

  const estimatedCalls = rounds * Math.max(2, usable.length) * 2
  const progress = ((rounds - 1) / (MAX_ROUNDS - 1)) * 100

  const submit = () => {
    if (!topic.trim()) {
      setLocalError('Enter a topic for the roundtable.')
      return
    }
    if (usable.length < MIN_SEATS) {
      setLocalError(`At least ${MIN_SEATS} seats need a usable model.`)
      return
    }
    const badCustom = usable.find(
      (seat) => seat.custom && !(seat.base_url || '').startsWith('http'),
    )
    if (badCustom) {
      setLocalError(
        `Seat "${badCustom.name}" needs a base URL starting with http:// or https://`,
      )
      return
    }
    const names = usable.map((seat) => seat.name.trim().toLowerCase())
    if (new Set(names).size !== names.length) {
      setLocalError('Seat names must be distinct.')
      return
    }
    setLocalError(null)
    onStart({
      topic: topic.trim(),
      seats: usable.map((seat) => {
        const preset = presetFor(seat.provider_id)
        return {
          id: seat.id,
          name: seat.name.trim(),
          provider_id: seat.provider_id,
          model: seat.model,
          thinking: seat.thinking,
          wire_format: seat.wire_format || preset?.format || 'openai',
          base_url: (seat.custom ? seat.base_url : preset?.base_url) || '',
          label: seat.custom ? seat.name.trim() : preset?.label || seat.provider_id,
          custom: Boolean(seat.custom),
          api_key: seat.api_key || '',
        }
      }),
      host_id: hostId,
      max_rounds: rounds,
      dissent,
      thinking,
    })
  }

  const shownError = localError || error

  return (
    <Modal
      title="Roundtable"
      onClose={onClose}
      wide
      footer={
        <>
          <button className="btn" data-variant="text" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button className="btn" data-variant="filled" onClick={submit} disabled={busy}>
            <Users size={14} /> Convene
          </button>
        </>
      }
    >
      <div className="modal__body">
        <div className="field rt-stagger" style={{ ['--stagger' as string]: '0ms' }}>
          <label>Topic</label>
          <textarea
            className="mcp-code"
            rows={2}
            value={topic}
            onChange={(event) => setTopic(event.target.value)}
            placeholder="What should the roundtable decide?"
            autoFocus
          />
        </div>

        <div className="field rt-stagger" style={{ ['--stagger' as string]: '40ms' }}>
          <label>
            Seats ({seats.length}/{MAX_SEATS})
          </label>
          <div className="stack">
            {seats.map((seat, index) => {
              const preset = presetFor(seat.provider_id)
              return (
                <div
                  key={seat.id}
                  className="seat-block rt-stagger"
                  data-host={seat.id === hostId}
                  data-custom={seat.custom ? 'true' : 'false'}
                  style={{ ['--stagger' as string]: `${60 + index * 30}ms` }}
                >
                  <div className="seat-row">
                    <span className="seat-row__index">{index + 1}</span>
                    <input
                      className="seat-row__name"
                      value={seat.name}
                      onChange={(event) => updateSeat(seat.id, { name: event.target.value })}
                      aria-label={`Seat ${index + 1} name`}
                      maxLength={32}
                    />
                    <span className="seat-row__provider">
                      <LobeIcon name={seat.custom ? 'openai' : preset?.icon || 'openai'} size={15} />
                      <select
                        value={seat.custom ? 'custom' : seat.provider_id}
                        onChange={(event) => {
                          const value = event.target.value
                          if (value === 'custom') {
                            updateSeat(seat.id, {
                              custom: true,
                              provider_id: 'custom',
                              base_url: seat.base_url || 'https://',
                              wire_format: seat.wire_format || 'openai',
                            })
                            return
                          }
                          const next = presetFor(value)
                          updateSeat(seat.id, {
                            custom: false,
                            provider_id: value,
                            model: next?.models[0] || '',
                            base_url: undefined,
                            wire_format: undefined,
                            api_key: undefined,
                          })
                        }}
                        aria-label={`Seat ${index + 1} provider`}
                      >
                        {roster
                          .filter((entry) => !entry.custom)
                          .map((entry) => (
                            <option key={entry.id} value={entry.id}>
                              {entry.label}
                              {entry.has_api_key ? '' : ' (no key)'}
                            </option>
                          ))}
                        <option value="custom">Others…</option>
                      </select>
                    </span>
                    {!seat.custom && (
                      <select
                        className="seat-row__model"
                        value={seat.model}
                        onChange={(event) => updateSeat(seat.id, { model: event.target.value })}
                        aria-label={`Seat ${index + 1} model`}
                      >
                        {(preset?.models || []).map((model) => (
                          <option key={model} value={model}>
                            {model}
                          </option>
                        ))}
                        {preset && !preset.models.includes(seat.model) && seat.model && (
                          <option value={seat.model}>{seat.model}</option>
                        )}
                      </select>
                    )}
                    <button
                      className="seat-row__host"
                      data-active={seat.id === hostId}
                      onClick={() => setHostId(seat.id)}
                      title="Make this seat the host"
                    >
                      Host
                    </button>
                    <button
                      className="onion-iconbtn"
                      onClick={() => removeSeat(seat.id)}
                      disabled={seats.length <= MIN_SEATS}
                      title="Remove seat"
                      aria-label="Remove seat"
                      style={{ width: 28, height: 28 }}
                    >
                      <Minus size={14} />
                    </button>
                  </div>

                  {seat.custom && (
                    <div className="seat-custom">
                      <input
                        className="seat-custom__url"
                        value={seat.base_url || ''}
                        onChange={(event) => updateSeat(seat.id, { base_url: event.target.value })}
                        placeholder="https://api.example.com/v1"
                        aria-label="Custom base URL"
                        spellCheck={false}
                      />
                      <input
                        className="seat-custom__model"
                        value={seat.model}
                        onChange={(event) => updateSeat(seat.id, { model: event.target.value })}
                        placeholder="model name"
                        aria-label="Custom model name"
                        spellCheck={false}
                      />
                      <select
                        className="seat-custom__format"
                        value={seat.wire_format || 'openai'}
                        onChange={(event) => updateSeat(seat.id, { wire_format: event.target.value })}
                        aria-label="Wire format"
                      >
                        <option value="openai">OpenAI</option>
                        <option value="anthropic">Anthropic</option>
                      </select>
                      <input
                        className="seat-custom__key"
                        type="password"
                        value={seat.api_key || ''}
                        onChange={(event) => updateSeat(seat.id, { api_key: event.target.value })}
                        placeholder="API key (optional for local)"
                        aria-label="Custom API key"
                      />
                    </div>
                  )}
                </div>
              )
            })}
            <button
              className="btn rt-stagger"
              data-variant="outlined"
              onClick={addSeat}
              disabled={seats.length >= MAX_SEATS}
              style={{ alignSelf: 'flex-start' }}
            >
              <Plus size={14} /> Add seat
            </button>
          </div>
          <span className="field__hint">
            Seats may mix providers and models. Choose <strong>Others…</strong> to use any
            OpenAI-compatible or Anthropic endpoint with your own base URL and model name.
          </span>
        </div>

        <div className="field rt-stagger" style={{ ['--stagger' as string]: '80ms' }}>
          <label className="rt-label-row">
            <span>Round limit</span>
            <span className="rt-slider__value">{rounds}</span>
          </label>
          <div className="rt-slider">
            <input
              type="range"
              min={1}
              max={MAX_ROUNDS}
              step={1}
              value={rounds}
              onChange={(event) => setRounds(Number(event.target.value))}
              aria-label="Round limit"
              style={{ ['--rt-progress' as string]: `${progress}%` }}
            />
            <div className="rt-slider__marks" aria-hidden>
              <span>1</span>
              <span>25</span>
              <span>50</span>
              <span>75</span>
              <span>100</span>
            </div>
          </div>
          <span className="field__hint">
            Hard stop after this many rounds without consensus. The meeting then hands its
            divergences to you; the token budget can stop it sooner.
          </span>
        </div>

        <div className="field rt-stagger" style={{ ['--stagger' as string]: '120ms' }}>
          <label>Dissent frequency</label>
          <div
            className="segmented segmented--sliding"
            data-size="small"
            role="radiogroup"
            aria-label="Dissent frequency"
            style={{
              ['--seg-count' as string]: '3',
              ['--seg-index' as string]: String(
                DISSENT.findIndex((item) => item.id === dissent),
              ),
            }}
          >
            <span className="segmented__indicator" aria-hidden />
            {DISSENT.map((item) => (
              <button
                key={item.id}
                role="radio"
                aria-checked={dissent === item.id}
                data-active={dissent === item.id}
                onClick={() => setDissent(item.id)}
                title={item.hint}
              >
                {item.label}
              </button>
            ))}
          </div>
        </div>

        <div className="field rt-stagger" style={{ ['--stagger' as string]: '160ms' }}>
          <label>Thinking depth</label>
          <div
            className="segmented segmented--sliding"
            data-size="small"
            role="radiogroup"
            aria-label="Thinking depth"
            style={{
              ['--seg-count' as string]: String(thinkingLevels.length),
              ['--seg-index' as string]: String(
                Math.max(0, thinkingLevels.indexOf(thinking)),
              ),
            }}
          >
            <span className="segmented__indicator" aria-hidden />
            {thinkingLevels.map((level) => (
              <button
                key={level}
                role="radio"
                aria-checked={thinking === level}
                data-active={thinking === level}
                onClick={() => setThinking(level)}
              >
                {level}
              </button>
            ))}
          </div>
        </div>

        {missingKeys.length > 0 && (
          <div className="card-change__warn rt-stagger">
            <ShieldAlert size={15} style={{ flexShrink: 0, marginTop: 1 }} />
            <span>
              {missingKeys.length} seat{missingKeys.length === 1 ? '' : 's'} use a provider with
              no saved API key. Add one in Advanced settings, use Others… with its own key, or
              pick another provider.
            </span>
          </div>
        )}

        {shownError && (
          <div className="card-change__warn rt-stagger">
            <ShieldAlert size={15} style={{ flexShrink: 0, marginTop: 1 }} />
            <span>{shownError}</span>
          </div>
        )}

        <div className="field__hint rt-stagger">
          <Sparkles size={11} style={{ verticalAlign: -1, marginRight: 4 }} />
          Up to {estimatedCalls.toLocaleString()} model calls · the breaker stops the meeting
          earlier if it runs out of budget.
        </div>
      </div>
    </Modal>
  )
}