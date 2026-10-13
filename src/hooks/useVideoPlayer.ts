/**
 * useVideoPlayer.ts — Fluxora v2
 *
 * Changes from v1:
 *  • DASH source type routed to dash.js engine (lazy-loaded)
 *  • 'remuxed' and 'hls_generated' source types supported
 *  • Progress timer paused when video is paused (saves CPU)
 *  • currentTimeRef exported so useResume can read it without
 *    causing extra re-renders
 *  • Autoplay-policy: NotAllowedError → muted retry + state flag
 *  • Offline/online event handling (pause retries, resume on reconnect)
 *  • setPlaybackRate now also persists across source changes via ref
 *
 * Everything already good in v1 is preserved exactly.
 */

import {
  useRef, useState, useEffect, useCallback, useImperativeHandle,
  type RefObject,
} from 'react'
import type { ForwardedRef } from 'react'
// Type-only import — erased at build time, zero bundle cost.
// The HLSManager class (and hls.js) are loaded dynamically the first time an
// HLS source is played, keeping them out of the initial JS bundle entirely.
import type { HLSManager } from '../core/HLSManager'
import { PlaybackManager } from '../core/PlaybackManager'

import type {
  PlayerSource, PlayerCallbacks, PlayerHandle, VideoPlayerState,
} from '../types'

// Re-export so existing consumers (PlayerControls, MediaInfoPanel) keep working
export type { VideoPlayerState }

interface UseVideoPlayerOptions extends PlayerCallbacks {
  source: PlayerSource
  autoPlay: boolean
  muted: boolean
  fallbackSources?: PlayerSource[]
  handleRef?: ForwardedRef<PlayerHandle>
  /**
   * Called by HLSManager when the proxy manifest returns 403, indicating the
   * CDN signed-URL token has expired.
   *
   * The implementation must obtain a fresh signed CDN URL (from your auth
   * backend) and return it together with any updated auth headers.
   * Return null if the refresh cannot be completed (e.g. user is logged out).
   *
   * On a successful return HLSManager calls refreshProxySession() and then
   * re-initialises hls.js against the same proxy URL transparently.
   *
   * @param token  The 32-char proxy session token (safe to log).
   */
  onTokenExpired?: (token: string) => Promise<{ url: string; headers: Record<string, string> } | null>
}

const PROGRESS_INTERVAL = 250
const SPEEDS = [0.25, 0.5, 0.75, 1, 1.25, 1.5, 1.75, 2]

