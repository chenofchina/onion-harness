/**
 * Seat avatars: hand-drawn mono SVG monograms.
 *
 * No emoji and no raster avatars — each seat gets a geometric mark derived from
 * its name, in Material 3 `currentColor` so it follows the theme.
 */

const SHAPES = 6

function hash(text: string): number {
  let value = 0
  for (let index = 0; index < text.length; index += 1) {
    value = (value * 31 + text.charCodeAt(index)) >>> 0
  }
  return value
}

export function roundtableSeatGlyph(name: string): React.ReactNode {
  const h = hash(name || 'seat')
  const shape = h % SHAPES
  const initial = (name || '?').trim().slice(0, 1).toUpperCase()

  return (
    <svg viewBox="0 0 32 32" width="26" height="26" fill="none" stroke="currentColor" aria-hidden>
      {shape === 0 && <circle cx="16" cy="16" r="12" strokeWidth="1.6" opacity="0.5" />}
      {shape === 1 && <rect x="5" y="5" width="22" height="22" rx="7" strokeWidth="1.6" opacity="0.5" />}
      {shape === 2 && <path d="M16 4 L28 16 L16 28 L4 16 Z" strokeWidth="1.6" opacity="0.5" />}
      {shape === 3 && <path d="M16 4 L28 27 L4 27 Z" strokeWidth="1.6" opacity="0.5" strokeLinejoin="round" />}
      {shape === 4 && <path d="M16 4 C22 10 22 22 16 28 C10 22 10 10 16 4 Z" strokeWidth="1.6" opacity="0.5" />}
      {shape === 5 && (
        <>
          <circle cx="16" cy="16" r="12" strokeWidth="1.6" opacity="0.5" />
          <path d="M4 16 H28" strokeWidth="1.2" opacity="0.35" />
        </>
      )}
      <text
        x="16"
        y="20.5"
        textAnchor="middle"
        fontSize="11"
        fontWeight="600"
        fill="currentColor"
        stroke="none"
      >
        {initial}
      </text>
    </svg>
  )
}