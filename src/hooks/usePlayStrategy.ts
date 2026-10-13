/**
 * usePlayStrategy.ts — Resolves the optimal playback URL for a given source.
 *
 * Pipeline:
 *   1. If source is 'local', 'hls', or 'dash' → use directly (no backend needed)
 *   2. Call /api/media/play on the backend
 *   3. If strategy = 'remux' or 'transcode' → poll until job is ready
 *   4. Return resolved PlayerSource + playback metadata
 *
 * Falls back to the original URL if the backend is unavailable.
 *
 * Fix: DASH streams now bypass the backend just like HLS, so public .mpd URLs
 * (Akamai, test streams) load directly without a backend round-trip.
 */

import { useState, useEffect, useRef, useCallback } from 'react'
import type { PlayerSource, PlayDecision } from '../types'
import { resolvePlayUrl, pollStreamReady, pollHlsReady } from '../api/mediaApi'

type StrategyState =
  | { phase: 'idle' }
  | { phase: 'resolving' }
  | { phase: 'polling'; jobId: string }
  | { phase: 'ready'; source: PlayerSource; decision: PlayDecision }
  | { phase: 'error'; message: string }
  | { phase: 'fallback'; source: PlayerSource }  // backend unavailable

export function usePlayStrategy(rawSource: PlayerSource | null): {
  resolvedSource: PlayerSource | null
  decision: PlayDecision | null
  state: StrategyState
  retry: () => void
} {
  const [state, setState] = useState<StrategyState>({ phase: 'idle' })
  const abortRef = useRef<AbortController | null>(null)
  const rawRef = useRef(rawSource)
  rawRef.current = rawSource

  const resolve = useCallback(async (source: PlayerSource) => {
    // Sources that bypass the backend entirely and load directly into <video>:
    //  • local   — blob URL, no network needed
    //  • hls     — hls.js manages its own XHR pipeline
    //  • dash    — dash.js manages its own XHR pipeline
    //  • .webm   — WebM container spec allows ONLY VP8/VP9/AV1+Vorbis/Opus,
    //              all of which are browser-native; probing adds pure latency
    //  • .ogg / .ogv — likewise restricted to Theora/VP8/Vorbis/Opus
    const srcPath = source.url.toLowerCase().split('?')[0]
    const isNativeBrowserFormat =
      source.type === 'mp4' &&
      (srcPath.endsWith('.webm') || srcPath.endsWith('.ogg') || srcPath.endsWith('.ogv'))

    if (
      source.type === 'local' ||
      source.type === 'hls'   ||
      source.type === 'dash'  ||
      isNativeBrowserFormat
    ) {
      setState({ phase: 'ready', source, decision: _directDecision(source) })
      return
    }

    setState({ phase: 'resolving' })

    abortRef.current?.abort()
    const controller = new AbortController()
    abortRef.current = controller

    try {
      const decision = await resolvePlayUrl(source.url, controller.signal)

      if (controller.signal.aborted) return

      const resolvedSource: PlayerSource = {
        type: decision.source_type,
        url: decision.play_url,
      }

      // Wait for the background job to produce playable output.
      //
      // Bug fix: the old code polled pollStreamReady() (which checks
      // /media/stream/{id} for the remuxed MP4) for BOTH 'remux' AND
      // 'transcode'.  But 'transcode' and 'hls_transcode' both run
      // generate_hls() and write output to /media/hls/{id}/playlist.m3u8 —
      // a completely different path.  pollStreamReady() always got 404 for
      // those strategies and timed out after 30 s before the player ever
      // tried to load the (already-ready) HLS stream.
      //
      // Fix: route to the correct poll function by strategy:
      //   'remux'         → pollStreamReady  (waits for the MP4 file)
      //   'transcode' /   → pollHlsReady     (waits for the HLS playlist;
      //   'hls_transcode'    ffmpeg's append_list makes it available after
      //                      the first segment, not the full transcode)
      if (decision.job_id) {
        setState({ phase: 'polling', jobId: decision.job_id })
        const ready = decision.strategy === 'remux'
          ? await pollStreamReady(decision.job_id)
          : await pollHlsReady(decision.job_id)
        if (controller.signal.aborted) return
        if (!ready) {
          setState({ phase: 'error', message: 'Media processing timed out. Try again.' })
          return
        }
      }

      setState({ phase: 'ready', source: resolvedSource, decision })

    } catch (err) {
      if (controller.signal.aborted) return

      // Backend unavailable — fall back to direct play
      console.warn('[usePlayStrategy] backend unavailable, falling back:', err)
      setState({ phase: 'fallback', source })
    }
  }, [])

  useEffect(() => {
    if (!rawSource) {
      setState({ phase: 'idle' })
      return
    }
    resolve(rawSource)
    return () => { abortRef.current?.abort() }
  }, [rawSource, resolve])

  const retry = useCallback(() => {
    if (rawRef.current) resolve(rawRef.current)
  }, [resolve])

  // Derive outputs
  if (state.phase === 'ready') {
    return { resolvedSource: state.source, decision: state.decision, state, retry }
  }
  if (state.phase === 'fallback') {
    return { resolvedSource: state.source, decision: null, state, retry }
  }
  return { resolvedSource: null, decision: null, state, retry }
}

function _directDecision(source: PlayerSource): PlayDecision {
  return {
    original_url: source.url,
    play_url: source.url,
    source_type: source.type,
    strategy: 'direct',
    reason: 'Direct play — no backend processing required.',
  }
}