export function useVideoPlayer(
  videoRef: RefObject<HTMLVideoElement | null>,
  containerRef: RefObject<HTMLDivElement | null>,
  options: UseVideoPlayerOptions,
) {
  const {
    source, autoPlay, muted: initialMuted, fallbackSources,
    onPlay, onPause, onEnded, onProgress, onError,
    onQualityChange, onAudioChange, onSubtitleChange,
    onTokenExpired,
    handleRef,
  } = options

  const hlsManager    = useRef<HLSManager | null>(null)
  // dash.js player — loaded lazily; typed as `any` to avoid bundling dashjs types
  const dashPlayerRef = useRef<any>(null)
  const playbackMgr   = useRef(new PlaybackManager())
  const fallbackIndex = useRef(0)
  const rateRef       = useRef(1)   // persists rate across source changes
  // Incremented on every loadSource call so stale playWithPolicy invocations
  // from a previous source don't trigger play() on the newly-loaded video.
  // This is the root cause of the AbortError: the old source's onManifestParsed
  // fires play() just as the new source has called pause() via video.src = newUrl.
  const loadGenRef    = useRef(0)

  // Stable callback refs (prevents stale closures without adding to effect deps)
  const onErrorRef        = useRef(onError)
  const onPlayRef         = useRef(onPlay)
  const onPauseRef        = useRef(onPause)
  const onEndedRef        = useRef(onEnded)
  const onTokenExpiredRef = useRef(onTokenExpired)
  const onProgressRef     = useRef(onProgress)   // B11 fix: stable ref prevents interval churn
  useEffect(() => { onErrorRef.current        = onError        }, [onError])
  useEffect(() => { onPlayRef.current         = onPlay         }, [onPlay])
  useEffect(() => { onPauseRef.current        = onPause        }, [onPause])
  useEffect(() => { onEndedRef.current        = onEnded        }, [onEnded])
  useEffect(() => { onTokenExpiredRef.current = onTokenExpired }, [onTokenExpired])
  useEffect(() => { onProgressRef.current     = onProgress     }, [onProgress])

  const [state, setState] = useState<VideoPlayerState>({
    playing: false,
    buffering: false,
    ready: false,
    error: null,
    duration: 0,
    bufferedAhead: 0,
    qualityLevels: [],
    audioTracks: [],
    subtitleTracks: [],
    currentQuality: -1,
    currentAudio: 0,
    currentSubtitle: -1,
    volume: 1,
    muted: initialMuted,
    mutedForAutoplay: false,
    fullscreen: false,
    playbackRate: 1,
    pip: false,
    pipSupported: false,
    offline: false,
  })

  // Refs for values read in tight loops without triggering renders
  const currentTimeRef = useRef(0)
  const durationRef    = useRef(0)
  const playingRef     = useRef(false)   // mirrors state.playing without closure risk
  const progressTimerRef = useRef<ReturnType<typeof setInterval> | null>(null)

  // ── Autoplay with policy handling ─────────────────────────────────────────
  // Each call captures the current generation; if the source changes before
  // play() resolves, the callback is a no-op, eliminating AbortError noise.
  const playWithPolicy = useCallback(async (video: HTMLVideoElement, gen?: number) => {
    const myGen = gen ?? loadGenRef.current
    try {
      await video.play()
      // If the source changed while we were awaiting play(), discard the result
      if (loadGenRef.current !== myGen) return
      setState(s => ({ ...s, mutedForAutoplay: false }))
    } catch (err: any) {
      if (loadGenRef.current !== myGen) return  // stale — new source already loading
      if (err?.name === 'NotAllowedError') {
        // Browser blocked unmuted autoplay — retry muted
        video.muted = true
        try {
          await video.play()
          if (loadGenRef.current !== myGen) return
          setState(s => ({ ...s, mutedForAutoplay: true }))
        } catch {
          // Even muted autoplay blocked (e.g. data-saver mode) — show controls
          setState(s => ({ ...s, buffering: false }))
        }
      } else if (err?.name === 'AbortError') {
        // Interrupted by a subsequent load() — harmless, already guarded above.
      } else {
        // Unrecoverable: codec unsupported, CORS blocked, network error, etc.
        const msg = (err as Error)?.message ?? 'Unable to play this stream'
        setState(s => ({ ...s, error: msg, buffering: false }))
        onErrorRef.current?.(msg)
      }
    }
  }, [])

  // ── Tear down DASH player ─────────────────────────────────────────────────
  const teardownDash = useCallback(() => {
    if (dashPlayerRef.current) {
      try { dashPlayerRef.current.reset() } catch {}
      dashPlayerRef.current = null
    }
  }, [])

  // ── Source loading ────────────────────────────────────────────────────────
  const loadSource = useCallback(async (src: PlayerSource) => {
    const video = videoRef.current
    if (!video) return

    // Bump the generation counter. Any in-flight playWithPolicy from the
    // previous source will see a different generation and bail out silently,
    // preventing the AbortError: play() interrupted by pause().
    const gen = ++loadGenRef.current

    setState(s => ({
      ...s,
      ready: false,
      error: null,
      buffering: true,
      qualityLevels: [],
      audioTracks: [],
      subtitleTracks: [],
      currentQuality: -1,
      currentAudio: 0,
      currentSubtitle: -1,
    }))

    // ── HLS ──────────────────────────────────────────────────────────────────
    // hls.js lives in its own chunk, fetched on the first HLS play (the browser
    // caches it for later plays). No modulepreload hint is emitted, so that first
    // HLS play waits for the chunk download.
    if (src.type === 'hls' || src.type === 'hls_generated') {
      teardownDash()
      hlsManager.current?.destroy()

      let HLSCtor: typeof HLSManager
      try {
        HLSCtor = (await import('../core/HLSManager')).HLSManager
      } catch {
        // Chunk failed to load (offline, flaky network, stale deploy). Surface an
        // error like a failed dash.js script load does, instead of leaving the
        // player buffering forever behind an unhandled promise rejection.
        if (gen !== loadGenRef.current) return
        const msg = 'Failed to load the HLS player component. Check your connection and reload the page.'
        setState(s => ({ ...s, error: msg, buffering: false }))
        onErrorRef.current?.(msg)
        return
      }

      // Guard: if the source changed (or the player unmounted) while the async
      // import was in flight, abort — the generation counter was bumped.
      if (gen !== loadGenRef.current) return

      const mgr = new HLSCtor({
        onLevelsLoaded:        (levels)  => setState(s => ({ ...s, qualityLevels: levels })),
        onAudioTracksLoaded:   (tracks)  => setState(s => ({ ...s, audioTracks: tracks })),
        onSubtitleTracksLoaded:(tracks)  => setState(s => ({ ...s, subtitleTracks: tracks })),
        onManifestParsed: () => {
          // Restore playback rate
          if (video.playbackRate !== rateRef.current) {
            video.playbackRate = rateRef.current
          }
          if (autoPlay) playWithPolicy(video, gen)
        },
        onError: (message, fatal) => {
          if (fatal) {
            const fallbacks = fallbackSources ?? []
            if (fallbackIndex.current < fallbacks.length) {
              loadSource(fallbacks[fallbackIndex.current++])
            } else {
              setState(s => ({ ...s, error: message, buffering: false }))
              onErrorRef.current?.(message)
            }
          }
        },
        onLevelSwitched: (levelId) => setState(s => ({ ...s, currentQuality: levelId })),

        // ── CDN token expiry on live streams ────────────────────────────────
        // HLSManager calls this when the proxy manifest returns 403.
        // Forward to the app-level callback which returns the original CDN
        // URL + headers. HLSManager.attemptTokenRefresh then calls
        // refreshProxySession() internally — do NOT call it here too.
        // The previous double-call sent two identical POSTs to
        // /media/proxy/session/{token}/refresh for every 403 event.
        onTokenExpired: onTokenExpiredRef.current
          ? async (token: string) => {
              const cb = onTokenExpiredRef.current
              if (!cb) return null
              return cb(token)
            }
          : undefined,
      })

      hlsManager.current = mgr
      mgr.attach(video, src.url)

    // ── DASH ─────────────────────────────────────────────────────────────────
    } else if (src.type === 'dash') {
      hlsManager.current?.destroy()
      hlsManager.current = null
      teardownDash()

      // Bug #13a: the old code appended a new <script> tag every time a DASH
      // source was loaded even when dash.js was already on window — because the
      // if (window.dashjs) branch called initDash() immediately (correct) but
      // the else branch appended a SECOND tag if the script was already loading
      // (the onload event hadn't fired yet).  Guard with a sentinel attribute.
      const DASH_SRC = 'https://cdnjs.cloudflare.com/ajax/libs/dashjs/4.7.4/dash.all.min.js'
      const DASH_SENTINEL = 'data-dashjs-loading'

      const initDash = (dashjs: any) => {
        // Bug #13b: all DASH event callbacks captured `gen` from the outer
        // closure, but they never checked whether the source had changed since
        // the player was created.  Add a generation guard to every handler.
        const player = dashjs.MediaPlayer().create()
        dashPlayerRef.current = player
        player.initialize(video, src.url, autoPlay)

        player.on(dashjs.MediaPlayer.events.STREAM_INITIALIZED, () => {
          if (loadGenRef.current !== gen) return   // stale — new source loading
          if (autoPlay) void playWithPolicy(video, gen)
        })
        player.on(dashjs.MediaPlayer.events.ERROR, (e: any) => {
          if (loadGenRef.current !== gen) return   // stale — ignore
          const msg = e?.error?.message ?? 'DASH playback error'
          setState(s => ({ ...s, error: msg, buffering: false }))
          onErrorRef.current?.(msg)
        })
        player.on(dashjs.MediaPlayer.events.QUALITY_CHANGE_RENDERED, () => {
          if (loadGenRef.current !== gen) return   // stale — ignore
          const info = player.getVideoElement()
          if (info) setState(s => ({ ...s, currentQuality: player.getQualityFor('video') }))
        })
      }

      if ((window as any).dashjs) {
        // Library already loaded — init immediately
        initDash((window as any).dashjs)
      } else if (document.querySelector(`script[${DASH_SENTINEL}]`)) {
        // Script tag already injected (loading in progress) — wait for it
        // B10 fix: check gen before acting so a post-unmount load doesn't call setState
        const existing = document.querySelector(`script[${DASH_SENTINEL}]`) as HTMLScriptElement
        existing.addEventListener('load',  () => {
          if (loadGenRef.current !== gen) return  // B10: stale — component unmounted or source changed
          initDash((window as any).dashjs)
        }, { once: true })
        existing.addEventListener('error', () => {
          if (loadGenRef.current !== gen) return  // B10: stale
          setState(s => ({ ...s, error: 'Failed to load DASH player library', buffering: false }))
        }, { once: true })
      } else {
        const script = document.createElement('script')
        script.src = DASH_SRC
        script.setAttribute(DASH_SENTINEL, '1')
        script.onload = () => {
          script.removeAttribute(DASH_SENTINEL)
          initDash((window as any).dashjs)
        }
        script.onerror = () => {
          script.removeAttribute(DASH_SENTINEL)
          if (loadGenRef.current !== gen) return
          setState(s => ({ ...s, error: 'Failed to load DASH player library', buffering: false }))
        }
        document.head.appendChild(script)
      }

    // ── MP4 / WebM / Local / Remuxed ─────────────────────────────────────────
    } else if (
      src.type === 'mp4' ||
      src.type === 'local' ||
      src.type === 'remuxed'
    ) {
      hlsManager.current?.destroy()
      hlsManager.current = null
      teardownDash()

      video.src = src.url
      video.playbackRate = rateRef.current

      if (autoPlay) {
        // Call play() immediately — do NOT wait for loadedmetadata first.
        //
        // The old pattern (addEventListener 'loadedmetadata' → playWithPolicy)
        // is a hard deadlock when <video preload="none"> is used:
        //   1. video.src = url  →  readyState stays HAVE_NOTHING (0)
        //   2. We wait for loadedmetadata to call play()
        //   3. But with preload="none" the browser makes NO network request
        //      until play() is called — so loadedmetadata never fires.
        //
        // The HTML spec is explicit: calling play() on an element with
        // preload="none" overrides the preload hint and starts fetching.
        // useResume's own { once: true } loadedmetadata listener fires
        // *between* this play() call and canplay, so the saved-position
        // seek is applied before the first frame renders.
        playWithPolicy(video, gen)
      }

    } else {
      const t = (src as PlayerSource).type
      const msg = `Source type "${t}" is not supported`
      setState(s => ({ ...s, error: msg, buffering: false }))
      onErrorRef.current?.(msg)
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoPlay, fallbackSources, videoRef, playWithPolicy, teardownDash])

  // ── Mount / source changes ────────────────────────────────────────────────
  useEffect(() => {
    const video = videoRef.current
    if (!video) return

    playbackMgr.current.attach(video)
    fallbackIndex.current = 0
    loadSource(source)

    setState(s => ({
      ...s,
      pipSupported:
        'pictureInPictureEnabled' in document &&
        !!(document as any).pictureInPictureEnabled,
    }))

    return () => {
      // Invalidate any loadSource() still awaiting the lazy HLS chunk, so it
      // cannot create + attach a manager after unmount / source change.
      loadGenRef.current++
      hlsManager.current?.destroy()
      hlsManager.current = null
      teardownDash()
      playbackMgr.current.detach()
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [source])

  // ── Offline / online ──────────────────────────────────────────────────────
  useEffect(() => {
    const onOffline = () => {
      setState(s => ({ ...s, offline: true }))
      videoRef.current?.pause()
    }
    const onOnline = () => {
      setState(s => ({ ...s, offline: false }))
      // Resume HLS if it had stalled
      if (hlsManager.current) {
        try { hlsManager.current['hls']?.startLoad() } catch {}
      }
      // Bug #1: use playWithPolicy so NotAllowedError is caught and muted retry fires
      if (playingRef.current && videoRef.current?.paused) {
        void playWithPolicy(videoRef.current)
      }
    }
    window.addEventListener('offline', onOffline)
    window.addEventListener('online',  onOnline)
    return () => {
      window.removeEventListener('offline', onOffline)
      window.removeEventListener('online',  onOnline)
    }
  }, [videoRef, playWithPolicy])

  // ── Media Session ─────────────────────────────────────────────────────────
  useEffect(() => {
    if (!('mediaSession' in navigator)) return
    navigator.mediaSession.metadata = new MediaMetadata({ title: 'Fluxora' })

    const video = videoRef.current
    // Bug #1: bare video.play() → uncaught AbortError / NotAllowedError.
    navigator.mediaSession.setActionHandler('play',  () => { if (video) void playWithPolicy(video) })
    navigator.mediaSession.setActionHandler('pause', () => video?.pause())
    navigator.mediaSession.setActionHandler('seekbackward', (d) => {
      if (video && isFinite(video.currentTime)) {
        video.currentTime = Math.max(0, video.currentTime - (d.seekOffset ?? 10))
      }
    })
    navigator.mediaSession.setActionHandler('seekforward', (d) => {
      // Bug #12: video.duration is NaN before loadedmetadata → clamp only if finite
      if (video && isFinite(video.currentTime)) {
        const target = video.currentTime + (d.seekOffset ?? 10)
        video.currentTime = isFinite(video.duration) ? Math.min(video.duration, target) : target
      }
    })
    navigator.mediaSession.setActionHandler('seekto', (d) => {
      // Bug #12: only seek if seekTime is a finite number
      if (video && d.seekTime != null && isFinite(d.seekTime)) video.currentTime = d.seekTime
    })
    return () => {
      try {
        ;['play','pause','seekbackward','seekforward','seekto'].forEach(a =>
          navigator.mediaSession.setActionHandler(a as MediaSessionAction, null)
        )
      } catch {}
    }
  }, [videoRef, playWithPolicy])

  useEffect(() => {
    if (!('mediaSession' in navigator)) return
    navigator.mediaSession.playbackState = state.playing ? 'playing' : 'paused'
  }, [state.playing])

  useEffect(() => {
    if (!('mediaSession' in navigator) || !state.duration || !isFinite(state.duration)) return
    try {
      navigator.mediaSession.setPositionState({
        duration: state.duration,
        playbackRate: rateRef.current,
        position: currentTimeRef.current,
      })
    } catch {}
  }, [state.playing, state.duration])

  // ── Video event listeners ─────────────────────────────────────────────────
  useEffect(() => {
    const video = videoRef.current
    if (!video) return

    const onVideoPlay    = () => { playingRef.current = true;  setState(s => ({ ...s, playing: true,  buffering: false })); onPlayRef.current?.() }
    const onVideoPause   = () => { playingRef.current = false; setState(s => ({ ...s, playing: false })); onPauseRef.current?.() }
    const onVideoEnded   = () => { playingRef.current = false; setState(s => ({ ...s, playing: false })); onEndedRef.current?.() }
    const onVideoWaiting = () => setState(s => ({ ...s, buffering: true  }))
    const onVideoPlaying = () => setState(s => ({ ...s, buffering: false }))
    const onVideoCanPlay = () => setState(s => ({ ...s, ready: true, buffering: false }))
    const onVideoDuration= () => {
      // Live streams expose video.duration === Infinity.  'Infinity || 0' is
      // Infinity (truthy), so we must use isFinite() to clamp it to 0, which
      // signals to the rest of the UI that the stream has no known length.
      const safeDur = isFinite(video.duration) ? video.duration : 0
      durationRef.current = safeDur
      setState(s => ({ ...s, duration: safeDur }))
    }
    const onVideoVolume  = () => setState(s => ({ ...s, volume: video.volume, muted: video.muted }))
    const onVideoRate    = () => { rateRef.current = video.playbackRate; setState(s => ({ ...s, playbackRate: video.playbackRate })) }
    const onVideoError   = () => {
      // Bug #11: when hls.js is active it handles all media errors internally via
      // its own ERROR event (including recoverMediaError). If we also respond to
      // the raw <video> error event we show a fatal UI error for errors that
      // hls.js would have recovered from. Suppress when an HLSManager is attached.
      if (hlsManager.current) return
      const code = video.error?.code
      // MEDIA_ERR_SRC_NOT_SUPPORTED (4) — codec not supported
      // MEDIA_ERR_NETWORK (2) — network failure mid-stream
      // Only surface as fatal; MEDIA_ERR_ABORTED (1) is caused by src change and is harmless.
      if (code === 1) return  // aborted by src change — harmless
      const msg = code === 4
        ? 'Format or codec not supported'
        : (video.error?.message || 'Unable to play this stream')
      setState(s => ({ ...s, error: msg, buffering: false }))
      onErrorRef.current?.(msg)
    }
    const onVideoTime    = () => { currentTimeRef.current = video.currentTime }
    const onPipEnter     = () => setState(s => ({ ...s, pip: true  }))
    const onPipLeave     = () => setState(s => ({ ...s, pip: false }))

    const events: [string, EventListener][] = [
      ['play',                    onVideoPlay],
      ['pause',                   onVideoPause],
      ['ended',                   onVideoEnded],
      ['waiting',                 onVideoWaiting],
      ['playing',                 onVideoPlaying],
      ['canplay',                 onVideoCanPlay],
      ['durationchange',          onVideoDuration],
      ['volumechange',            onVideoVolume],
      ['ratechange',              onVideoRate],
      ['error',                   onVideoError],
      ['timeupdate',              onVideoTime],
      ['enterpictureinpicture',   onPipEnter],
      ['leavepictureinpicture',   onPipLeave],
    ]
    events.forEach(([e, h]) => video.addEventListener(e, h))
    return ()  => events.forEach(([e, h]) => video.removeEventListener(e, h))
  }, [videoRef])

  // ── Progress timer (paused when video is paused — saves CPU) ──────────────
  useEffect(() => {
    if (!state.playing) {
      if (progressTimerRef.current) {
        clearInterval(progressTimerRef.current)
        progressTimerRef.current = null
      }
      return
    }
    progressTimerRef.current = setInterval(() => {
      // B11 fix: use stable ref so the interval is not recreated on every parent render
      onProgressRef.current?.(currentTimeRef.current, durationRef.current)
      const buffered = playbackMgr.current.getBufferedAhead()
      setState(s => s.bufferedAhead !== buffered ? { ...s, bufferedAhead: buffered } : s)
    }, PROGRESS_INTERVAL)
    return () => {
      if (progressTimerRef.current) clearInterval(progressTimerRef.current)
    }
  }, [state.playing])  // B11 fix: removed onProgress dep — now read via stable ref

  // ── Fullscreen detection ──────────────────────────────────────────────────
  useEffect(() => {
    const h = () => setState(s => ({ ...s, fullscreen: !!document.fullscreenElement }))
    document.addEventListener('fullscreenchange', h)
    return () => document.removeEventListener('fullscreenchange', h)
  }, [])

  // ── Keyboard shortcuts ────────────────────────────────────────────────────
  useEffect(() => {
    const container = containerRef.current
    if (!container) return

    const onKey = (e: KeyboardEvent) => {
      const video = videoRef.current
      if (!video) return
      if (['INPUT','TEXTAREA','SELECT'].includes(document.activeElement?.tagName ?? '')) return

      switch (e.code) {
        case 'Space': case 'KeyK':
          // Bug #1: was bare video.play() — must go through playWithPolicy so
          // NotAllowedError / AbortError are handled and not thrown uncaught.
          e.preventDefault()
          if (video.paused) { void playWithPolicy(video) } else { video.pause() }
          break
        case 'ArrowLeft':
          e.preventDefault(); video.currentTime = Math.max(0, video.currentTime - 10); break
        case 'ArrowRight':
          // Bug #12: video.duration is NaN before loadedmetadata → Math.min(NaN,…)=NaN
          e.preventDefault()
          if (isFinite(video.duration)) {
            video.currentTime = Math.min(video.duration, video.currentTime + 10)
          }
          break
        case 'ArrowUp':
          e.preventDefault(); video.volume = Math.min(1, video.volume + 0.1); break
        case 'ArrowDown':
          e.preventDefault(); video.volume = Math.max(0, video.volume - 0.1); break
        case 'KeyM':
          video.muted = !video.muted; break
        case 'KeyF':
          e.preventDefault()
          document.fullscreenElement
            ? document.exitFullscreen()
            : container.requestFullscreen()
          break
        case 'Comma': {
          e.preventDefault()
          const idx = SPEEDS.findIndex(r => r >= video.playbackRate)
          const prev = idx > 0 ? SPEEDS[idx - 1] : SPEEDS[0]
          video.playbackRate = prev; rateRef.current = prev
          setState(s => ({ ...s, playbackRate: prev })); break
        }
        case 'Period': {
          e.preventDefault()
          let li = 0
          for (let i = 0; i < SPEEDS.length; i++) { if (SPEEDS[i] <= video.playbackRate) li = i }
          const next = SPEEDS[Math.min(SPEEDS.length - 1, li + 1)]
          video.playbackRate = next; rateRef.current = next
          setState(s => ({ ...s, playbackRate: next })); break
        }
        case 'KeyI':
          e.preventDefault()
          document.pictureInPictureElement
            ? document.exitPictureInPicture().catch(() => {})
            : video.requestPictureInPicture().catch(() => {})
          break
        default:
          if (e.code.startsWith('Digit') && video.duration > 0 && isFinite(video.duration)) {
            const digit = parseInt(e.code.slice(-1), 10)
            if (!isNaN(digit)) {
              e.preventDefault()
              video.currentTime = (digit / 10) * video.duration
            }
          }
      }
    }

    container.addEventListener('keydown', onKey)
    return () => container.removeEventListener('keydown', onKey)
  }, [videoRef, containerRef])

  // ── Actions ───────────────────────────────────────────────────────────────
  const togglePlay = useCallback(() => {
    // Bug #1: bare v.play() throws uncaught AbortError / NotAllowedError.
    // Route through playWithPolicy so all error cases are handled.
    const v = videoRef.current; if (!v) return
    if (v.paused) { void playWithPolicy(v) } else { v.pause() }
  }, [videoRef, playWithPolicy])

  const seek = useCallback((time: number) => {
    playbackMgr.current.seek(time)
  }, [])

  const setVolume = useCallback((level: number) => {
    playbackMgr.current.setVolume(level)
  }, [])

  const toggleMute = useCallback(() => {
    const v = videoRef.current; if (!v) return
    v.muted = !v.muted
    // Dismiss autoplay-muted state when user explicitly unmutes
    if (!v.muted) setState(s => ({ ...s, mutedForAutoplay: false }))
  }, [videoRef])

  const setQuality = useCallback((levelId: number) => {
    hlsManager.current?.setLevel(levelId)
    setState(s => ({ ...s, currentQuality: levelId }))
    const level = state.qualityLevels.find(l => l.id === levelId)
    if (level) onQualityChange?.(level)
  }, [state.qualityLevels, onQualityChange])

  const setAudioTrack = useCallback((trackId: number) => {
    hlsManager.current?.setAudioTrack(trackId)
    setState(s => ({ ...s, currentAudio: trackId }))
    const track = state.audioTracks.find(t => t.id === trackId)
    if (track) onAudioChange?.(track)
  }, [state.audioTracks, onAudioChange])

  const setSubtitleTrack = useCallback((trackId: number) => {
    hlsManager.current?.setSubtitleTrack(trackId)
    setState(s => ({ ...s, currentSubtitle: trackId }))
    const track = state.subtitleTracks.find(t => t.id === trackId)
    onSubtitleChange?.(track ?? null)
  }, [state.subtitleTracks, onSubtitleChange])

  const setPlaybackRate = useCallback((rate: number) => {
    const v = videoRef.current; if (!v) return
    v.playbackRate = rate; rateRef.current = rate
    setState(s => ({ ...s, playbackRate: rate }))
  }, [videoRef])

  const togglePip = useCallback(async () => {
    const v = videoRef.current; if (!v) return
    try {
      document.pictureInPictureElement
        ? await document.exitPictureInPicture()
        : await v.requestPictureInPicture()
    } catch (err) { console.warn('[useVideoPlayer] PiP:', err) }
  }, [videoRef])

  const reload = useCallback(() => {
    setState(s => ({ ...s, error: null }))
    fallbackIndex.current = 0
    loadSource(source)
  }, [loadSource, source])

  const toggleFullscreen = useCallback(async () => {
    const c = containerRef.current; if (!c) return
    document.fullscreenElement
      ? await document.exitFullscreen()
      : await playbackMgr.current.enterFullscreen(c)
  }, [containerRef])

  // ── Imperative ref ────────────────────────────────────────────────────────
  useImperativeHandle(handleRef, () => ({
    play:    () => playbackMgr.current.play(),
    pause:   () => playbackMgr.current.pause(),
    seek,
    setVolume,
    enterFullscreen: () => containerRef.current
      ? playbackMgr.current.enterFullscreen(containerRef.current)
      : Promise.resolve(),
    exitFullscreen:  () => playbackMgr.current.exitFullscreen(),
  }), [seek, setVolume, containerRef])

  return {
    state,
    currentTimeRef,   // exported for useResume
    actions: {
      togglePlay, seek, setVolume, toggleMute,
      setQuality, setAudioTrack, setSubtitleTrack,
      setPlaybackRate, togglePip, toggleFullscreen, reload,
    },
  }
}
