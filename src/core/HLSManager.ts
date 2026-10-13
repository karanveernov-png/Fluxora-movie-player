/**
 * HLSManager
 * Owns the hls.js instance lifecycle.
 *
 * Low-internet / reliability improvements over original:
 * ────────────────────────────────────────────────────────
 * - startLevel: 0  →  always begin at the LOWEST quality tier; ABR upgrades
 *   after the first fragment lands. Zero rebuffering on cold start.
 * - Network Information API awareness: if the browser reports 2G/3G, we pin
 *   to the bottom quality and stay there until bandwidth proves otherwise.
 * - maxBufferLength: 60s  →  fill a full minute before playback can stall.
 * - Fragment retries: 8 attempts, exponential back-off up to 32 s per retry.
 * - Seek-forward stall recovery: when a fragment repeatedly fails we advance
 *   the playhead past it rather than hard-erroring.
 * - BUFFER_STALLED / nudge events are handled so hls.js self-recovers without
 *   surfacing an error to the UI.
 * - More conservative ABR factors (0.65 / 0.5) so bandwidth spikes don't cause
 *   premature quality upgrades that immediately rebuffer.
 * - Manifest + level-playlist retries brought in line with fragment retries.
 *
 * Bug fixes in this revision:
 * ────────────────────────────────────────────────────────
 * Bug 1 — "mastermaster" path duplication
 *   Root cause: the backend proxy's _rewrite_m3u8() was skipping URI= attribute
 *   values inside #EXT-X-MEDIA, #EXT-X-KEY, and #EXT-X-MAP tag lines. hls.js
 *   resolved those unproxied relative URIs against the proxy manifest URL,
 *   producing paths like "mastermaster_video10000k.m3u8". Fixed on the backend
 *   (proxy.py). HLSManager is unchanged for Bug 1 — the fix lives entirely in
 *   the rewriter.
 *
 * Bug 2 — Live stream 403 on CDN token expiry
 *   Root cause: CDNs like Akamai embed a short-lived signed token in the
 *   master.m3u8 URL query string. When the token expires the proxy session's
 *   stored URL is stale, so every manifest re-fetch returns 403.
 *   Fix: on a MANIFEST_LOAD_ERROR with status 403 HLSManager now calls the
 *   optional onTokenExpired callback. The application is expected to obtain a
 *   fresh signed URL from its auth backend and then call refreshProxySession()
 *   (which hits POST /api/media/proxy/session/{token}/refresh). After the
 *   refresh succeeds, HLSManager tears down and re-attaches hls.js against
 *   the SAME proxy URL (no source change) so playback resumes transparently.
 */

import Hls, { type ErrorData, type HlsConfig } from 'hls.js'
import type { QualityLevel, AudioTrack, SubtitleTrack } from '../types'

const BASE_API = import.meta.env.VITE_API_URL ?? 'http://localhost:8000/api'

const MAX_FATAL_RETRIES = 6
const BASE_RETRY_MS     = 500    // first retry delay
const MAX_RETRY_MS      = 32_000 // cap per-attempt wait

/** Max times we will try to refresh a CDN token before giving up. */
const MAX_TOKEN_REFRESHES = 3

function retryDelay(attempt: number): number {
  // Exponential back-off: 500, 1 000, 2 000, 4 000, 8 000, 16 000, …
  return Math.min(MAX_RETRY_MS, BASE_RETRY_MS * Math.pow(2, attempt))
}

/**
 * Detect CDN expiry-token parameters in a URL.
 *
 * Returns:
 *  'expired'   — an expiry timestamp was parsed and is in the past
 *  'maybe'     — expiry-related params exist but the timestamp cannot be parsed reliably
 *  'none'      — no expiry-related params found
 *
 * This avoids treating the mere presence of a param as proof of expiration.
 * Do NOT log the URL — it may contain tokens.
 */
type ExpiryResult = 'expired' | 'maybe' | 'none'

