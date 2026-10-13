/**
 * SubtitleMenu — visual redesign only.
 * All logic preserved exactly.
 */

import { useState, useRef, useEffect } from 'react'
import type { SubtitleTrack } from '../../types'

interface SubtitleMenuProps {
  tracks: SubtitleTrack[]
  current: number
  onSelect: (id: number) => void
  icon?: React.ReactNode
}

export function SubtitleMenu({ tracks, current, onSelect, icon }: SubtitleMenuProps) {
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

  if (tracks.length === 0) return null

  const isOff = current === -1
  const currentTrack = tracks.find(t => t.id === current)

  return (
    <div className="menu-wrap" ref={wrapRef}>
      <button
        className={`ctrl-btn${!isOff ? ' active-cc' : ''}`}
        onClick={() => setOpen(o => !o)}
        aria-label={`Subtitles: ${isOff ? 'Off' : (currentTrack?.label ?? 'On')}`}
        aria-haspopup="listbox"
        aria-expanded={open}
        title="Subtitles"
        type="button"
      >
        {icon ?? 'CC'}
      </button>
      {open && (
        <ul className="popup-menu" role="listbox" aria-label="Subtitles">
          <li className="menu-section-label">Subtitles</li>
          <li
            role="option"
            aria-selected={isOff}
            className={`menu-item${isOff ? ' active' : ''}`}
            onClick={() => { onSelect(-1); setOpen(false) }}
          >
            Off
          </li>
          {tracks.map(track => (
            <li
              key={track.id}
              role="option"
              aria-selected={track.id === current}
              className={`menu-item${track.id === current ? ' active' : ''}`}
              onClick={() => { onSelect(track.id); setOpen(false) }}
            >
              {track.label}
              {track.language && (
                <span className="menu-item-sub">{track.language.toUpperCase()}</span>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
