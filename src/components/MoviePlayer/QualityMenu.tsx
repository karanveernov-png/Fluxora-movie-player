/**
 * QualityMenu — visual redesign only.
 * All logic preserved exactly.
 */

import { useState, useRef, useEffect } from 'react'
import type { QualityLevel } from '../../types'

interface QualityMenuProps {
  levels: QualityLevel[]
  current: number
  onSelect: (id: number) => void
}

export function QualityMenu({ levels, current, onSelect }: QualityMenuProps) {
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

  if (levels.length <= 1) return null

  const currentLabel = levels.find(l => l.id === current)?.label ?? 'Auto'

  return (
    <div className="menu-wrap" ref={wrapRef}>
      <button
        className="ctrl-btn"
        onClick={() => setOpen(o => !o)}
        aria-label={`Quality: ${currentLabel}`}
        aria-haspopup="listbox"
        aria-expanded={open}
        title="Video quality"
        type="button"
      >
        {current === -1 ? 'Auto' : currentLabel}
      </button>
      {open && (
        <ul className="popup-menu" role="listbox" aria-label="Video quality">
          <li className="menu-section-label">Quality</li>
          {levels.map(level => (
            <li
              key={level.id}
              role="option"
              aria-selected={level.id === current}
              className={`menu-item${level.id === current ? ' active' : ''}`}
              onClick={() => { onSelect(level.id); setOpen(false) }}
            >
              {level.label}
              {level.bitrate > 0 && (
                <span className="menu-item-sub">
                  {(level.bitrate / 1_000_000).toFixed(1)} Mbps
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