/** Parameter names whose values are Unix-epoch timestamps (seconds) */
const EPOCH_PARAMS  = ['expire', 'expires', 'exp', 'validuntil'] as const
/** Parameter names that indicate a signed/expiring URL but carry no parseable timestamp */
const SIGNED_PARAMS = ['x-amz-expires', 'x-goog-expires', 'token_expires'] as const
/** Short aliases that could be epoch timestamps but are ambiguous */
const AMBIG_PARAMS  = ['e'] as const

function urlExpiryStatus(url: string): ExpiryResult {
  let params: URLSearchParams
  try {
    params = new URL(url).searchParams
  } catch {
    return 'none'
  }

  // Build a lowercase-key → value map so matching is case-insensitive.
  // (URLSearchParams preserves the case of keys as sent; CDNs vary:
  //  AWS uses X-Amz-Expires, others use lowercase.)
  const lc = new Map<string, string>()
  for (const [k, v] of params.entries()) lc.set(k.toLowerCase(), v)

  // Try to parse a definitive epoch timestamp
  for (const key of EPOCH_PARAMS) {
    const raw = lc.get(key)   // keys in EPOCH_PARAMS are already lowercase
    if (raw !== undefined) {
      const ts = Number(raw)
      if (Number.isFinite(ts) && ts > 0) {
        // Unix-epoch seconds: values >1e9 are dates after 2001; reject tiny values
        const nowSec = Date.now() / 1000
        if (ts > 1_000_000_000) {
          return ts < nowSec ? 'expired' : 'maybe'
        }
        // Ambiguous small value — treat cautiously
        return 'maybe'
      }
      // Key present but value is not a number — still cautious
      return 'maybe'
    }
  }

  // Short single-char 'e' param — ambiguous, treat cautiously
  for (const key of AMBIG_PARAMS) {
    if (lc.has(key)) return 'maybe'
  }

  // Signed-URL markers with no parseable timestamp
  for (const key of SIGNED_PARAMS) {
    if (lc.has(key)) return 'maybe'
  }

  return 'none'
}

/**
 * Map an HTTP status code to a user-facing error string.
 *
 * 403 + confirmed-expired token → "Access denied — URL has expired"
 * 403 + maybe-expiry token      → "Access denied — URL may have expired"
 * 403 + no expiry token         → "Access denied"
 * 404                           → "Stream not found — URL may have expired or been removed"
 * 410                           → "Stream removed or expired"
 * 429                           → "Source rate limit reached; try again later"
 * 5xx                           → "Source server unavailable"
 * other                         → "Unable to play this stream"
 *
 * The url argument must NOT be logged by callers — it may contain tokens.
 */
function classifyHttpError(code: number, url = ''): string {
  switch (code) {
    case 401: return 'Authentication required'
    case 403: {
      const expiry = urlExpiryStatus(url)
      if (expiry === 'expired') return 'Access denied — URL has expired'
      if (expiry === 'maybe')   return 'Access denied — URL may have expired'
      return 'Access denied'
    }
    case 404: return 'Stream not found — URL may have expired or been removed'
    case 410: return 'Stream removed or expired'
    case 429: return 'Source rate limit reached; try again later'
    default:  return code >= 500 ? 'Source server unavailable' : 'Unable to play this stream'
  }
}

/** Detect approximate connection tier via Network Information API (where available) */
type ConnTier = 'slow' | 'medium' | 'fast' | 'unknown'
function connectionTier(): ConnTier {
  const c = (navigator as Navigator & { connection?: { effectiveType?: string } }).connection
  if (!c?.effectiveType) return 'unknown'
  if (c.effectiveType === 'slow-2g' || c.effectiveType === '2g') return 'slow'
  if (c.effectiveType === '3g') return 'medium'
  return 'fast'
}

