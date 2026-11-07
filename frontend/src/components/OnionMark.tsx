/**
 * The Onion mark: a circle split down the middle.
 *
 * Left half  - an onion cross-section drawn as concentric arcs with
 *              decreasing stroke weight, clipped to the left semicircle.
 * Right half - a two-star constellation (large + small on a diagonal),
 *              echoing a dual-star logo, drawn in the same mono style.
 *
 * Everything uses `currentColor`, so the mark follows the Material 3 theme.
 */

type Props = {
  size?: number
  className?: string
  animated?: boolean
  title?: string
}

export function OnionMark({ size = 120, className, animated = false, title = 'Onion Harness' }: Props) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 120 120"
      fill="none"
      className={className}
      role="img"
      aria-label={title}
    >
      <defs>
        <clipPath id="onion-left-clip">
          <path d="M60 8 A52 52 0 0 0 60 112 Z" />
        </clipPath>
      </defs>

      {/* Outer ring: the shared silhouette for both halves. */}
      <circle cx="60" cy="60" r="52" stroke="currentColor" strokeOpacity="0.28" strokeWidth="1.25" />

      {/* ---- Left half: onion cross-section ---- */}
      <g clipPath="url(#onion-left-clip)" stroke="currentColor" strokeLinecap="round">
        {[
          { r: 44.5, w: 1.9, o: 0.95 },
          { r: 36.5, w: 1.65, o: 0.82 },
          { r: 28.5, w: 1.4, o: 0.68 },
          { r: 20.5, w: 1.15, o: 0.54 },
          { r: 13.0, w: 0.95, o: 0.4 },
          { r: 6.4, w: 0.8, o: 0.28 },
        ].map((ring) => (
          <circle
            key={ring.r}
            cx="60"
            cy="60"
            r={ring.r}
            strokeWidth={ring.w}
            strokeOpacity={ring.o}
            className={animated ? 'onion-ring' : undefined}
          />
        ))}
        {/* Radial hints: thin arcs that read as onion layer veins. */}
        <path d="M60 16 Q50 44 47 60 Q50 76 60 104" strokeWidth="0.75" strokeOpacity="0.22" />
        <path d="M60 24 Q54 46 52 60 Q54 74 60 96" strokeWidth="0.6" strokeOpacity="0.16" />
      </g>

      {/* ---- Right half: dual-star constellation ---- */}
      <g stroke="currentColor" strokeLinejoin="round" strokeLinecap="round">
        <Star cx={80} cy={44} r={14} strokeWidth={1.5} opacity={0.95} />
        <Star cx={90.5} cy={82} r={7.5} strokeWidth={1.15} opacity={0.7} />
        {/* Connector between the two stars, kept faint and thin. */}
        <path d="M84 56 L88 69" strokeWidth="0.7" strokeOpacity="0.3" />
        <circle cx="86" cy="63" r="0.9" fill="currentColor" stroke="none" opacity="0.45" />
      </g>

      {/* Vertical split axis. */}
      <line
        x1="60"
        y1="9.5"
        x2="60"
        y2="110.5"
        stroke="currentColor"
        strokeOpacity="0.45"
        strokeWidth="1"
        strokeLinecap="round"
      />
    </svg>
  )
}

/** A four-point star: tapered spokes with concave sides, classic sparkle. */
function Star({
  cx,
  cy,
  r,
  strokeWidth,
  opacity,
}: {
  cx: number
  cy: number
  r: number
  strokeWidth: number
  opacity: number
}) {
  const k = r * 0.19
  const d = [
    `M ${cx} ${cy - r}`,
    `C ${cx + k} ${cy - k} ${cx + k} ${cy - k} ${cx + r} ${cy}`,
    `C ${cx + k} ${cy + k} ${cx + k} ${cy + k} ${cx} ${cy + r}`,
    `C ${cx - k} ${cy + k} ${cx - k} ${cy + k} ${cx - r} ${cy}`,
    `C ${cx - k} ${cy - k} ${cx - k} ${cy - k} ${cx} ${cy - r}`,
    'Z',
  ].join(' ')
  return <path d={d} strokeWidth={strokeWidth} opacity={opacity} />
}

/** Compact variant for the sidebar and window chrome. */
export function OnionGlyph({ size = 28, className }: { size?: number; className?: string }) {
  return (
    <svg width={size} height={size} viewBox="0 0 120 120" fill="none" className={className} aria-hidden>
      <clipPath id="onion-glyph-clip">
        <path d="M60 8 A52 52 0 0 0 60 112 Z" />
      </clipPath>
      <circle cx="60" cy="60" r="50" stroke="currentColor" strokeOpacity="0.7" strokeWidth="4" />
      <g clipPath="url(#onion-glyph-clip)" stroke="currentColor" strokeWidth="5" strokeOpacity="0.9">
        <circle cx="60" cy="60" r="34" />
        <circle cx="60" cy="60" r="18" />
      </g>
      <Star cx={82} cy={46} r={16} strokeWidth={4} opacity={1} />
      <Star cx={90} cy={80} r={9} strokeWidth={3.5} opacity={0.75} />
      <line x1="60" y1="10" x2="60" y2="110" stroke="currentColor" strokeOpacity="0.55" strokeWidth="3" />
    </svg>
  )
}