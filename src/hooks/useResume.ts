/**
 * useResume.ts — Per-URL playback position persistence.
 *
 * Saves currentTime to localStorage every SAVE_INTERVAL ms while playing.
 * Restores position after loadedmetadata fires.
 * Clears saved position when video ends or position is within 5s of start/end.
 *
 * Key format: fluxora_resume_<sha-like-hash-of-url>
 * to avoid very long localStorage keys.
 */

import { useEffect, useRef, useCallback, type RefObject } from 'react'

const SAVE_INTERVAL_MS = 5_000
const MIN_SAVE_POSITION = 5     // don't save if within 5s of start
const END_MARGIN = 10           // don't save if within 10s of end

function urlKey(url: string): string {
  // Simple FNV-like hash — good enough for localStorage keys
  let h = 0x811c9dc5
  for (let i = 0; i < url.length; i++) {
    h ^= url.charCodeAt(i)
    h = (h * 0x01000193) >>> 0
  }
  return `fluxora_resume_${h.toString(16)}`
}

function loadPosition(url: string): number | null {
  try {
    const raw = localStorage.getItem(urlKey(url))
    if (!raw) return null
    const val = parseFloat(raw)
    return isFinite(val) && val > 0 ? val : null
  } catch {
    return null
  }
}

function savePosition(url: string, time: number): void {
  try {
    localStorage.setItem(urlKey(url), String(time))
  } catch { /* storage full — ignore */ }
}

function clearPosition(url: string): void {
  try {
    localStorage.removeItem(urlKey(url))
  } catch { /* ignore */ }
}

interface UseResumeOptions {
  url: string | null
  videoRef: RefObject<HTMLVideoElement | null>
  enabled?: boolean
  /** Called when a resume position is restored, with the time in seconds */
  onResumed?: (time: number) => void
}

export function useResume({
  url,
  videoRef,
  enabled = true,
  onResumed,
}: UseResumeOptions): {
  savedPosition: number | null
  clearSaved: () => void
} {
  const saveTimer    = useRef<ReturnType<typeof setInterval> | null>(null)
  const currentUrl   = useRef<string | null>(null)
  // Bug #4: onResumed is an inline callback prop that changes on every render.
  // Storing it in a ref breaks the dependency cycle so the restore effect only
  // re-runs when url/enabled/videoRef change, not on every parent render.
  const onResumedRef = useRef(onResumed)
  onResumedRef.current = onResumed

  const clearSaved = useCallback(() => {
    if (currentUrl.current) clearPosition(currentUrl.current)
  }, [])

  // Restore position after metadata loads
  useEffect(() => {
    if (!enabled || !url) return
    currentUrl.current = url

    const video = videoRef.current
    if (!video) return

    const onMeta = () => {
      const saved = loadPosition(url)
      // Skip resume for live streams (duration === Infinity) — the position is
      // meaningless and seeking to a stored time on a live source is an error.
      if (
        saved &&
        video.duration &&
        isFinite(video.duration) &&
        saved > MIN_SAVE_POSITION &&
        saved < video.duration - END_MARGIN
      ) {
        video.currentTime = saved
        onResumedRef.current?.(saved)
      }
    }

    // If metadata is already loaded, run immediately (but only once per url change).
    // Bug #4 (old code): checking readyState here AND including onResumed in deps
    // caused this effect to re-run and re-seek on every render after metadata loaded.
    // Fix: use a { once: true } listener unconditionally — if metadata is already
    // loaded, dispatch a synthetic event that fires the listener immediately.
    if (video.readyState >= 1 && isFinite(video.duration) && video.duration > 0) {
      // Metadata already available: call synchronously, no event needed.
      onMeta()
    } else {
      video.addEventListener('loadedmetadata', onMeta, { once: true })
    }

    return () => {
      video.removeEventListener('loadedmetadata', onMeta)
    }
  // onResumed intentionally excluded — we use onResumedRef to avoid re-runs (Bug #4)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [url, enabled, videoRef])

  // Periodic save
  useEffect(() => {
    if (!enabled || !url) return

    const video = videoRef.current
    if (!video) return

    const tick = () => {
      const ct = video.currentTime
      const dur = video.duration
      if (!isFinite(ct) || !isFinite(dur)) return
      if (ct < MIN_SAVE_POSITION || dur - ct < END_MARGIN) {
        clearPosition(url)
        return
      }
      savePosition(url, ct)
    }

    saveTimer.current = setInterval(tick, SAVE_INTERVAL_MS)

    const onEnded = () => clearPosition(url)
    video.addEventListener('ended', onEnded)

    return () => {
      if (saveTimer.current) clearInterval(saveTimer.current)
      video.removeEventListener('ended', onEnded)
    }
  }, [url, enabled, videoRef])

  return {
    savedPosition: url ? loadPosition(url) : null,
    clearSaved,
  }
}