function buildConfig(): Partial<HlsConfig> {
  const tier = connectionTier()

  // ABR factors: be more conservative on slow connections
  const bwFactor   = tier === 'slow'   ? 0.55 : tier === 'medium' ? 0.65 : 0.80
  const bwUpFactor = tier === 'slow'   ? 0.40 : tier === 'medium' ? 0.50 : 0.65

  // Starting EWMA estimate — very conservative so ABR doesn't overshoot
  const ewmaDefault = tier === 'slow'   ? 150_000
                    : tier === 'medium' ? 400_000
                    :                    800_000

  // Buffer targets
  const maxBuf = tier === 'slow' ? 90 : 60   // fill more on slow nets

  return {
    // ── Startup: always begin at lowest quality ──────────────────────────
    startLevel: 0,                  // index 0 = lowest bitrate in manifest
    abrEwmaDefaultEstimate: ewmaDefault,
    abrBandWidthFactor:   bwFactor,
    abrBandWidthUpFactor: bwUpFactor,

    // ── Buffer: fill aggressively so stalls are rare ─────────────────────
    maxBufferLength:    maxBuf,         // seconds to buffer ahead
    maxMaxBufferLength: 600,            // never cap below this on fast nets
    maxBufferSize:      60 * 1024 * 1024, // 60 MB hard cap
    backBufferLength:   30,             // keep 30 s back for seeks
    maxBufferHole:      0.5,            // auto-bridge gaps ≤ 0.5 s
    highBufferWatchdogPeriod: 2,        // check stall condition every 2 s

    // ── Stall nudging ────────────────────────────────────────────────────
    nudgeOffset:  0.1,
    nudgeMaxRetry: 8,

    // ── Fragment loading: reduced retries for 5xx to avoid storm ─────────
    // Bug fix (B18): the original config used 8 retries with 500ms base delay.
    // When the backend returns 502/503 on every segment (e.g. during the SSRF
    // loop caused by B13), hls.js retried 8 times per fragment at exponential
    // back-off — with multiple concurrent fragment requests, this produced
    // dozens of in-flight XHR requests all returning 502 simultaneously (the
    // "502 storm" visible in the network tab). Reducing retries to 3 with a
    // longer base delay (2s) means hls.js surfaces a fatal error faster, which
    // triggers HLSManager's own retry logic (scheduleRetry) with proper
    // back-off — a cleaner recovery path.
    fragLoadingTimeOut:         25_000,
    fragLoadingMaxRetry:        3,      // was 8; surface fatal faster on 5xx
    fragLoadingRetryDelay:      2_000,  // was 500ms; longer gap between retries
    fragLoadingMaxRetryTimeout: MAX_RETRY_MS,

    // ── Manifest loading ─────────────────────────────────────────────────
    manifestLoadingTimeOut:        15_000,
    manifestLoadingMaxRetry:       3,   // was 6; faster fatal on persistent failure
    manifestLoadingRetryDelay:     1_000,
    manifestLoadingMaxRetryTimeout: 16_000,

    // ── Level playlist loading ───────────────────────────────────────────
    levelLoadingTimeOut:        15_000,
    levelLoadingMaxRetry:       3,      // was 6
    levelLoadingRetryDelay:     1_000,
    levelLoadingMaxRetryTimeout: 16_000,

    // ── Live streams ─────────────────────────────────────────────────────
    liveSyncDurationCount:        3,
    liveMaxLatencyDurationCount: 10,
    lowLatencyMode:              false,  // disabled — better for slow nets

    // ── Fragment lookahead tolerance ─────────────────────────────────────
    maxFragLookUpTolerance: 0.5,

    // ── Performance ──────────────────────────────────────────────────────
    enableWorker: true,  // parse TS/AAC in a worker → main thread stays free
  }
}

/**
 * Refresh a proxy session's CDN URL on the backend.
 *
 * Called when the root manifest returns 403, indicating the CDN signed-URL
 * token has expired.  The caller must supply a freshly-signed CDN URL (and
 * any updated auth headers) obtained from its own auth backend.
 *
 * On success the backend silently updates the stored URL; the player's
 * proxy_url does NOT change — hls.js can be re-initialised against the same
 * proxy endpoint without any visible source change.
 *
 * Returns true if the session was refreshed, false if it had already expired
 * (in which case the caller should create a new session and re-attach).
 */
