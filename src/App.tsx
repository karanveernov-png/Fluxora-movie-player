/**
 * App.tsx — Fluxora v3.1
 * Adds: auth-header proxy, RTMP/RTSP relay, localhost whitelist toggle.
 */

import { useState, useCallback, useRef, useEffect } from 'react'
import { MoviePlayer }    from './components/MoviePlayer'
import { MediaInfoPanel } from './components/MediaInfoPanel'
import { usePlayStrategy } from './hooks/usePlayStrategy'
import {
  fetchHealth,
  createProxySession,
  relayRtmp,
  pollHlsReady,
  updateSettings,
} from './api/mediaApi'
import type {
  PlayerSource, QualityLevel, AudioTrack, SubtitleTrack,
  BackendHealth, RecentEntry, VideoPlayerState,
} from './types'
import './App.css'

// ── Persistence ───────────────────────────────────────────────────────────────
const RECENT_KEY  = 'fluxora-recent-v1'
const RECENT_MAX  = 8
const PRIVATE_KEY = 'fluxora-allow-private'

function loadRecent(): RecentEntry[] {
  try { return JSON.parse(localStorage.getItem(RECENT_KEY) ?? '[]') } catch { return [] }
}
function saveRecent(e: RecentEntry[]): void {
  try { localStorage.setItem(RECENT_KEY, JSON.stringify(e.slice(0, RECENT_MAX))) } catch {}
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function detectType(url: string): PlayerSource['type'] {
  const lo = url.toLowerCase().split('?')[0]
  if (lo.endsWith('.m3u8') || lo.includes('/hls/') || lo.includes('/live/')) return 'hls'
  if (lo.endsWith('.mpd')) return 'dash'
  return 'mp4'
}

function isRtmpUrl(url: string): boolean {
  return /^rtmps?:\/\//i.test(url) || /^rtsps?:\/\//i.test(url)
}

function shortLabel(src: PlayerSource, fileName: string | null): string {
  if (src.type === 'local') return fileName ?? 'Local file'
  try {
    const u = new URL(src.url)
    return u.pathname.split('/').filter(Boolean).at(-1) ?? u.hostname
  } catch { return src.url.length > 55 ? src.url.slice(0, 52) + '…' : src.url }
}

function resumeKey(url: string): string {
  let h = 0x811c9dc5
  for (let i = 0; i < url.length; i++) { h ^= url.charCodeAt(i); h = (h * 0x01000193) >>> 0 }
  return `fluxora_resume_${h.toString(16)}`
}
function getSavedResume(url: string): number | null {
  try { const v = parseFloat(localStorage.getItem(resumeKey(url)) ?? ''); return isFinite(v) && v > 5 ? v : null }
  catch { return null }
}
function fmtTime(s: number): string {
  const m = Math.floor(s / 60), sec = Math.floor(s % 60).toString().padStart(2, '0')
  return `${m}:${sec}`
}

/** Parse "Key: Value" lines (one per line) into a headers dict. */
function parseHeaderLines(raw: string): Record<string, string> {
  const result: Record<string, string> = {}
  for (const line of raw.split('\n')) {
    const colon = line.indexOf(':')
    if (colon < 1) continue
    const key = line.slice(0, colon).trim()
    const val = line.slice(colon + 1).trim()
    if (key && val) result[key] = val
  }
  return result
}

// ── SVG Icons ─────────────────────────────────────────────────────────────────
const IcArrow = () => (
  <svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor">
    <path d="M20 11H7.83l5.59-5.59L12 4l-8 8 8 8 1.41-1.41L7.83 13H20v-2z"/>
  </svg>
)
const IcInfo = () => (
  <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor">
    <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-6h2v6zm0-8h-2V7h2v2z"/>
  </svg>
)
const IcLink = () => (
  <svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor">
    <path d="M3.9 12c0-1.71 1.39-3.1 3.1-3.1h4V7H7c-2.76 0-5 2.24-5 5s2.24 5 5 5h4v-1.9H7c-1.71 0-3.1-1.39-3.1-3.1zM8 13h8v-2H8v2zm9-6h-4v1.9h4c1.71 0 3.1 1.39 3.1 3.1s-1.39 3.1-3.1 3.1h-4V17h4c2.76 0 5-2.24 5-5s-2.24-5-5-5z"/>
  </svg>
)
const IcFolder = () => (
  <svg viewBox="0 0 24 24" fill="currentColor" className="av-tab-icon">
    <path d="M20 6h-8l-2-2H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V8c0-1.1-.9-2-2-2z"/>
  </svg>
)
const IcWifi = () => (
  <svg viewBox="0 0 24 24" fill="currentColor" className="av-tab-icon">
    <path d="M1 9l2 2c4.97-4.97 13.03-4.97 18 0l2-2C16.93 2.93 7.08 2.93 1 9zm8 8 3 3 3-3c-1.65-1.66-4.34-1.66-6 0zm-4-4 2 2c2.76-2.76 7.24-2.76 10 0l2-2C15.14 9.14 8.87 9.14 5 13z"/>
  </svg>
)
const IcUpload = () => (
  <svg viewBox="0 0 24 24" width="22" height="22" fill="currentColor">
    <path d="M19.35 10.04A7.49 7.49 0 0 0 12 4C9.11 4 6.6 5.64 5.35 8.04A5.994 5.994 0 0 0 0 14c0 3.31 2.69 6 6 6h13c2.76 0 5-2.24 5-5 0-2.64-2.05-4.78-4.65-4.96zM14 13v4h-4v-4H7l5-5 5 5h-3z"/>
  </svg>
)
const IcPlay = () => (
  <svg viewBox="0 0 24 24" width="12" height="12" fill="currentColor">
    <path d="M8 5v14l11-7z"/>
  </svg>
)
const IcClock = () => (
  <svg viewBox="0 0 24 24" width="11" height="11" fill="currentColor">
    <path d="M11.99 2C6.47 2 2 6.48 2 12s4.47 10 9.99 10C17.52 22 22 17.52 22 12S17.52 2 11.99 2zM12 20c-4.42 0-8-3.58-8-8s3.58-8 8-8 8 3.58 8 8-3.58 8-8 8zm.5-13H11v6l5.25 3.15.75-1.23-4.5-2.67V7z"/>
  </svg>
)
const IcChevronDown = () => (
  <svg viewBox="0 0 24 24" width="13" height="13" fill="currentColor">
    <path d="M16.59 8.59 12 13.17 7.41 8.59 6 10l6 6 6-6z"/>
  </svg>
)
const IcKey = () => (
  <svg viewBox="0 0 24 24" width="13" height="13" fill="currentColor">
    <path d="M12.65 10A5.99 5.99 0 0 0 7 6c-3.31 0-6 2.69-6 6s2.69 6 6 6a5.99 5.99 0 0 0 5.65-4H17v4h4v-4h2v-4H12.65zM7 14c-1.1 0-2-.9-2-2s.9-2 2-2 2 .9 2 2-.9 2-2 2z"/>
  </svg>
)
const IcRtmp = () => (
  <svg viewBox="0 0 24 24" width="13" height="13" fill="currentColor">
    <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 14.5v-9l6 4.5-6 4.5z"/>
  </svg>
)

type Screen = 'welcome' | 'player'
type Tab    = 'local' | 'stream'

export default function App() {
  const [screen, setScreen]         = useState<Screen>('welcome')
  const [rawSource, setRawSource]   = useState<PlayerSource | null>(null)
  const [fileName, setFileName]     = useState<string | null>(null)
  const [tab, setTab]               = useState<Tab>('local')
  const [customUrl, setCustomUrl]   = useState('')
  const [isDragOver, setIsDragOver] = useState(false)
  const [showInfo, setShowInfo]     = useState(false)
  const [recent, setRecent]         = useState<RecentEntry[]>(loadRecent)
  const [health, setHealth]         = useState<BackendHealth | null>(null)
  const [playerState, setPlayerState] = useState<VideoPlayerState | null>(null)

  // Advanced stream options
  const [showAdvanced, setShowAdvanced]   = useState(false)
  const [headerText, setHeaderText]       = useState('')   // raw "Key: Value\nKey: Value" text
  const [allowPrivateIps, setAllowPrivateIps] = useState(() => {
    // Bug #15: localStorage.getItem can throw in private-browsing or when
    // storage is blocked by the browser policy — wrapping in try/catch prevents
    // a white-screen crash on first render.
    try { return localStorage.getItem(PRIVATE_KEY) === 'true' } catch { return false }
  })
  const [connectingMsg, setConnectingMsg] = useState<string | null>(null)
  const [rtmpError, setRtmpError]         = useState<string | null>(null)

  const blobRef    = useRef<string | null>(null)
  const fileRef    = useRef<HTMLInputElement>(null)

  // ── Proxy session ref — for Bug 2 (CDN token expiry) ──────────────────────
  // Stores the original CDN URL and auth headers used to create the proxy
  // session.  When the CDN rotates its signed token and the manifest returns
  // 403, onTokenExpired (below) reads this ref to know which URL and headers
  // to re-fetch a fresh signed URL for.  We use a ref (not state) because it
  // must be readable inside an async callback without causing re-renders.
  const proxySessionRef = useRef<{
    token: string
    originalUrl: string
    headers: Record<string, string>
  } | null>(null)

  /**
   * Prevents an infinite retry loop when the auto-proxy attempt itself fails.
   * Reset to false whenever the user starts a new stream so the next stream
   * gets its own fresh CORS-retry chance.
   */
  const corsAutoRetryRef = useRef(false)

  const { resolvedSource, decision, state: strat } = usePlayStrategy(rawSource)

  useEffect(() => { fetchHealth().then(setHealth).catch(() => {}) }, [])
  useEffect(() => () => { if (blobRef.current) URL.revokeObjectURL(blobRef.current) }, [])

  // Sync private-IP setting to backend on mount
  useEffect(() => {
    if (allowPrivateIps) {
      updateSettings({ allow_private_ips: true }).catch(() => {})
    }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const openFile = useCallback((file: File) => {
    if (blobRef.current) URL.revokeObjectURL(blobRef.current)
    const blob = URL.createObjectURL(file)
    blobRef.current = blob
    setFileName(file.name)
    setRawSource({ type: 'local', url: blob })
    setShowInfo(false)
    setConnectingMsg(null)
    setScreen('player')
  }, [])

  const play = useCallback(async (urlIn?: string, typeIn?: PlayerSource['type']) => {
    const url = urlIn ?? customUrl.trim()
    if (!url) return

    // Each new play attempt gets a fresh CORS-retry opportunity.
    corsAutoRetryRef.current = false

    const parsedHeaders = parseHeaderLines(headerText)
    const hasHeaders    = Object.keys(parsedHeaders).length > 0

    // ── RTMP / RTSP relay ──────────────────────────────────────────────────
    if (isRtmpUrl(url)) {
      setFileName(null)
      setShowInfo(false)
      setConnectingMsg('Connecting to stream…')
      setRtmpError(null)
      setScreen('player')
      try {
        const relay = await relayRtmp(url)
        setConnectingMsg('Waiting for first segments…')
        await pollHlsReady(relay.key)
        setConnectingMsg(null)
        // Try regardless of poll result — segments may still be starting
        setRawSource({ type: 'hls', url: relay.play_url })
      } catch (err) {
        // Bug #9: the old code set connectingMsg to null and left rawSource null,
        // showing a completely blank player screen with no indication of what went
        // wrong.  Fix: surface the error message in the player overlay.
        setConnectingMsg(null)
        const msg = err instanceof Error ? err.message : 'RTMP relay failed — check the stream URL and backend connection.'
        setRtmpError(msg)
        console.error('[play] RTMP relay failed:', err)
      }
      return
    }

    // ── Auth-header proxy ──────────────────────────────────────────────────
    if (hasHeaders) {
      setFileName(null)
      setShowInfo(false)
      setConnectingMsg('Setting up auth proxy…')
      setScreen('player')
      try {
        const session = await createProxySession(url, parsedHeaders)
        setConnectingMsg(null)
        // Store session details so onTokenExpired can refresh the token later
        proxySessionRef.current = {
          token: session.token,
          originalUrl: url,
          headers: parsedHeaders,
        }
        setRawSource({ type: 'hls', url: session.proxy_url })
        setRecent(prev => {
          // Bug #10: the old code saved `url` (the original URL without auth
          // headers) in recents.  Replaying from recents would bypass the proxy
          // and fail with 401/403.  Fix: save the proxy_url so replaying picks
          // up the already-established proxy session.  Label still shows the
          // original URL so the user knows what stream they're watching.
          const e: RecentEntry = {
            url:          session.proxy_url,
            label:        url,
            type:         'hls',
            lastPlayedAt: Date.now(),
          }
          const next = [e, ...prev.filter(r => r.url !== session.proxy_url && r.url !== url)].slice(0, RECENT_MAX)
          saveRecent(next)
          return next
        })
      } catch (err) {
        setConnectingMsg(null)
        // Fall through to direct play
        console.warn('[play] proxy session failed, trying direct:', err)
        const type = typeIn ?? detectType(url)
        setRawSource({ type, url })
      }
      return
    }

    // ── Direct / backend pipeline ──────────────────────────────────────────
    const type = typeIn ?? detectType(url)
    setRawSource({ type, url })
    setFileName(null)
    setShowInfo(false)
    setConnectingMsg(null)
    setScreen('player')
    setRecent(prev => {
      const e: RecentEntry = { url, label: url, type, lastPlayedAt: Date.now() }
      const next = [e, ...prev.filter(r => r.url !== url)].slice(0, RECENT_MAX)
      saveRecent(next)
      return next
    })
  }, [customUrl, headerText])

  const playRecent = useCallback((e: RecentEntry) => {
    // RTMP entries in recent: re-relay (session would have expired anyway)
    if (isRtmpUrl(e.url)) {
      void play(e.url)
      return
    }
    corsAutoRetryRef.current = false
    setRawSource({ type: e.type, url: e.url })
    setFileName(null)
    setShowInfo(false)
    setConnectingMsg(null)
    setScreen('player')
  }, [play])

  const delRecent = useCallback((url: string, ev: React.MouseEvent) => {
    ev.stopPropagation()
    setRecent(prev => { const n = prev.filter(r => r.url !== url); saveRecent(n); return n })
  }, [])

  const goBack = useCallback(() => {
    setScreen('welcome')
    setShowInfo(false)
    setRawSource(null)
    setConnectingMsg(null)
    setRtmpError(null)
  }, [])

  const togglePrivateIps = useCallback(async () => {
    const newVal = !allowPrivateIps
    setAllowPrivateIps(newVal)
    localStorage.setItem(PRIVATE_KEY, String(newVal))
    try {
      await updateSettings({ allow_private_ips: newVal })
    } catch { /* backend offline — setting stored locally, will sync on next start */ }
  }, [allowPrivateIps])

  const onDragOver  = (e: React.DragEvent) => { e.preventDefault(); setIsDragOver(true) }
  const onDragLeave = (e: React.DragEvent) => { if (!e.currentTarget.contains(e.relatedTarget as Node)) setIsDragOver(false) }
  const onDrop      = (e: React.DragEvent) => { e.preventDefault(); setIsDragOver(false); const f = e.dataTransfer.files[0]; if (f) openFile(f) }

  const onState    = useCallback((s: VideoPlayerState) => setPlayerState(s), [])
  const onQuality  = useCallback((_: QualityLevel) => {}, [])
  const onAudio    = useCallback((_: AudioTrack) => {}, [])
  const onSubtitle = useCallback((_: SubtitleTrack | null) => {}, [])
  const onProgress = useCallback((_p: number, _d: number) => {}, [])

  // ── PLAYER SCREEN ──────────────────────────────────────────────────────────
  if (screen === 'player') {
    const label = rawSource ? shortLabel(rawSource, fileName) : connectingMsg ? '…' : '…'
    const isProc = strat.phase === 'resolving' || strat.phase === 'polling'
    const isConnecting = connectingMsg !== null && strat.phase === 'idle' && !rawSource
    const procMsg = connectingMsg ?? (strat.phase === 'polling' ? 'Processing media…' : 'Analysing source…')

    return (
      <div className="av-player-view">
        <header className="av-bar">
          <button className="av-bar-back" onClick={goBack} aria-label="Back" type="button">
            <IcArrow />
          </button>
          <span className="av-bar-logo">Fluxora</span>
          <div className="av-bar-divider" />
          <span className="av-bar-title" title={rawSource?.url ?? ''}>{label}</span>
          {decision?.strategy && decision.strategy !== 'direct' && (
            <span className="av-bar-pill">{decision.strategy.replace('_', '+').toUpperCase()}</span>
          )}
          <button
            className={`av-bar-info-btn${showInfo ? ' av-bar-info-btn--active' : ''}`}
            onClick={() => setShowInfo(s => !s)}
            aria-label="Media info"
            type="button"
          >
            <IcInfo />
          </button>
        </header>

        <div className="av-player-body">
          <div className="av-player-stage">
            {(isProc || isConnecting) && (
              <div className="av-overlay-state processing">
                <div className="av-spinner" />
                <p className="av-overlay-msg">{procMsg}</p>
                {strat.phase === 'polling' && (
                  <p className="av-overlay-sub">{decision?.reason ?? 'Converting for browser playback…'}</p>
                )}
              </div>
            )}
            {/* Bug #9: RTMP relay error — surface it in the overlay */}
            {rtmpError && !rawSource && (
              <div className="av-overlay-state error">
                <p className="av-overlay-err">⚠ {rtmpError}</p>
                <button
                  className="av-overlay-retry"
                  type="button"
                  onClick={goBack}
                >
                  ← Go back
                </button>
              </div>
            )}
            {strat.phase === 'error' && (
              <div className="av-overlay-state error">
                <p className="av-overlay-err">⚠ {strat.message}</p>
              </div>
            )}
            {resolvedSource && (
              <MoviePlayer
                source={resolvedSource}
                autoPlay
                onQualityChange={onQuality}
                onAudioChange={onAudio}
                onSubtitleChange={onSubtitle}
                onProgress={onProgress}
                onStateChange={onState}
                onError={async (message) => {
                  /**
                   * Auto-proxy on CORS block.
                   *
                   * HLSManager emits this specific message (Bug #7 in HLSManager.ts)
                   * when navigator.onLine is true but the manifest fetch returns no
                   * response (status 0) — the tell-tale sign of a CORS rejection.
                   *
                   * When that happens we silently create a backend proxy session for
                   * the original URL (with no auth headers — the CDN is public but
                   * CORS-restricted) and swap rawSource to the proxy URL.
                   * usePlayStrategy will treat the new source as a plain HLS source
                   * and load it directly; the backend proxies every manifest and
                   * segment request server-side, so hls.js never touches the CDN
                   * directly again.
                   *
                   * corsAutoRetryRef prevents an infinite retry if the proxy itself
                   * fails or the backend is offline.
                   */
                  if (
                    message.includes('CORS') &&
                    !corsAutoRetryRef.current &&
                    rawSource?.type === 'hls' &&
                    rawSource?.url
                  ) {
                    corsAutoRetryRef.current = true
                    try {
                      const session = await createProxySession(rawSource.url, {})
                      // Store session so the token-expiry handler can refresh it later
                      proxySessionRef.current = {
                        token:       session.token,
                        originalUrl: rawSource.url,
                        headers:     {},
                      }
                      setRawSource({ type: 'hls', url: session.proxy_url })
                    } catch (err) {
                      console.error('[App] CORS auto-proxy failed:', err)
                    }
                  }
                }}
                onTokenExpired={async (token) => {
                  // ── CDN token-expiry: return the original URL + headers ──────
                  // HLSManager detected a 403 on the proxy manifest, meaning the
                  // CDN's signed URL has rotated.
                  //
                  // The useVideoPlayer wrapper (onTokenExpired in useVideoPlayer.ts)
                  // will call refreshProxySession() with the token we receive here
                  // (the proxy token, which never changes), then HLSManager calls
                  // it again internally — the backend silently re-fetches using the
                  // original URL so the CDN can re-issue a fresh signed response.
                  //
                  // Bug fixes vs. previous implementation:
                  //  1. Do NOT call createProxySession() — that created a brand-new
                  //     session, leaking a slot and returning a different token.
                  //  2. Do NOT update proxySessionRef.current.token — HLSManager's
                  //     this.proxyToken is set at attach() time and never changes,
                  //     so updating the ref to a different token caused every
                  //     subsequent 403 to fail the `session.token !== token` guard.
                  const session = proxySessionRef.current
                  if (!session || session.token !== token) return null
                  return { url: session.originalUrl, headers: session.headers }
                }}
              />
            )}
          </div>
          {showInfo && playerState && (
            <MediaInfoPanel
              decision={decision}
              playerState={playerState}
              sourceLabel={rawSource?.url ?? ''}
              onClose={() => setShowInfo(false)}
            />
          )}
        </div>
      </div>
    )
  }

  // ── WELCOME SCREEN ─────────────────────────────────────────────────────────
  const parsedHeaders  = parseHeaderLines(headerText)
  const headerCount    = Object.keys(parsedHeaders).length
  const urlIsRtmp      = isRtmpUrl(customUrl)

  return (
    <div className="av-welcome-view">
      {/* Nav */}
      <nav className="av-nav">
        <div className="av-nav-brand">
          <div className="av-logo-mark" aria-hidden>
            <svg className="av-logo-inner" viewBox="0 0 20 20" fill="none">
              <rect x="3"    y="10" width="3" height="7"  rx="1.4" fill="currentColor" />
              <rect x="8.5"  y="5"  width="3" height="12" rx="1.4" fill="currentColor" />
              <rect x="14"   y="8"  width="3" height="9"  rx="1.4" fill="currentColor" />
            </svg>
          </div>
          <div className="av-brand-text">
            <span className="av-brand-name">Fluxora</span>
            <span className="av-brand-tagline">Any Stream. Zero Compromise.</span>
          </div>
        </div>

        <div className="av-nav-end">
          {health && (
            <div className="av-health">
              <span className={`av-health-dot av-health-dot--${health.status === 'ok' ? 'ok' : 'fail'}`} />
              <span className="av-health-text">Backend {health.status === 'ok' ? 'online' : 'offline'}</span>
            </div>
          )}
        </div>
      </nav>

      {/* Page content — constrained + two-column on desktop */}
      <div className="av-page-wrap">
      <div className="av-desktop-grid">

      {/* LEFT COLUMN: hero + stats + presets */}
      <div className="av-left-col">
      {/* Hero */}
      <section className="av-hero">
        <div className="av-hero-eyebrow">
          <span className="av-eyebrow-dot" />
          <span className="av-eyebrow-line" />
          <span>HLS · DASH · MP4 · RTMP · Local files</span>
        </div>
        <h1 className="av-hero-title">
          Every stream,<br />
          <span className="av-hero-title-line2">perfectly played</span>
        </h1>
        <p className="av-hero-body">
          Drop a file or paste a URL — Fluxora handles HLS, DASH, WebM, MP4, RTMP, and local video
          without plugins or friction. One player. Every stream.
        </p>

        {/* Stats strip */}
        <div className="av-stats">
          {[
            { num: '5', label: 'Stream formats' },
            { num: '0', label: 'Plugins needed' },
            { num: '∞', label: 'No file size limit' },
          ].map(s => (
            <div key={s.label} className="av-stat">
              <div className="av-stat-num">{s.num}</div>
              <div className="av-stat-label">{s.label}</div>
            </div>
          ))}
        </div>
      </section>
      </div>{/* /av-left-col */}

      {/* RIGHT COLUMN: input panel + recent list */}
      <main className="av-welcome-main">

        {/* Input panel */}
        <div className="av-input-panel">
          <div className="av-tabs" role="tablist">
            <button
              role="tab" aria-selected={tab === 'local'} type="button"
              className={`av-tab${tab === 'local' ? ' av-tab--active' : ''}`}
              onClick={() => setTab('local')}
            >
              <IcFolder /> Local file
            </button>
            <button
              role="tab" aria-selected={tab === 'stream'} type="button"
              className={`av-tab${tab === 'stream' ? ' av-tab--active' : ''}`}
              onClick={() => setTab('stream')}
            >
              <IcWifi /> Stream URL
            </button>
          </div>

          <div className="av-tab-body">
            {tab === 'local' && (
              <div
                className={`av-drop-zone${isDragOver ? ' av-drop-zone--over' : ''}`}
                onDragOver={onDragOver} onDragLeave={onDragLeave} onDrop={onDrop}
                onClick={() => fileRef.current?.click()}
                role="button" tabIndex={0} aria-label="Drop a video file or click to browse"
                onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') fileRef.current?.click() }}
              >
                <input
                  ref={fileRef} type="file"
                  accept="video/*,.mp4,.webm,.ogg,.mov,.avi,.mkv,.m4v"
                  style={{ display: 'none' }}
                  onChange={e => { const f = e.target.files?.[0]; if (f) openFile(f); e.target.value = '' }}
                />
                <div className="av-drop-icon"><IcUpload /></div>
                <p className="av-drop-title">{isDragOver ? 'Release to play' : 'Drop your video here'}</p>
                <p className="av-drop-sub">or click to browse</p>
                <div className="av-format-pills">
                  {['MP4', 'WebM', 'MOV', 'MKV', 'AVI', 'OGG'].map(f => (
                    <span key={f} className="av-format-pill">{f}</span>
                  ))}
                  {health?.ffprobe_available && <span className="av-format-pill">+ more via FFmpeg</span>}
                </div>
              </div>
            )}

            {tab === 'stream' && (
              <div className="av-stream-panel">
                {/* URL input row */}
                <div className="av-url-field">
                  <span className="av-url-prefix">
                    {urlIsRtmp ? <IcRtmp /> : <IcLink />}
                  </span>
                  <input
                    className="av-url-input"
                    type="url" autoFocus spellCheck={false}
                    placeholder="https://…/stream.m3u8  or  manifest.mpd  or  rtmp://live.example.com/app/key"
                    value={customUrl}
                    onChange={e => setCustomUrl(e.target.value)}
                    onKeyDown={e => e.key === 'Enter' && void play()}
                    aria-label="Stream URL"
                  />
                  <button
                    className="av-play-btn" type="button"
                    onClick={() => void play()} disabled={!customUrl.trim()}
                  >
                    Play
                  </button>
                </div>

                {/* Hint line */}
                <p className="av-hint">
                  {urlIsRtmp
                    ? 'RTMP/RTSP stream — relayed to HLS via backend FFmpeg.'
                    : <>
                        HLS (.m3u8), DASH (.mpd), MP4, WebM, MKV, RTMP supported.
                        {health?.ffmpeg_available ? ' Incompatible formats auto-converted via FFmpeg.' : ''}
                      </>
                  }
                </p>

                {/* Advanced options toggle */}
                <div className="av-advanced-wrap">
                  <button
                    type="button"
                    className="av-advanced-toggle"
                    onClick={() => setShowAdvanced(v => !v)}
                    aria-expanded={showAdvanced}
                  >
                    <IcChevronDown />
                    Advanced options
                    {(headerCount > 0 || allowPrivateIps) && (
                      <span className="av-advanced-badge">
                        {[headerCount > 0 && `${headerCount} header${headerCount > 1 ? 's' : ''}`, allowPrivateIps && 'LAN']
                          .filter(Boolean).join(' · ')}
                      </span>
                    )}
                  </button>

                  {showAdvanced && (
                    <div className="av-advanced-body">

                      {/* Auth headers */}
                      <div className="av-adv-section">
                        <label className="av-adv-label">
                          <IcKey /> Auth headers
                          <span className="av-adv-hint"> — one per line, format: Key: Value</span>
                        </label>
                        <textarea
                          className="av-headers-textarea"
                          rows={3}
                          spellCheck={false}
                          placeholder={"Authorization: Bearer eyJhbGciOiJSUz...\nX-Auth-Token: abc123"}
                          value={headerText}
                          onChange={e => setHeaderText(e.target.value)}
                          aria-label="Auth headers"
                        />
                        {headerCount > 0 && (
                          <p className="av-adv-active">
                            {headerCount} header{headerCount > 1 ? 's' : ''} active — stream will be fetched server-side (HLS only).
                          </p>
                        )}
                      </div>

                      {/* Private IP toggle */}
                      <div className="av-adv-section av-adv-toggle-row">
                        <div>
                          <span className="av-adv-label">Allow private networks</span>
                          <span className="av-adv-hint">
                            {' '}— 192.168.x.x, 10.x.x.x, 127.x.x.x (Plex, Jellyfin, NAS)
                          </span>
                        </div>
                        <button
                          type="button"
                          className={`av-toggle${allowPrivateIps ? ' av-toggle--on' : ''}`}
                          onClick={() => void togglePrivateIps()}
                          aria-pressed={allowPrivateIps}
                          aria-label="Allow private networks"
                        >
                          <span className="av-toggle-knob" />
                        </button>
                      </div>

                    </div>
                  )}
                </div>
              </div>
            )}
          </div>
        </div>

        {/* Recently played */}
        {recent.length > 0 && (
          <div className="av-recent-wrap">
            <div className="av-section-header">
              <span className="av-section-title">Recently played</span>
              <div className="av-section-rule" />
            </div>
            <ul className="av-recent-list">
              {recent.map(e => {
                const resume = getSavedResume(e.url)
                return (
                  <li key={e.url} className="av-recent-row">
                    <button
                      className="av-recent-play-btn" type="button"
                      onClick={() => playRecent(e)} title={e.url}
                    >
                      <span className="av-recent-type-badge">
                        {isRtmpUrl(e.url) ? 'RTMP' : e.type.toUpperCase()}
                      </span>
                      <span className="av-recent-url-text">{e.url}</span>
                      {resume && (
                        <span className="av-recent-resume">
                          <IcClock /> {fmtTime(resume)}
                        </span>
                      )}
                      <IcPlay />
                    </button>
                    <button
                      className="av-recent-del" type="button"
                      onClick={ev => delRecent(e.url, ev)} aria-label="Remove"
                    >×</button>
                  </li>
                )
              })}
            </ul>
          </div>
        )}
      </main>

      </div>{/* /av-desktop-grid */}
      </div>{/* /av-page-wrap */}

      {/* Footer */}
      <footer className="av-footer">
        <div className="av-footer-left">
          <span className="av-footer-brand">Fluxora</span>
          <span className="av-footer-year">{new Date().getFullYear()}</span>
        </div>
        <span className="av-footer-formats">Any Stream. Zero Compromise.</span>
      </footer>
    </div>
  )
}
