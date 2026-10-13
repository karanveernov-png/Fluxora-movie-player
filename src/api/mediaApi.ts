/**
 * api/mediaApi.ts — Typed fetch client for the Fluxora backend.
 *
 * All functions return typed responses or throw a descriptive Error.
 * Never throws for expected server errors — returns them as typed objects.
 */

import type {
  MediaProbeInfo,
  CompatibilityInfo,
  PlayDecision,
  BackendHealth,
  ProxySession,
  RtmpRelayResult,
  AppSettings,
} from '../types'

const BASE = import.meta.env.VITE_API_URL ?? 'http://localhost:8000/api'
const TIMEOUT_MS = 12_000

async function fetchJSON<T>(
  path: string,
  options?: RequestInit,
  signal?: AbortSignal,
  timeoutMs = TIMEOUT_MS,
): Promise<T> {
  // Bug #8: when a caller passed their own `signal`, the old code used it
  // directly — the internal timeout controller was created but never connected
  // to the fetch, so the timeout never fired.
  // Fix: always combine both signals so the first one to abort wins.
  const timeoutController = new AbortController()
  const timer = setTimeout(
    () => timeoutController.abort(new Error(`Request timed out after ${timeoutMs}ms`)),
    timeoutMs,
  )

  // AbortSignal.any() is available in Chrome 116+, Firefox 115+, Safari 17.4+.
  // Polyfill for older runtimes: forward the caller's abort into our controller.
  let combinedSignal: AbortSignal
  if (signal) {
    if (typeof AbortSignal.any === 'function') {
      combinedSignal = AbortSignal.any([signal, timeoutController.signal])
    } else {
      combinedSignal = timeoutController.signal
      signal.addEventListener(
        'abort',
        () => timeoutController.abort(signal.reason),
        { once: true },
      )
    }
  } else {
    combinedSignal = timeoutController.signal
  }

  try {
    const response = await fetch(`${BASE}${path}`, {
      ...options,
      signal: combinedSignal,
      headers: {
        'Content-Type': 'application/json',
        ...(options?.headers ?? {}),
      },
    })

    clearTimeout(timer)

    if (!response.ok) {
      let detail = `HTTP ${response.status}`
      try {
        const body = await response.json()
        detail = body.detail ?? detail
      } catch { /* ignore */ }
      throw new Error(detail)
    }

    return response.json() as Promise<T>
  } catch (err) {
    clearTimeout(timer)
    throw err
  }
}

// ── Health ────────────────────────────────────────────────────────────────────

export async function fetchHealth(): Promise<BackendHealth> {
  return fetchJSON<BackendHealth>('/health')
}

// ── Probe ─────────────────────────────────────────────────────────────────────

export async function probeMedia(
  url: string,
  force = false,
): Promise<MediaProbeInfo> {
  return fetchJSON<MediaProbeInfo>('/media/probe', {
    method: 'POST',
    body: JSON.stringify({ url, force }),
  })
}

// ── Compatibility ─────────────────────────────────────────────────────────────

export async function checkCompatibility(
  url: string,
): Promise<CompatibilityInfo> {
  return fetchJSON<CompatibilityInfo>('/media/compatibility', {
    method: 'POST',
    body: JSON.stringify({ url }),
  })
}

// ── Full play decision ────────────────────────────────────────────────────────

export async function resolvePlayUrl(
  url: string,
  signal?: AbortSignal,
): Promise<PlayDecision> {
  return fetchJSON<PlayDecision>(
    '/media/play',
    { method: 'POST', body: JSON.stringify({ url }) },
    signal,
  )
}

// ── Stream poll (for remux jobs not yet ready) ────────────────────────────────