export async function refreshProxySession(
  token: string,
  freshCdnUrl: string,
  headers: Record<string, string> = {},
): Promise<boolean> {
  try {
    const resp = await fetch(`${BASE_API}/media/proxy/session/${token}/refresh`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: freshCdnUrl, headers }),
    })
    // 200 → refreshed; 410 → session expired (caller must create a new one)
    return resp.ok
  } catch {
    // Network error while refreshing — treat as failure; let the retry loop
    // attempt recovery via normal scheduleRetry path.
    return false
  }
}

export type HLSManagerEvents = {
  onLevelsLoaded:          (levels: QualityLevel[]) => void
  onAudioTracksLoaded:     (tracks: AudioTrack[])   => void
  onSubtitleTracksLoaded:  (tracks: SubtitleTrack[]) => void
  onError:                 (message: string, fatal: boolean) => void
  onManifestParsed:        () => void
  onLevelSwitched?:        (levelId: number) => void
  onBandwidthEstimate?:    (bps: number) => void

  /**
   * Called when the proxy manifest returns 403 and token expiry is detected.
   *
   * The application must:
   *   1. Obtain a fresh signed CDN URL from its auth backend.
   *   2. Call refreshProxySession(token, freshUrl, updatedHeaders).
   *   3. Resolve the returned Promise with { url: freshUrl, headers } on
   *      success, or null if the refresh cannot be completed (e.g. the user
   *      is logged out).
   *
   * When this callback is not provided, a 403 on the manifest is treated as
   * a permanent auth failure and surfaced directly to the user via onError.
   *
   * @param token  The proxy session token (NOT the CDN URL — safe to log).
   * @returns      Promise resolving to a fresh { url, headers } or null.
   */
  onTokenExpired?: (token: string) => Promise<{ url: string; headers: Record<string, string> } | null>
}

export class HLSManager {
  private hls:         Hls | null = null
  private retryCount = 0
  private retryTimer: ReturnType<typeof setTimeout> | null = null
  private currentSrc  = ''
  private events:     HLSManagerEvents
  private destroyed   = false

  /**
   * Native HLS (Safari) listener tracking.
   * attachNativeHLSEvents() registers a loadedmetadata listener whose
   * reference must be kept so destroy() can remove it before it fires.
   * Without this, calling destroy() before loadedmetadata fires leaves
   * the listener alive; it eventually calls onManifestParsed() on a
   * destroyed manager, causing state updates after unmount.
   */
  private nativeHLSVideo:     HTMLVideoElement | null = null
  private nativeMetaListener: (() => void) | null     = null

  /** Fragile segments: track consecutive failures on the same fragment */
  private lastFailedFrag: string | null = null
  private fragFailCount   = 0
  /** Set when a manifest timeout fires so scheduleRetry can show the right message on exhaustion */
  private lastTimeoutSeen = false

  /**
   * Proxy token extracted from currentSrc when the source is a proxy URL.
   * Used by the 403 token-refresh path.
   * Null when playing a non-proxy (direct CDN) URL.
   */
  private proxyToken: string | null = null
  /** How many token refreshes have been attempted in this session. */
  private tokenRefreshCount = 0

  constructor(events: HLSManagerEvents) {
    this.events = events
  }

  static isHLSSupported(): boolean        { return Hls.isSupported() }
  static hasNativeHLS(v: HTMLVideoElement) { return v.canPlayType('application/vnd.apple.mpegurl') !== '' }

  /**
   * Extract the proxy session token from a proxy URL, if present.
   *
   * Proxy URLs follow the pattern:
   *   http(s)://<host>/api/media/proxy/<32-char-hex-token>
   *
   * Returns null for non-proxy or unrecognised URLs.
   */
  private static _extractProxyToken(url: string): string | null {
    try {
      const path = new URL(url).pathname
      const m = path.match(/\/api\/media\/proxy\/([0-9a-f]{32})$/)
      return m ? m[1] : null
    } catch {
      return null
    }
  }

