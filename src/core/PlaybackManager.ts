/**
 * PlaybackManager
 * Direct interface to HTMLVideoElement.
 *
 * Stall detection (new):
 * ─────────────────────
 * Starts a 2-second interval poll when the video is playing.
 * If currentTime stops advancing despite playing=true, the video
 * is considered stalled. We nudge the playhead forward by 0.3 s
 * to skip over broken/missing data. After MAX_NUDGES without progress
 * we call the onPersistentStall callback so the UI can react.
 *
 * Volume strategy — device volume sync (unchanged):
 * ────────────────────────────────────────────────
 * We write directly to video.volume so browsers route it to the OS
 * media-volume channel; hardware keys and the in-app slider stay in sync.
 */

const STALL_INTERVAL_MS  = 2_000   // check every 2 s
const STALL_NUDGE_S      = 0.3     // seek forward this far on each stall
const MAX_NUDGES         = 6       // give up after this many nudges

export class PlaybackManager {
  private video: HTMLVideoElement | null = null
  private _volume = 1

  // Stall detection
  private stallTimer:   ReturnType<typeof setInterval> | null = null
  private lastTime      = 0
  private nudgeCount    = 0
  private onStallCb?:   () => void

  attach(video: HTMLVideoElement): void {
    this.video = video
    video.volume = this._volume
  }

  detach(): void {
    this.stopStallDetection()
    this.video = null
  }

  play(): Promise<void> {
    return this.video?.play() ?? Promise.resolve()
  }

  pause(): void {
    this.video?.pause()
  }

  seek(seconds: number): void {
    if (!this.video) return
    this.video.currentTime = Math.max(0, Math.min(seconds, this.video.duration || 0))
    // Reset stall tracking after a manual seek — currentTime is expected to change
    this.lastTime    = this.video.currentTime
    this.nudgeCount  = 0
  }

  setVolume(level: number): void {
    const v = Math.max(0, Math.min(1, level))
    this._volume = v
    if (this.video) this.video.volume = v
  }

  getCurrentVolume(): number {
    return this.video?.volume ?? this._volume
  }

  setMuted(muted: boolean): void {
    if (this.video) this.video.muted = muted
  }

  getCurrentTime(): number  { return this.video?.currentTime ?? 0 }
  getDuration(): number     { return this.video?.duration    ?? 0 }

  getBufferedAhead(): number {
    if (!this.video) return 0
    const ct  = this.video.currentTime
    const buf = this.video.buffered
    for (let i = 0; i < buf.length; i++) {
      if (buf.start(i) <= ct && buf.end(i) >= ct) return buf.end(i) - ct
    }
    return 0
  }

  // ── Stall detection ────────────────────────────────────────────────────────

  /**
   * Start watching for playback stalls.
   * @param onPersistentStall  called once when nudges are exhausted
   */
  startStallDetection(onPersistentStall?: () => void): void {
    this.stopStallDetection()
    this.onStallCb  = onPersistentStall
    this.nudgeCount = 0
    this.lastTime   = this.video?.currentTime ?? 0

    this.stallTimer = setInterval(() => {
      const video = this.video
      if (!video || video.paused || video.ended || video.readyState < 3) {
        // Not playing or not ready — reset and skip
        this.nudgeCount = 0
        this.lastTime   = video?.currentTime ?? 0
        return
      }

      const ct = video.currentTime

      if (ct === this.lastTime) {
        // Time hasn't advanced — we're stalled
        this.nudgeCount++

        if (this.nudgeCount <= MAX_NUDGES) {
          const nudgeTo = Math.min(ct + STALL_NUDGE_S, video.duration - 0.5)
          if (nudgeTo > ct) {
            console.warn(`[PlaybackManager] stall nudge ${this.nudgeCount}/${MAX_NUDGES} → ${nudgeTo.toFixed(2)}s`)
            video.currentTime = nudgeTo
          }
        }

        if (this.nudgeCount > MAX_NUDGES) {
          console.error('[PlaybackManager] persistent stall — notifying parent')
          this.stopStallDetection()
          this.onStallCb?.()
        }
      } else {
        // Time advanced — clear stall counter
        if (this.nudgeCount > 0) console.log('[PlaybackManager] stall recovered')
        this.nudgeCount = 0
      }

      this.lastTime = ct
    }, STALL_INTERVAL_MS)
  }

  stopStallDetection(): void {
    if (this.stallTimer !== null) {
      clearInterval(this.stallTimer)
      this.stallTimer = null
    }
    this.nudgeCount = 0
  }

  // ── Fullscreen ─────────────────────────────────────────────────────────────

  async enterFullscreen(container: HTMLElement): Promise<void> {
    if (document.fullscreenElement) return
    try {
      await container.requestFullscreen()
    } catch {
      const v = this.video as HTMLVideoElement & { webkitEnterFullscreen?: () => void }
      v?.webkitEnterFullscreen?.()
    }
  }

  async exitFullscreen(): Promise<void> {
    if (!document.fullscreenElement) return
    await document.exitFullscreen()
  }
}
