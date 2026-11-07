import type {
  OnionEvent,
  RoundtableAgreement,
  RoundtableDivergence,
  RoundtableSeatPublic,
  RoundtableSpeech,
  RoundtableState,
} from './api'

export const EMPTY_ROUNDTABLE: RoundtableState = {
  seats: [],
  hostId: '',
  maxRounds: 3,
  round: 0,
  speeches: [],
  rebuttals: [],
  agreements: [],
  divergences: [],
  activeSeat: null,
  messages: [],
  consensus: null,
  breaker: null,
  done: false,
  totalTokens: 0,
}

/**
 * Fold the roundtable event family into one render-ready state object.
 *
 * Pure and idempotent, so it can be recomputed from persisted history after a
 * reload exactly as it was built live.
 */
export function reduceRoundtable(events: OnionEvent[]): RoundtableState {
  const ordered = [...events].sort((a, b) => a.timestamp - b.timestamp || a.seq - b.seq)
  let state: RoundtableState = { ...EMPTY_ROUNDTABLE }

  for (const event of ordered) {
    const p = event.payload || {}
    switch (event.type) {
      case 'roundtable-start':
        state = {
          ...state,
          seats: (p.seats || []) as RoundtableSeatPublic[],
          hostId: p.host_id || '',
          maxRounds: Number(p.max_rounds || 3),
          done: false,
          breaker: null,
          consensus: null,
        }
        break

      case 'roundtable-round':
        state = { ...state, round: Number(p.round || state.round), activeSeat: null }
        break

      case 'roundtable-message':
        state = {
          ...state,
          messages: [
            ...state.messages,
            {
              text: p.text || '',
              to: p.to || 'all',
              round: Number(p.round || state.round),
              at: Number(p.at || event.timestamp),
            },
          ],
        }
        break

      case 'roundtable-seat-added':
        if (p.seat && !state.seats.some((seat) => seat.id === p.seat.id)) {
          state = { ...state, seats: [...state.seats, p.seat] }
        }
        break

      case 'roundtable-turn':
        state = { ...state, activeSeat: p.seat_id || null, round: Number(p.round || state.round) }
        break

      case 'roundtable-speech': {
        const speech = p as unknown as RoundtableSpeech
        state = {
          ...state,
          speeches: [...state.speeches, speech],
          activeSeat: null,
          totalTokens: state.totalTokens + (speech.input_tokens || 0) + (speech.output_tokens || 0),
        }
        break
      }

      case 'roundtable-rebuttal':
        state = {
          ...state,
          rebuttals: [
            ...state.rebuttals,
            {
              from: p.from_seat,
              to: p.to_seat,
              round: Number(p.round || state.round),
              reasoning: p.reasoning || '',
            },
          ],
        }
        break

      case 'roundtable-consensus':
        state = {
          ...state,
          agreements: (p.agreements || []) as RoundtableAgreement[],
          divergences: (p.divergences || []) as RoundtableDivergence[],
          consensus: p.reached ? p.summary || '' : state.consensus,
        }
        break

      case 'roundtable-breaker':
        state = {
          ...state,
          breaker: { message: p.message || '', reason: p.reason || '' },
          divergences: (p.divergences || []) as RoundtableDivergence[],
          agreements: (p.agreements || []) as RoundtableAgreement[],
          activeSeat: null,
        }
        break

      case 'roundtable-end':
        state = {
          ...state,
          done: true,
          activeSeat: null,
          consensus: p.consensus ?? state.consensus,
          totalTokens: Number(p.total_tokens || state.totalTokens),
          breaker: p.requires_human
            ? { message: p.message || state.breaker?.message || '', reason: p.reason || '' }
            : state.breaker,
        }
        break

      default:
        break
    }
  }

  return state
}

/** Where the meeting stands right now, as one short label. */
export function roundtableStatus(state: RoundtableState): string {
  if (state.breaker) return 'Needs human'
  if (state.consensus) return 'Consensus'
  if (state.done) return 'Closed'
  if (state.round > 0) return `Round ${state.round} of ${state.maxRounds}`
  return 'Preparing'
}