  attach(video: HTMLVideoElement, url: string): void {
    this.destroy()
    this.destroyed    = false
    this.currentSrc   = url
    this.retryCount      = 0
    this.fragFailCount   = 0
    this.lastFailedFrag  = null
    this.lastTimeoutSeen = false
    this.tokenRefreshCount = 0
    this.proxyToken = HLSManager._extractProxyToken(url)

    if (!HLSManager.isHLSSupported() && HLSManager.hasNativeHLS(video)) {
      video.src = url
      this.attachNativeHLSEvents(video)
      return
    }
    if (!HLSManager.isHLSSupported()) {
      this.events.onError('HLS is not supported in this browser', true)
      return
    }
    this.initHLS(video, url)
  }

  /** Safari native HLS — expose minimal quality/audio/subtitle info */
  private attachNativeHLSEvents(video: HTMLVideoElement): void {
    // Overcome preload="none": with that attribute the browser makes no
    // network request until play() is called, so loadedmetadata never fires
    // and onManifestParsed (which calls playWithPolicy) is never reached —
    // a deadlock.  video.load() explicitly starts the resource-fetch
    // algorithm, firing loadedmetadata even before play() is called, which
    // breaks the cycle without altering the overall autoplay flow.
    video.load()

    const onLoaded = () => {
      // Guard: destroy() may have been called before loadedmetadata fired.
      // The listener was already removed from the video element in destroy(),
      // but if somehow it still fires (e.g. browser queued the event before
      // removeEventListener ran), bail out silently.
      if (this.destroyed) return

      this.events.onLevelsLoaded([{ id: -1, label: 'Auto', height: 0, bitrate: 0 }])

      type NATList = { length: number; [i: number]: { label: string; language: string; enabled: boolean } }
      const nat = (video as HTMLVideoElement & { audioTracks?: NATList }).audioTracks
      if (nat?.length) {
        const tracks: AudioTrack[] = []
        for (let i = 0; i < nat.length; i++) {
          const t = nat[i]
          tracks.push({ id: i, label: t.label || t.language || `Track ${i + 1}`, language: t.language })
        }
        this.events.onAudioTracksLoaded(tracks)
      }

      const subs: SubtitleTrack[] = []
      for (let i = 0; i < video.textTracks.length; i++) {
        const t = video.textTracks[i]
        if (t.kind === 'subtitles' || t.kind === 'captions') {
          subs.push({ id: i, label: t.label || t.language || `Sub ${i + 1}`, language: t.language })
        }
      }
      if (subs.length) this.events.onSubtitleTracksLoaded(subs)

      this.events.onManifestParsed()
      // Self-cleanup: clear our tracked refs once the listener has fired.
      video.removeEventListener('loadedmetadata', onLoaded)
      this.nativeHLSVideo     = null
      this.nativeMetaListener = null
    }

    // Store refs so destroy() can remove this listener if it hasn't fired yet.
    this.nativeHLSVideo     = video
    this.nativeMetaListener = onLoaded
    video.addEventListener('loadedmetadata', onLoaded)
  }

