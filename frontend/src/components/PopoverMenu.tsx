import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Check } from 'lucide-react'

/**
 * A compact popover that opens *above* its trigger and sizes to its content.
 *
 * It is portalled to <body> so the composer's rounded, clipped container can
 * never cut it off, and it is positioned from the trigger's bounding box so it
 * never covers the conversation above more than necessary.
 */

type PopoverProps = {
  open: boolean
  anchorRef: React.RefObject<HTMLElement | null>
  onClose: () => void
  children: React.ReactNode
  align?: 'start' | 'end'
  minWidth?: number
}

export function ComposerPopover({
  open,
  anchorRef,
  onClose,
  children,
  align = 'start',
  minWidth = 200,
}: PopoverProps) {
  const ref = useRef<HTMLDivElement>(null)
  const [position, setPosition] = useState<{ left: number; bottom: number; minWidth: number } | null>(
    null,
  )

  useLayoutEffect(() => {
    if (!open) {
      setPosition(null)
      return
    }
    const compute = () => {
      const anchor = anchorRef.current
      if (!anchor) return
      const rect = anchor.getBoundingClientRect()
      const width = Math.max(minWidth, Math.round(rect.width))
      let left = align === 'end' ? rect.right - width : rect.left
      left = Math.max(8, Math.min(left, window.innerWidth - width - 8))

      // Anchor above the whole composer, not the individual pill: the pills sit
      // in a second row, so anchoring to one would place the panel on top of the
      // input. Fall back to the pill when no composer wrapper is found.
      const composer = anchor.closest<HTMLElement>('.onion-composer')
      const composerRect = composer?.getBoundingClientRect()
      const anchorTop = composerRect ? composerRect.top : rect.top
      const gap = 10

      setPosition({
        left,
        bottom: Math.max(8, window.innerHeight - anchorTop + gap),
        minWidth: width,
      })
    }
    compute()
    window.addEventListener('resize', compute)
    window.addEventListener('scroll', compute, true)
    return () => {
      window.removeEventListener('resize', compute)
      window.removeEventListener('scroll', compute, true)
    }
  }, [open, anchorRef, align, minWidth])

  useEffect(() => {
    if (!open) return
    const onPointer = (event: MouseEvent) => {
      const target = event.target as Node
      if (ref.current?.contains(target)) return
      if (anchorRef.current?.contains(target)) return
      onClose()
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    // Defer so the click that opened the popover does not immediately close it.
    const timer = window.setTimeout(() => {
      document.addEventListener('mousedown', onPointer)
    }, 0)
    window.addEventListener('keydown', onKey)
    return () => {
      window.clearTimeout(timer)
      document.removeEventListener('mousedown', onPointer)
      window.removeEventListener('keydown', onKey)
    }
  }, [open, onClose, anchorRef])

  if (!open || !position) return null

  return createPortal(
    <div
      ref={ref}
      className="composer-popover"
      style={{ left: position.left, bottom: position.bottom, minWidth: position.minWidth }}
      role="dialog"
    >
      {children}
    </div>,
    document.body,
  )
}

export function MenuItem({
  label,
  hint,
  selected,
  icon,
  onClick,
  danger,
}: {
  label: string
  hint?: string
  selected?: boolean
  icon?: React.ReactNode
  onClick: () => void
  danger?: boolean
}) {
  return (
    <button
      className="menu-item"
      data-selected={selected}
      data-danger={danger}
      onClick={onClick}
      type="button"
    >
      {icon && <span className="menu-item__icon">{icon}</span>}
      <span className="menu-item__text">
        <span>{label}</span>
        {hint && <small>{hint}</small>}
      </span>
      {selected && <Check size={14} className="menu-item__check" />}
    </button>
  )
}

/** Smooth three-stop segmented control with a sliding indicator. */
export function Segmented({
  options,
  value,
  onChange,
  size = 'regular',
  ariaLabel,
}: {
  options: { id: string; label: string; hint?: string }[]
  value: string
  onChange: (id: string) => void
  size?: 'small' | 'regular'
  ariaLabel: string
}) {
  const trackRef = useRef<HTMLDivElement>(null)
  const [indicator, setIndicator] = useState<{ left: number; width: number } | null>(null)
  const index = Math.max(0, options.findIndex((option) => option.id === value))

  // Measure the active button and pin the pill to it. A pure-CSS percentage
  // indicator drifts when the track's width settles after first paint (inline
  // flex containers, font loading), so geometry comes from the DOM instead.
  useLayoutEffect(() => {
    const track = trackRef.current
    if (!track) return
    const measure = () => {
      const button = track.querySelectorAll<HTMLButtonElement>('button')[index]
      if (!button) return
      const trackRect = track.getBoundingClientRect()
      const rect = button.getBoundingClientRect()
      setIndicator({ left: rect.left - trackRect.left, width: rect.width })
    }
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(track)
    return () => observer.disconnect()
  }, [index, options.length])

  return (
    <div
      ref={trackRef}
      className="segmented segmented--sliding"
      data-size={size}
      role="radiogroup"
      aria-label={ariaLabel}
    >
      <span
        className="segmented__indicator"
        aria-hidden
        style={
          indicator
            ? {
                width: indicator.width,
                transform: `translateX(${indicator.left - 3}px)`,
              }
            : { opacity: 0 }
        }
      />
      {options.map((option) => (
        <button
          key={option.id}
          role="radio"
          aria-checked={option.id === value}
          data-active={option.id === value}
          onClick={() => onChange(option.id)}
          type="button"
          title={option.hint}
        >
          {option.label}
        </button>
      ))}
    </div>
  )
}

/** Smooth slider for the thinking-depth dial. */
export function ThinkingSlider({
  stops,
  labels,
  value,
  onChange,
}: {
  stops: string[]
  labels: string[]
  value: string
  onChange: (id: string) => void
}) {
  const index = Math.max(0, stops.indexOf(value))
  const max = Math.max(1, stops.length - 1)
  return (
    <div className="thinking-slider">
      <div className="thinking-slider__head">
        <span className="thinking-slider__label">Thinking depth</span>
        <span className="thinking-slider__value">{labels[index] ?? 'Off'}</span>
      </div>
      <input
        type="range"
        min={0}
        max={max}
        step={1}
        value={index}
        onChange={(event) => onChange(stops[Number(event.target.value)] ?? stops[0])}
        aria-label="Thinking depth"
        style={{ ['--slider-progress' as string]: `${(index / max) * 100}%` }}
      />
      <div className="thinking-slider__ticks" aria-hidden>
        {labels.map((label, tick) => (
          <span key={label} data-active={tick <= index} data-current={tick === index}>
            {label}
          </span>
        ))}
      </div>
    </div>
  )
}