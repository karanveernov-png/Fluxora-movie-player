/**
 * AudioMenu — visual redesign only.
 * All logic preserved exactly.
 */

import { useState, useRef, useEffect } from 'react'
import type { AudioTrack } from '../../types'

interface AudioMenuProps {
  tracks: AudioTrack[]
  current: number
  onSelect: (id: number) => void
  icon?: React.ReactNode
}

export function AudioMenu({ tracks, current, onSelect, icon }: AudioMenuProps) {
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

  if (tracks.length <= 1) return null

  const currentTrack = tracks.find(t => t.id === current)

  return (
    <div className="menu-wrap" ref={wrapRef}>
      <button
        className="ctrl-btn"
        onClick={() => setOpen(o => !o)}
        aria-label={`Audio: ${currentTrack?.label ?? 'Track 1'}`}
        aria-haspopup="listbox"
        aria-expanded={open}
        title="Audio track"
        type="button"
      >
        {icon ?? '♪'}
      </button>
      {open && (
        <ul className="popup-menu" role="listbox" aria-label="Audio track">
          <li className="menu-section-label">Audio</li>
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