  private initHLS(video: HTMLVideoElement, url: string): void {
    const config = buildConfig()
    const hls    = new Hls(config)
    this.hls     = hls

    hls.loadSource(url)
    hls.attachMedia(video)

    hls.on(Hls.Events.MANIFEST_PARSED, (_e, data) => {
      this.retryCount = 0
      this.tokenRefreshCount = 0   // successful manifest load — reset refresh counter

      const levels: QualityLevel[] = [
        { id: -1, label: 'Auto', height: 0, bitrate: 0 },
        ...data.levels.map((l, i) => ({
          id:      i,
          label:   l.height ? `${l.height}p` : `Level ${i}`,
          height:  l.height  ?? 0,
          bitrate: l.bitrate ?? 0,
        })),
      ]
      this.events.onLevelsLoaded(levels)
      this.events.onManifestParsed()
    })

    hls.on(Hls.Events.AUDIO_TRACKS_UPDATED, (_e, data) => {
      const tracks: AudioTrack[] = data.audioTracks.map((t, i) => ({
        id:       i,
        label:    t.name || t.lang || `Track ${i + 1}`,
        language: t.lang || '',
      }))
      this.events.onAudioTracksLoaded(tracks)
    })

    hls.on(Hls.Events.SUBTITLE_TRACKS_UPDATED, (_e, data) => {
      const tracks: SubtitleTrack[] = data.subtitleTracks.map((t, i) => ({
        id:       i,
        label:    t.name || t.lang || `Sub ${i + 1}`,
        language: t.lang || '',
      }))
      this.events.onSubtitleTracksLoaded(tracks)
    })

    hls.on(Hls.Events.LEVEL_SWITCHED, (_e, data) => {
      this.events.onLevelSwitched?.(data.level)
    })

    // Report bandwidth estimates so the UI can show a "slow connection" indicator
    hls.on(Hls.Events.FRAG_LOADED, (_e, data) => {
      const stats = data.frag.stats
      if (stats?.total && stats?.loading?.end && stats?.loading?.start) {
        const ms  = stats.loading.end - stats.loading.start
        const bps = (stats.total * 8 * 1000) / ms
        this.events.onBandwidthEstimate?.(bps)
      }
    })

    // Non-fatal stall/nudge — let hls.js self-recover, no UI error
    hls.on(Hls.Events.ERROR, (_e, data: ErrorData) => {
      if (!data.fatal) {
        // Log non-fatal events at debug level; don't surface to user
        if (data.details === Hls.ErrorDetails.BUFFER_STALLED_ERROR ||
            data.details === Hls.ErrorDetails.BUFFER_NUDGE_ON_STALL) {
          // hls.js will nudge automatically — nothing to do
        }
        return
      }
      this.handleFatalError(data, video)
    })
  }

