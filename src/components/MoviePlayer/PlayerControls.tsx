/**
 * PlayerControls — Fluxora
 * Visual redesign only. All props/callback signatures preserved exactly.
 * Time display via direct DOM — no React re-renders at 60fps (unchanged).
 */

import { useRef, useEffect, type MutableRefObject } from 'react'
import { ProgressBar } from './ProgressBar'
import { QualityMenu } from './QualityMenu'
import { AudioMenu } from './AudioMenu'
import { SubtitleMenu } from './SubtitleMenu'
import { SpeedMenu } from './SpeedMenu'
import { formatTime } from '../../utils/formatTime'
import type { VideoPlayerState } from '../../hooks/useVideoPlayer'

interface PlayerControlsProps {
  state: VideoPlayerState
  currentTimeRef: MutableRefObject<number>
  onPlayPause: () => void
  onSeek: (t: number) => void
  onVolumeChange: (v: number) => void
  onMuteToggle: () => void
  onQuality: (id: number) => void
  onAudio: (id: number) => void
  onSubtitle: (id: number) => void
  onSpeed: (rate: number) => void
  onPip: () => void
  onFullscreen: () => void
}

// ─── SVG icons — clean, consistent 20×20 grid ────────────────────────────────

const PlayIcon = () => (
  <svg viewBox="0 0 24 24" width="22" height="22" fill="currentColor" aria-hidden="true">
    <path d="M8 5.14v14l11-7-11-7z" />
  </svg>
)
const PauseIcon = () => (
  <svg viewBox="0 0 24 24" width="22" height="22" fill="currentColor" aria-hidden="true">
    <path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z" />
  </svg>
)

const VolumeHighIcon = () => (
  <svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor" aria-hidden="true">
    <path d="M3 9v6h4l5 5V4L7 9H3zm13.5 3c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02zM14 3.23v2.06c2.89.86 5 3.54 5 6.71s-2.11 5.85-5 6.71v2.06c4.01-.91 7-4.49 7-8.77s-2.99-7.86-7-8.77z" />
  </svg>
)
const VolumeLowIcon = () => (
  <svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor" aria-hidden="true">
    <path d="M18.5 12c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02zM5 9v6h4l5 5V4L9 9H5z" />
  </svg>
)
const VolumeMuteIcon = () => (
  <svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor" aria-hidden="true">
    <path d="M16.5 12c0-1.77-1.02-3.29-2.5-4.03v2.21l2.45 2.45c.03-.2.05-.41.05-.63zm2.5 0c0 .94-.2 1.82-.54 2.64l1.51 1.51C20.63 14.91 21 13.5 21 12c0-4.28-2.99-7.86-7-8.77v2.06c2.89.86 5 3.54 5 6.71zM4.27 3L3 4.27 7.73 9H3v6h4l5 5v-6.73l4.25 4.25c-.67.52-1.42.93-2.25 1.18v2.06c1.38-.31 2.63-.95 3.69-1.81L19.73 21 21 19.73l-9-9L4.27 3zM12 4L9.91 6.09 12 8.18V4z" />
  </svg>
)

function VolumeIcon({ muted, volume }: { muted: boolean; volume: number }) {
  if (muted || volume === 0) return <VolumeMuteIcon />
  if (volume < 0.5) return <VolumeLowIcon />
  return <VolumeHighIcon />
}

const FullscreenEnterIcon = () => (
  <svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor" aria-hidden="true">
    <path d="M7 14H5v5h5v-2H7v-3zm-2-4h2V7h3V5H5v5zm12 7h-3v2h5v-5h-2v3zM14 5v2h3v3h2V5h-5z" />
  </svg>
)
const FullscreenExitIcon = () => (
  <svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor" aria-hidden="true">
    <path d="M5 16h3v3h2v-5H5v2zm3-8H5v2h5V5H8v3zm6 11h2v-3h3v-2h-5v5zm2-11V5h-2v5h5V8h-3z" />
  </svg>
)

const AudioIcon = () => (
  <svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor" aria-hidden="true">
    <path d="M12 3v10.55c-.59-.34-1.27-.55-2-.55-2.21 0-4 1.79-4 4s1.79 4 4 4 4-1.79 4-4V7h4V3h-6z" />
  </svg>
)

const SubtitleIcon = ({ active }: { active: boolean }) => (
  <svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor" aria-hidden="true"
    style={{ opacity: active ? 1 : 0.65 }}>
    <path d="M20 4H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V6c0-1.1-.9-2-2-2zm-8 11H4v-2h8v2zm8 0h-6v-2h6v2zm0-4H4V9h16v2z" />
  </svg>
)

