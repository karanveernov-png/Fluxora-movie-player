/**
 * ProgressBar
 * Updates the visual bar via direct DOM manipulation using requestAnimationFrame.
 *
 * Improvements over original:
 * - Touch drag support for mobile scrubbing
 * - Seek-time tooltip on hover
 * - Touch-action: none prevents page scroll while scrubbing
 */

import { useEffect, useRef, useState, useCallback, type MutableRefObject } from 'react'
import { formatTime } from '../../utils/formatTime'

interface ProgressBarProps {
  duration: number
  currentTimeRef: MutableRefObject<number>
  onSeek: (seconds: number) => void
  buffered?: number
}

export function ProgressBar({ duration, currentTimeRef, onSeek, buffered = 0 }: ProgressBarProps) {
  const barRef    = useRef<HTMLDivElement>(null)
  const bufferRef = useRef<HTMLDivElement>(null)
  const thumbRef  = useRef<HTMLDivElement>(null)
  const trackRef  = useRef<HTMLDivElement>(null)
  const rafRef    = useRef<number>(0)
  const isDragging = useRef(false)

  // Tooltip: only position + label, avoids re-rendering on mouse move
  const [tooltip, setTooltip] = useState<{ pct: number; label: string } | null>(null)

  // ─── RAF loop ─────────────────────────────────────────────────────────────
  useEffect(() => {
    const update = () => {
      const ct = currentTimeRef.current
      if (!isDragging.current && duration > 0) {
        const pct = (ct / duration) * 100
        if (barRef.current)  barRef.current.style.width  = `${pct}%`
        if (thumbRef.current) thumbRef.current.style.left = `${pct}%`
      }
      if (bufferRef.current && duration > 0) {
        const bufPct = Math.min(100, ((ct + buffered) / duration) * 100)
        bufferRef.current.style.width = `${bufPct}%`
      }
      trackRef.current?.setAttribute('aria-valuenow', String(Math.round(ct)))
      rafRef.current = requestAnimationFrame(update)
    }
    rafRef.current = requestAnimationFrame(update)
    return () => cancelAnimationFrame(rafRef.current)
  }, [duration, currentTimeRef, buffered])

  // ─── Shared seek helper ───────────────────────────────────────────────────
  const seekFromClientX = useCallback((clientX: number, trackEl: HTMLDivElement) => {
    // duration === 0 means unknown (live stream or not yet loaded); seeking
    // into Infinity would throw a DOM error, so bail out early.
    if (!isFinite(duration) || duration <= 0) return
    const rect  = trackEl.getBoundingClientRect()
    const ratio = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width))
    const time  = ratio * duration
    onSeek(time)
    if (barRef.current)  barRef.current.style.width  = `${ratio * 100}%`
    if (thumbRef.current) thumbRef.current.style.left = `${ratio * 100}%`
  }, [duration, onSeek])

  // ─── Mouse drag ───────────────────────────────────────────────────────────
  const handleMouseDown = (e: React.MouseEvent<HTMLDivElement>) => {
    e.preventDefault()
    isDragging.current = true
    const track = e.currentTarget as HTMLDivElement
    seekFromClientX(e.clientX, track)

    const onMove = (ev: MouseEvent) => {
      const rect  = track.getBoundingClientRect()
      const ratio = Math.max(0, Math.min(1, (ev.clientX - rect.left) / rect.width))
      if (barRef.current)  barRef.current.style.width  = `${ratio * 100}%`
      if (thumbRef.current) thumbRef.current.style.left = `${ratio * 100}%`
      onSeek(ratio * duration)
    }
    const onUp = () => {
      isDragging.current = false
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
    }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
  }

  // ─── Touch drag ───────────────────────────────────────────────────────────
  const handleTouchStart = (e: React.TouchEvent<HTMLDivElement>) => {
    isDragging.current = true
    const track = e.currentTarget as HTMLDivElement
    seekFromClientX(e.touches[0].clientX, track)

    const onMove = (ev: TouchEvent) => {
      ev.preventDefault()
      const rect  = track.getBoundingClientRect()
      const ratio = Math.max(0, Math.min(1, (ev.touches[0].clientX - rect.left) / rect.width))
      if (barRef.current)  barRef.current.style.width  = `${ratio * 100}%`
      if (thumbRef.current) thumbRef.current.style.left = `${ratio * 100}%`
      onSeek(ratio * duration)
    }
    const onEnd = () => {
      isDragging.current = false
      window.removeEventListener('touchmove', onMove)
      window.removeEventListener('touchend', onEnd)
    }
    // passive: false so ev.preventDefault() stops page scroll while scrubbing
    window.addEventListener('touchmove', onMove, { passive: false })
    window.addEventListener('touchend', onEnd)
  }

  // ─── Hover tooltip ────────────────────────────────────────────────────────
  const handleMouseMove = (e: React.MouseEvent<HTMLDivElement>) => {
    if (!duration) return
    const rect  = e.currentTarget.getBoundingClientRect()
    const ratio = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width))
    // Clamp so tooltip doesn't overflow at the edges
    const pct   = Math.min(95, Math.max(5, ratio * 100))
    setTooltip({ pct, label: formatTime(ratio * duration) })
  }

  return (
    <div className="progress-wrap">
      <div
        ref={trackRef}
        className="progress-track"
        onMouseDown={handleMouseDown}
        onTouchStart={handleTouchStart}
        onMouseMove={handleMouseMove}
        onMouseLeave={() => setTooltip(null)}
        role="slider"
        aria-label="Seek"
        aria-valuemin={0}
        aria-valuemax={Math.round(duration)}
        aria-valuenow={0}
        tabIndex={0}
      >
        <div className="progress-buffer" ref={bufferRef} />
        <div className="progress-fill"   ref={barRef}    />
        <div className="progress-thumb"  ref={thumbRef}  />

        {tooltip && duration > 0 && (
          <div className="seek-tooltip" style={{ left: `${tooltip.pct}%` }}>
            {tooltip.label}
          </div>
        )}
      </div>
    </div>
  )
}