  private handleFatalError(data: ErrorData, video: HTMLVideoElement): void {
    if (this.destroyed) return
    const msg = `${data.type}: ${data.details}`
    console.error('[HLSManager] fatal error:', msg)

    if (data.type === Hls.ErrorTypes.NETWORK_ERROR) {
      const errData      = data as ErrorData & { url?: string; response?: { code?: number } }
      const responseCode = errData.response?.code ?? 0
      const errUrl       = errData.url ?? ''

      // ── Load timeout (manifest, level playlist, or fragment) ──────────────
      // hls.js has already exhausted its own internal retries before emitting
      // this fatal event. Schedule our own retry loop; set lastTimeoutSeen so
      // the exhaustion message in scheduleRetry is "Source took too long to respond".
      if (
        data.details === Hls.ErrorDetails.MANIFEST_LOAD_TIMEOUT ||
        data.details === Hls.ErrorDetails.LEVEL_LOAD_TIMEOUT    ||
        data.details === Hls.ErrorDetails.FRAG_LOAD_TIMEOUT
      ) {
        this.lastTimeoutSeen = true
        this.scheduleRetry(video)
        return
      }

      // ── Manifest load error: classify by HTTP status ───────────────────────
      if (data.details === Hls.ErrorDetails.MANIFEST_LOAD_ERROR) {
        if (responseCode === 403 && this.proxyToken) {
          // ── Bug 2 fix: CDN signed-URL token expiry on live streams ──────────
          // The proxy root manifest returned 403.  This almost always means the
          // CDN signed-URL token has rotated.  If the application has registered
          // an onTokenExpired callback, invoke it so the app can fetch a fresh
          // signed URL and push it to the backend without changing the hls.js
          // source URL.  The same proxy endpoint serves the refreshed manifest
          // on the next request.
          if (this.events.onTokenExpired && this.tokenRefreshCount < MAX_TOKEN_REFRESHES) {
            this.attemptTokenRefresh(video)
            return
          }
          // No callback or exhausted refresh attempts — fall through to classify
        }

        if (responseCode > 0) {
          // Definitive HTTP error from origin — fail fast with a classified message.
          // 403 on a signed/expiring URL is treated as "expired" not "blocked".
          this.events.onError(classifyHttpError(responseCode, errUrl), true)
          return
        }
        // responseCode === 0: no response received at all
        // Bug #7 fix: distinguish CORS block from offline.
        if (navigator.onLine) {
          // Online + no response → almost certainly a CORS block
          this.events.onError(
            'Cannot load this HLS stream — the server is blocking cross-origin requests (CORS). ' +
            'Try a different stream or enable a backend proxy.',
            true,
          )
        } else {
          // Offline → retry silently; playback resumes when connection returns
          this.scheduleRetry(video)
        }
        return
      }
      // ── Invalid M3U8 / level playlist — fail fast with a clear message ─────
      if (
        data.details === Hls.ErrorDetails.MANIFEST_PARSING_ERROR ||
        data.details === Hls.ErrorDetails.LEVEL_PARSING_ERROR
      ) {
        this.events.onError('Invalid HLS playlist', true)
        return
      }

      // ── Fragment or level HTTP error — classify permanent codes fast ──────
      // 401/403/404/410/429 on a fragment or level playlist are unlikely to
      // succeed on retry; surface the classified message immediately.
      // 5xx and unrecognised codes (0 = no response) fall through to retry.
      if (
        data.details === Hls.ErrorDetails.FRAG_LOAD_ERROR ||
        data.details === Hls.ErrorDetails.LEVEL_LOAD_ERROR ||
        data.details === Hls.ErrorDetails.AUDIO_TRACK_LOAD_ERROR
      ) {
        const fragErrData  = data as ErrorData & { url?: string; response?: { code?: number } }
        const fragRespCode = fragErrData.response?.code ?? 0
        const fragErrUrl   = fragErrData.url ?? ''
        if (fragRespCode === 401 || fragRespCode === 403 || fragRespCode === 404 ||
            fragRespCode === 410 || fragRespCode === 429) {
          this.events.onError(classifyHttpError(fragRespCode, fragErrUrl), true)
          return
        }
        // 5xx or no-response: retry below
      }

      // On repeated failures to the same fragment, skip it
      const fragUrl = (data as ErrorData & { frag?: { url?: string } }).frag?.url
      if (fragUrl) {
        if (fragUrl === this.lastFailedFrag) {
          this.fragFailCount++
        } else {
          this.fragFailCount   = 1
          this.lastFailedFrag  = fragUrl
        }
        if (this.fragFailCount >= 3 && video.duration > 0) {
          // Skip past this fragment
          const skipTo = Math.min(video.currentTime + 4, video.duration - 1)
          console.log(`[HLSManager] skip-forward to ${skipTo.toFixed(1)}s (stuck fragment)`)
          video.currentTime = skipTo
          this.hls?.startLoad()
          this.fragFailCount = 0
          return
        }
      }
      this.scheduleRetry(video)

    } else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) {
      if (this.retryCount < MAX_FATAL_RETRIES) {
        console.log('[HLSManager] media error recovery attempt', this.retryCount + 1)
        this.hls?.recoverMediaError()
        this.retryCount++
      } else {
        this.events.onError('Format or codec not supported', true)
      }
    } else {
      this.events.onError('Unable to play this stream', true)
    }
  }

  /**
   * Token-refresh flow for Bug 2 (403 on live-stream manifest rotation).
   *
   * 1. Invoke onTokenExpired(token) — the app fetches a fresh signed CDN URL.
   * 2. Call POST /api/media/proxy/session/{token}/refresh with the new URL.
   * 3. On success: destroy the current hls.js instance and reinitialise
   *    against the SAME proxy URL.  The backend now serves the fresh manifest.
   * 4. On failure (app returned null, refresh endpoint returned 410, or network
   *    error): fall back to normal onError so the user sees a clear message.
   */
  private async attemptTokenRefresh(video: HTMLVideoElement): Promise<void> {
    if (this.destroyed || !this.proxyToken || !this.events.onTokenExpired) return

    this.tokenRefreshCount++
    const token = this.proxyToken
    console.log(`[HLSManager] token refresh attempt ${this.tokenRefreshCount}/${MAX_TOKEN_REFRESHES}`)

    let freshData: { url: string; headers: Record<string, string> } | null = null
    try {
      freshData = await this.events.onTokenExpired(token)
    } catch {
      // App callback threw — treat as refresh failure
    }

    if (this.destroyed) return   // was destroyed while waiting for the app callback

    if (!freshData) {
      // App couldn't provide a fresh URL — surface the original error
      this.events.onError('Access denied — stream token expired and could not be refreshed', true)
      return
    }

    const refreshed = await refreshProxySession(token, freshData.url, freshData.headers)

    if (this.destroyed) return

    if (!refreshed) {
      // Session expired on the backend (410) or network error
      if (this.tokenRefreshCount < MAX_TOKEN_REFRESHES) {
        // Session was evicted; try a normal retry which will create a new session
        // if the application handles session creation outside HLSManager
        this.scheduleRetry(video)
      } else {
        this.events.onError('Stream session expired — please reload the player', true)
      }
      return
    }

    // Backend updated — re-initialise hls.js against the same proxy URL.
    // The proxy URL hasn't changed; hls.js will now load the fresh manifest.
    console.log('[HLSManager] token refresh succeeded — re-initialising hls.js')
    if (this.hls) {
      this.hls.destroy()
      this.hls = null
    }
    this.retryCount = 0
    this.fragFailCount  = 0
    this.lastFailedFrag = null
    this.initHLS(video, this.currentSrc)
  }

  private scheduleRetry(video: HTMLVideoElement): void {
    if (this.destroyed) return
    if (this.retryCount >= MAX_FATAL_RETRIES) {
      const exhaustionMsg = this.lastTimeoutSeen
        ? 'Source took too long to respond'
        : `Could not load the stream after ${MAX_FATAL_RETRIES} retries. ` +
          `Check your connection or try a lower quality source.`
      this.events.onError(exhaustionMsg, true)
      return
    }

    const delay = retryDelay(this.retryCount)
    console.log(`[HLSManager] retry ${this.retryCount + 1}/${MAX_FATAL_RETRIES} in ${delay}ms`)
    this.retryCount++

    if (this.retryTimer !== null) clearTimeout(this.retryTimer)
    this.retryTimer = setTimeout(() => {
      if (this.destroyed) return
      // Bug #6: the old code called this.hls.startLoad() after a fatal manifest
      // error.  After a MANIFEST_LOAD_ERROR, hls.js has already exhausted its
      // own retries and put the instance into an unrecoverable state —
      // startLoad() only resumes *segment* loading on an already-parsed manifest;
      // it cannot reload a failed manifest.
      // Fix: always destroy the old instance and reinitialise from scratch so
      // the manifest is requested again from the server.
      if (this.hls) {
        this.hls.destroy()
        this.hls = null
      }
      this.initHLS(video, this.currentSrc)
    }, delay)
  }

  setLevel(levelId: number): void {
    if (!this.hls) return
    if (levelId === -1) {
      this.hls.currentLevel = -1   // re-enable ABR
    } else {
      this.hls.nextLevel = levelId  // switch without flushing buffer
    }
  }

  setAudioTrack(trackId: number): void   { if (this.hls) this.hls.audioTrack    = trackId }
  setSubtitleTrack(trackId: number): void { if (this.hls) this.hls.subtitleTrack = trackId }

  /** Returns the current bandwidth estimate in bps (0 if unknown) */
  getBandwidth(): number {
    return (this.hls as Hls & { bandwidthEstimate?: number })?.bandwidthEstimate ?? 0
  }

  destroy(): void {
    this.destroyed = true
    if (this.retryTimer !== null) { clearTimeout(this.retryTimer); this.retryTimer = null }
    if (this.hls) { this.hls.destroy(); this.hls = null }
    // Bug fix: remove the native-HLS loadedmetadata listener if it hasn't
    // fired yet. Without this, destroying the manager before loadedmetadata
    // fires leaves an orphaned listener that will eventually call
    // onManifestParsed() (and thus playWithPolicy / setState) on a component
    // that has already unmounted or changed source.
    if (this.nativeHLSVideo && this.nativeMetaListener) {
      this.nativeHLSVideo.removeEventListener('loadedmetadata', this.nativeMetaListener)
      this.nativeHLSVideo     = null
      this.nativeMetaListener = null
    }
    this.currentSrc    = ''
    this.retryCount    = 0
    this.fragFailCount = 0
    this.lastFailedFrag = null
    this.proxyToken     = null
    this.tokenRefreshCount = 0
  }
}
