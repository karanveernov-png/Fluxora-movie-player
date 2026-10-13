/**
 * MoviePlayer.tsx — Fluxora
 *
 * Changes from v1 (minimal, surgical):
 *  • AutoplayMutedBanner — shown when browser silenced autoplay; one-click unmute
 *  • OfflineBanner — shown when window goes offline during playback
 *  • useResume wired in: restores saved playback position per URL
 *  • container style changed from fixed aspectRatio to 100%/100% so
 *    it fills whatever space App gives it (App controls layout, not the player)
 *  • data-playing attribute kept for CSS auto-hide
 *
 * Everything else (controls, overlays, keyboard, HLS, DASH) unchanged.
 */

import {
  useRef, useState, useCallback, useEffect, forwardRef, type CSSProperties,
} from 'react'
import type { VideoPlayerState } from '../../types'
import { useVideoPlayer }  from '../../hooks/useVideoPlayer'
import { useResume }       from '../../hooks/useResume'
import { PlayerControls }  from './PlayerControls'
import { LoadingOverlay }  from './LoadingOverlay'
import { ErrorOverlay }    from './ErrorOverlay'
import type { MoviePlayerProps, PlayerHandle } from '../../types'
import './MoviePlayer.css'

export const MoviePlayer = forwardRef<PlayerHandle, MoviePlayerProps>(
  function MoviePlayer(props, ref) {
    const {
      source,
      autoPlay = false,
      muted    = false,
      controls = true,
      fallbackSources,
      className,
      style,
      onPlay, onPause, onEnded, onProgress, onError,
      onQualityChange, onAudioChange, onSubtitleChange,
      onTokenExpired,
      onStateChange,
    } = props

    const videoRef     = useRef<HTMLVideoElement>(null)
    const containerRef = useRef<HTMLDivElement>(null)
    const [controlsVisible, setControlsVisible] = useState(true)
    const [resumeToast, setResumeToast]         = useState<string | null>(null)
    const hideTimer    = useRef<ReturnType<typeof setTimeout> | null>(null)

    useEffect(() => () => { if (hideTimer.current) clearTimeout(hideTimer.current) }, [])

    const { state, currentTimeRef, actions } = useVideoPlayer(videoRef, containerRef, {
      source, autoPlay, muted, fallbackSources,
      onPlay, onPause, onEnded, onProgress, onError,
      onQualityChange, onAudioChange, onSubtitleChange,
      onTokenExpired,
      handleRef: ref,
    })

    // ── State change notifications ─────────────────────────────────────────────
    // Stable ref so the effect doesn't re-run when parent passes a new inline fn.
    const onStateChangeRef = useRef<((s: VideoPlayerState) => void) | undefined>(onStateChange)
    useEffect(() => { onStateChangeRef.current = onStateChange }, [onStateChange])

    // Notify parent on every discrete state change (play/pause/quality/etc.)
    // NOT 60fps — currentTime is tracked via ref, not React state.
    useEffect(() => {
      onStateChangeRef.current?.(state)
    }, [state])

    // ── Resume ────────────────────────────────────────────────────────────────
    useResume({
      url: source.type !== 'local' ? source.url : null,
      videoRef,
      enabled: true,
      onResumed: (t) => {
        const m = Math.floor(t / 60)
        const s = Math.floor(t % 60).toString().padStart(2, '0')
        setResumeToast(`Resumed at ${m}:${s}`)
        setTimeout(() => setResumeToast(null), 3000)
      },
    })

    // ── Controls auto-hide ────────────────────────────────────────────────────
    const showControls = useCallback(() => {
      setControlsVisible(true)
      if (hideTimer.current) clearTimeout(hideTimer.current)
      if (state.playing) {
        hideTimer.current = setTimeout(() => setControlsVisible(false), 3000)
      }
    }, [state.playing])

    useEffect(() => {
      if (!state.playing) {
        setControlsVisible(true)
        if (hideTimer.current) clearTimeout(hideTimer.current)
      }
    }, [state.playing])

    // ── Double-click → fullscreen ─────────────────────────────────────────────
    const handleDoubleClick = useCallback(() => actions.toggleFullscreen(), [actions])

    const containerStyle: CSSProperties = {
      width:  '100%',
      height: '100%',
      ...style,
    }

    const playerClass = [
      'movie-player',
      state.playing       ? 'player-playing'       : '',
      controlsVisible     ? 'controls-visible'      : '',
      state.fullscreen    ? 'player-fullscreen'     : '',
      className ?? '',
    ].filter(Boolean).join(' ')

    return (
      <div
        ref={containerRef}
        className={playerClass}
        style={containerStyle}
        onMouseMove={showControls}
        onMouseLeave={() => state.playing && setControlsVisible(false)}
        onTouchStart={showControls}
        tabIndex={0}
        aria-label="Video player"
      >
        <video
          ref={videoRef}
          className="player-video"
          playsInline
          muted={muted}
          preload="none"
          onClick={actions.togglePlay}
          onDoubleClick={handleDoubleClick}
          aria-label="Video"
        />

        {/* Offline banner */}
        {state.offline && (
          <div className="mp-offline-banner" role="alert">
            <span>📡 No connection — playback paused</span>
          </div>
        )}

        {/* Autoplay-muted unmute prompt */}
        {state.mutedForAutoplay && (
          <div className="mp-unmute-prompt">
            <span>▶ Playing muted — browser blocked audio autoplay</span>
            <button
              className="mp-unmute-btn"
              onClick={() => { actions.toggleMute() }}
              type="button"
            >
              Unmute
            </button>
          </div>
        )}

        {/* Resume toast */}
        {resumeToast && (
          <div className="mp-toast" role="status">{resumeToast}</div>
        )}

        <LoadingOverlay visible={state.buffering && !state.error} />

        {state.error && (
          <ErrorOverlay message={state.error} onRetry={actions.reload} />
        )}

        {controls && !state.error && (
          <PlayerControls
            state={state}
            currentTimeRef={currentTimeRef}
            onPlayPause={actions.togglePlay}
            onSeek={actions.seek}
            onVolumeChange={actions.setVolume}
            onMuteToggle={actions.toggleMute}
            onQuality={actions.setQuality}
            onAudio={actions.setAudioTrack}
            onSubtitle={actions.setSubtitleTrack}
            onSpeed={actions.setPlaybackRate}
            onPip={actions.togglePip}
            onFullscreen={actions.toggleFullscreen}
          />
        )}
      </div>
    )
  }
)
