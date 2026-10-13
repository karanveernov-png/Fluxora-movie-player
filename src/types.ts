// types.ts — Fluxora v2.1 type definitions

// ── Source ────────────────────────────────────────────────────────────────────

export interface PlayerSource {
  type: 'hls' | 'mp4' | 'local' | 'dash' | 'remuxed' | 'hls_generated'
  url: string
  headers?: Record<string, string>
  subtitles?: ExternalSubtitle[]
}

export interface ExternalSubtitle {
  label: string
  language: string
  src: string  // WebVTT URL
}

// ── Playback strategy ─────────────────────────────────────────────────────────

export type PlaybackStrategy =
  | 'direct'
  | 'remux'
  | 'transcode'
  | 'hls_transcode'
  | 'unsupported'

// ── Media info from backend ────────────────────────────────────────────────────

export interface StreamInfo {
  index: number
  codec_type: string
  codec_name: string
  codec_long_name: string
  width?: number | null
  height?: number | null
  fps?: number | null
  bit_rate?: number | null
  pix_fmt?: string | null
  sample_rate?: number | null
  channels?: number | null
  channel_layout?: string | null
  language?: string | null
  title?: string | null
  duration?: number | null
  profile?: string | null
}

export interface MediaProbeInfo {
  url: string
  container: string
  duration: number
  bit_rate: number
  size: number
  video_codec: string
  audio_codec: string
  width: number
  height: number
  fps: number
  video_streams: StreamInfo[]
  audio_streams: StreamInfo[]
  subtitle_streams: StreamInfo[]
  probe_error?: string | null
}

export interface CompatibilityInfo {
  url: string
  strategy: PlaybackStrategy
  reason: string
  target_container: string
  target_video_codec: string
  target_audio_codec: string
  video_codec: string
  audio_codec: string
  container: string
  needs_processing: boolean
  processing_url?: string | null
}

export interface PlayDecision {
  original_url: string
  play_url: string
  source_type: PlayerSource['type']
  strategy: PlaybackStrategy
  reason: string
  probe?: MediaProbeInfo | null
  job_id?: string | null
}

// ── Auth-header proxy ─────────────────────────────────────────────────────────

export interface ProxySession {
  token: string
  /** Load this URL as an HLS source — all segments are fetched server-side with your headers. */
  proxy_url: string
}

// ── RTMP relay ────────────────────────────────────────────────────────────────

export interface RtmpRelayResult {
  key: string
  /** HLS playlist URL. Poll with HEAD until 200 before loading. */
  play_url: string
  message: string
}

// ── Runtime settings ──────────────────────────────────────────────────────────

export interface AppSettings {
  /** When true, backend will fetch from 192.168.x.x, 127.x.x.x, etc. */
  allow_private_ips: boolean
}

// ── Player quality / track types ──────────────────────────────────────────────

export interface QualityLevel {
  id: number
  label: string
  height: number
  bitrate: number
}

export interface AudioTrack {
  id: number
  label: string
  language: string
}

export interface SubtitleTrack {
  id: number
  label: string
  language: string
}

// ── Player state ──────────────────────────────────────────────────────────────

export interface VideoPlayerState {
  playing: boolean
  buffering: boolean
  ready: boolean
  error: string | null
  duration: number
  bufferedAhead: number
  qualityLevels: QualityLevel[]
  audioTracks: AudioTrack[]
  subtitleTracks: SubtitleTrack[]
  currentQuality: number
  currentAudio: number
  currentSubtitle: number
  volume: number
  muted: boolean
  mutedForAutoplay: boolean
  fullscreen: boolean
  playbackRate: number
  pip: boolean
  pipSupported: boolean
  offline: boolean
}

// ── Player callbacks ──────────────────────────────────────────────────────────

export interface PlayerCallbacks {
  onPlay?: () => void
  onPause?: () => void
  onEnded?: () => void
  onProgress?: (position: number, duration: number) => void
  onError?: (error: string) => void
  onQualityChange?: (level: QualityLevel) => void
  onAudioChange?: (track: AudioTrack) => void
  onSubtitleChange?: (track: SubtitleTrack | null) => void
  /**
   * Called when the HLS proxy manifest returns 403 (CDN token expired).
   * Return a fresh { url, headers } to allow transparent token refresh,
   * or null to surface an error to the user.
   */
  onTokenExpired?: (token: string) => Promise<{ url: string; headers: Record<string, string> } | null>
}

// ── MoviePlayer props ─────────────────────────────────────────────────────────

export interface MoviePlayerProps extends PlayerCallbacks {
  source: PlayerSource
  autoPlay?: boolean
  muted?: boolean
  controls?: boolean
  fallbackSources?: PlayerSource[]
  className?: string
  style?: React.CSSProperties
  onStateChange?: (state: VideoPlayerState) => void
}

// ── Playback state ────────────────────────────────────────────────────────────

export interface PlaybackState {
  playing: boolean
  buffering: boolean
  currentTime: number
  duration: number
  buffered: number
  volume: number
  muted: boolean
  error: string | null
  ready: boolean
}

// ── Imperative handle ─────────────────────────────────────────────────────────

export interface PlayerHandle {
  play: () => void
  pause: () => void
  seek: (seconds: number) => void
  setVolume: (level: number) => void
  enterFullscreen: () => void
  exitFullscreen: () => void
}

// ── App state ─────────────────────────────────────────────────────────────────

export interface RecentEntry {
  url: string
  label: string
  type: PlayerSource['type']
  lastPlayedAt: number   // Unix ms
  resumeAt?: number      // seconds
}

// ── Backend health ────────────────────────────────────────────────────────────

export interface BackendHealth {
  status: string
  ffprobe_available: boolean
  ffmpeg_available: boolean
  version: string
}