export async function pollStreamReady(
  jobId: string,
  maxAttempts = 20,
  intervalMs = 1500,
): Promise<boolean> {
  const url = `${BASE}/media/stream/${jobId}`
  for (let i = 0; i < maxAttempts; i++) {
    try {
      const r = await fetch(url, { method: 'HEAD' })
      // B12 fix: check r.status === 200, NOT r.ok.
      // r.ok is true for all 2xx — including 202 (Accepted / still processing).
      // The stream endpoint now returns 404 while the job is in progress and
      // 200 once the file is ready, so r.ok would have returned true on 202
      // and caused the player to load a non-existent file.
      if (r.status === 200) return true
      // 404 = not ready yet (processing) — keep polling
      // 4xx other than 404 = permanent failure — stop polling
      if (r.status !== 404) return false
    } catch { /* network hiccup — keep polling */ }
    await new Promise(res => setTimeout(res, intervalMs))
  }
  return false
}

// ── Auth-header proxy ─────────────────────────────────────────────────────────

/**
 * Register auth headers for a URL and get back a proxy token + play URL.
 * Load `proxy_url` as an HLS source — the backend fetches all segments
 * server-side with your headers injected.
 *
 * Supported: Bearer tokens, signed URLs, X-Auth-Token, Cookie, etc.
 * HLS streams (.m3u8) only. Use for private CDNs, Akamai, corporate video.
 */
export async function createProxySession(
  url: string,
  headers: Record<string, string>,
): Promise<ProxySession> {
  return fetchJSON<ProxySession>('/media/proxy/session', {
    method: 'POST',
    body: JSON.stringify({ url, headers }),
  })
}

/**
 * Push a fresh CDN URL (and updated auth headers) into an existing proxy
 * session without changing the hls.js source URL.
 *
 * Call this when the root manifest returns 403 — it means the CDN's
 * signed-URL token has rotated.  After a successful refresh the backend
 * serves the new manifest on the next request to the same proxy endpoint.
 *
 * Returns the same token + proxy_url on success.
 * Throws if the session has expired (HTTP 410) — caller should create a
 * new session via createProxySession() and re-attach the player.
 */
export async function refreshProxySession(
  token: string,
  url: string,
  headers: Record<string, string>,
): Promise<ProxySession> {
  return fetchJSON<ProxySession>(`/media/proxy/session/${token}/refresh`, {
    method: 'POST',
    body: JSON.stringify({ url, headers }),
  })
}

// ── RTMP / RTSP relay ─────────────────────────────────────────────────────────

/**
 * Start an RTMP or RTSP → HLS relay on the backend.
 * Returns immediately with a play_url (HLS). Poll pollHlsReady(key) before loading.
 *
 * Supported schemes: rtmp://, rtmps://, rtsp://, rtsps://
 * Use cases: OBS streams, IP cameras, NVR systems.
 */
export async function relayRtmp(rtmpUrl: string): Promise<RtmpRelayResult> {
  return fetchJSON<RtmpRelayResult>('/media/rtmp/relay', {
    method: 'POST',
    body: JSON.stringify({ rtmp_url: rtmpUrl }),
  })
}

/**
 * Poll an HLS playlist URL until ffmpeg has written the first segments.
 * Starts succeeding ~2–5 seconds after the relay is launched.
 */
export async function pollHlsReady(
  key: string,
  maxAttempts = 60,
  intervalMs = 500,
): Promise<boolean> {
  const url = `${BASE}/media/hls/${key}/playlist.m3u8`
  for (let i = 0; i < maxAttempts; i++) {
    try {
      const r = await fetch(url, { method: 'HEAD' })
      if (r.ok) return true
    } catch { /* keep polling */ }
    await new Promise(res => setTimeout(res, intervalMs))
  }
  return false
}

// ── Runtime settings ──────────────────────────────────────────────────────────

export async function getSettings(): Promise<AppSettings> {
  return fetchJSON<AppSettings>('/settings')
}

/**
 * Update runtime settings on the backend.
 * allow_private_ips: permit fetching from 192.168.x.x, 127.x.x.x, 10.x.x.x
 * so you can stream from Plex, Jellyfin, or any LAN media server.
 */
export async function updateSettings(
  patch: Partial<AppSettings>,
): Promise<AppSettings> {
  return fetchJSON<AppSettings>('/settings', {
    method: 'POST',
    body: JSON.stringify(patch),
  })
}
