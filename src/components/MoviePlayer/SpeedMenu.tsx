/**
 * SpeedMenu — visual redesign only.
 * All logic/state/event handlers preserved exactly.
 */

import { useState, useRef, useEffect } from 'react'

const SPEEDS = [0.25, 0.5, 0.75, 1, 1.25, 1.5, 1.75, 2]

interface SpeedMenuProps {
  current: number
  onSelect: (rate: number) => void
}

export function SpeedMenu({ current, onSelect }: SpeedMenuProps) {
  const [open, setOpen] = useState(false)
  const wrapRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const close = (e: MouseEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', close)
    return () => document.removeEventListener('mousedown', close)
  }, [open])

  const label = current === 1 ? '1×' : `${current}×`
  const isNonDefault = current !== 1

  return (
    <div className="menu-wrap" ref={wrapRef}>
      <button
        className={`ctrl-btn${isNonDefault ? ' speed-active' : ''}`}
        onClick={() => setOpen(o => !o)}
        aria-label={`Playback speed: ${label}`}
        aria-haspopup="listbox"
        aria-expanded={open}
        title="Playback speed"
        type="button"
      >
        {label}
      </button>
      {open && (
        <ul className="popup-menu" role="listbox" aria-label="Playback speed">
          <li className="menu-section-label">Speed</li>
          {SPEEDS.map(rate => (
            <li
              key={rate}
              role="option"
              aria-selected={rate === current}
              className={`menu-item${rate === current ? ' active' : ''}`}
              onClick={() => { onSelect(rate); setOpen(false) }}
            >
              {rate === 1 ? 'Normal' : `${rate}×`}
              <span className="menu-item-sub">{rate === 1 ? '1×' : ''}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