const PipIcon = ({ active }: { active: boolean }) => (
  <svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor" aria-hidden="true"
    style={{ opacity: active ? 1 : 0.75 }}>
    <path d="M19 7h-8v6h8V7zm2-4H3c-1.1 0-2 .9-2 2v14c0 1.1.9 1.99 2 1.99h18c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm0 16.01H3V4.99h18v14.02z" />
  </svg>
)

// ─── Component ────────────────────────────────────────────────────────────────

export function PlayerControls({
  state, currentTimeRef,
  onPlayPause, onSeek,
  onVolumeChange, onMuteToggle,
  onQuality, onAudio, onSubtitle,
  onSpeed, onPip,
  onFullscreen,
}: PlayerControlsProps) {
  // Time display via direct DOM — no React re-renders at 60fps (preserved)
  const timeDisplayRef = useRef<HTMLSpanElement>(null)
  const rafRef = useRef<number>(0)

  useEffect(() => {
    const dur = state.duration
    const update = () => {
      if (timeDisplayRef.current) {
        timeDisplayRef.current.textContent =
          `${formatTime(currentTimeRef.current)} / ${formatTime(dur)}`
      }
      rafRef.current = requestAnimationFrame(update)
    }
    rafRef.current = requestAnimationFrame(update)
    return () => cancelAnimationFrame(rafRef.current)
  }, [state.duration, currentTimeRef])

  // Determine if there are any settings to show in the settings group
  const hasSubtitles  = state.subtitleTracks.length > 0
  const hasAudio      = state.audioTracks.length > 1
  const hasQuality    = state.qualityLevels.length > 1

  return (
    <div className="controls">
      {/* ── Progress bar ── */}
      <ProgressBar
        duration={state.duration}
        currentTimeRef={currentTimeRef}
        onSeek={onSeek}
        buffered={state.bufferedAhead}
      />

      <div className="controls-row">
        {/* ── Left group ── */}
        <div className="ctrl-group">
          {/* Play/Pause */}
          <button
            className="ctrl-btn play-btn"
            onClick={onPlayPause}
            aria-label={state.playing ? 'Pause' : 'Play'}
            title={state.playing ? 'Pause (Space)' : 'Play (Space)'}
            type="button"
          >
            {state.playing ? <PauseIcon /> : <PlayIcon />}
          </button>

          {/* Volume */}
          <div className="volume-wrap">
            <button
              className="ctrl-btn"
              onClick={onMuteToggle}
              aria-label={state.muted ? 'Unmute' : 'Mute'}
              title="Mute (M)"
              type="button"
            >
              <VolumeIcon muted={state.muted} volume={state.volume} />
            </button>
            <input
              type="range"
              className="volume-slider"
              min={0} max={1} step={0.02}
              value={state.muted ? 0 : state.volume}
              onChange={e => onVolumeChange(parseFloat(e.target.value))}
              aria-label="Volume"
              style={{ '--vol': `${(state.muted ? 0 : state.volume) * 100}%` } as React.CSSProperties}
            />
          </div>

          {/* Time */}
          <span className="time-display" ref={timeDisplayRef} aria-live="off">
            0:00 / 0:00
          </span>
        </div>

        {/* ── Right group ── */}
        <div className="ctrl-group">
          {/* Subtitle — only when tracks exist */}
          {hasSubtitles && (
            <SubtitleMenu
              tracks={state.subtitleTracks}
              current={state.currentSubtitle}
              onSelect={onSubtitle}
              icon={<SubtitleIcon active={state.currentSubtitle !== -1} />}
            />
          )}

          {/* Audio — only when multiple tracks */}
          {hasAudio && (
            <AudioMenu
              tracks={state.audioTracks}
              current={state.currentAudio}
              onSelect={onAudio}
              icon={<AudioIcon />}
            />
          )}

          {/* Speed */}
          <SpeedMenu
            current={state.playbackRate}
            onSelect={onSpeed}
          />

          {/* Quality — only when HLS levels available */}
          {hasQuality && (
            <QualityMenu
              levels={state.qualityLevels}
              current={state.currentQuality}
              onSelect={onQuality}
            />
          )}

          {/* PiP */}
          {state.pipSupported && (
            <button
              className={`ctrl-btn${state.pip ? ' pip-active' : ''}`}
              onClick={onPip}
              aria-label={state.pip ? 'Exit picture-in-picture' : 'Picture-in-picture'}
              title="Picture-in-picture (I)"
              type="button"
            >
              <PipIcon active={state.pip} />
            </button>
          )}

          {/* Fullscreen */}
          <button
            className="ctrl-btn"
            onClick={onFullscreen}
            aria-label={state.fullscreen ? 'Exit fullscreen' : 'Fullscreen'}
            title={state.fullscreen ? 'Exit fullscreen (F)' : 'Fullscreen (F)'}
            type="button"
          >
            {state.fullscreen ? <FullscreenExitIcon /> : <FullscreenEnterIcon />}
          </button>
        </div>
      </div>
    </div>
  )
}