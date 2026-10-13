/**
 * MediaInfoPanel.tsx — Technical playback information panel.
 *
 * Shows:
 *   Basic row: container · codec · resolution · fps · bitrate
 *   Expandable: all streams, strategy reason, keyboard shortcuts
 *
 * Data sources:
 *   • PlayDecision from backend (codec/container/strategy)
 *   • Live playback stats from VideoPlayerState (buffered, rate)
 *   • Optional override via props
 */

import { useState } from 'react'
import type { PlayDecision } from '../types'
import type { VideoPlayerState } from '../hooks/useVideoPlayer'

interface Props {
  decision: PlayDecision | null
  playerState: VideoPlayerState
  sourceLabel: string
  onClose: () => void
}

export function MediaInfoPanel({ decision, playerState, sourceLabel, onClose }: Props) {
  const [expanded, setExpanded] = useState(false)
  const probe = decision?.probe

  const formatBitrate = (bps: number) => {
    if (!bps) return '—'
    if (bps >= 1_000_000) return `${(bps / 1_000_000).toFixed(1)} Mbps`
    return `${(bps / 1000).toFixed(0)} Kbps`
  }

  const formatSize = (bytes: number) => {
    if (!bytes) return '—'
    if (bytes >= 1_073_741_824) return `${(bytes / 1_073_741_824).toFixed(1)} GB`
    if (bytes >= 1_048_576) return `${(bytes / 1_048_576).toFixed(0)} MB`
    return `${(bytes / 1024).toFixed(0)} KB`
  }

  const strategyBadge = (s: string) => {
    const colors: Record<string, string> = {
      direct:        '#34d399',
      remux:         '#60a5fa',
      transcode:     '#f59e0b',
      hls_transcode: '#f59e0b',
      unsupported:   '#f87171',
    }
    return colors[s] ?? '#9ca3af'
  }

  return (
    <aside className="av-info-panel" aria-label="Media information">
      {/* Header */}
      <div className="av-info-header">
        <span className="av-info-title">Media Info</span>
        <button className="av-info-close" onClick={onClose} aria-label="Close">×</button>
      </div>

      {/* Source */}
      <div className="av-info-section">
        <div className="av-info-row">
          <span className="av-info-label">Source</span>
          <span className="av-info-value av-info-url" title={sourceLabel}>{sourceLabel}</span>
        </div>

        {/* Strategy badge */}
        {decision && (
          <div className="av-info-row">
            <span className="av-info-label">Playback</span>
            <span
              className="av-strategy-badge"
              style={{ background: strategyBadge(decision.strategy) + '22', color: strategyBadge(decision.strategy) }}
            >
              {decision.strategy.replace('_', ' ').toUpperCase()}
            </span>
          </div>
        )}

        {decision?.reason && (
          <div className="av-info-reason">{decision.reason}</div>
        )}
      </div>

      {/* Video info */}
      {probe && (
        <div className="av-info-section">
          <dl className="av-info-grid">
            <dt>Container</dt>
            <dd>{_friendlyContainer(probe.container)}</dd>

            <dt>Video</dt>
            <dd>{probe.video_codec.toUpperCase() || '—'}</dd>

            <dt>Resolution</dt>
            <dd>{probe.width && probe.height ? `${probe.width}×${probe.height}` : '—'}</dd>

            <dt>Frame rate</dt>
            <dd>{probe.fps ? `${probe.fps.toFixed(2)} fps` : '—'}</dd>

            <dt>Audio</dt>
            <dd>{probe.audio_codec.toUpperCase() || '—'}</dd>

            <dt>Bitrate</dt>
            <dd>{formatBitrate(probe.bit_rate)}</dd>

            <dt>File size</dt>
            <dd>{formatSize(probe.size)}</dd>

            <dt>Duration</dt>
            <dd>{probe.duration ? _formatDuration(probe.duration) : '—'}</dd>
          </dl>

          {probe.probe_error && (
            <div className="av-info-warn">⚠ {probe.probe_error}</div>
          )}
        </div>
      )}

      {/* Live stats */}
      <div className="av-info-section">
        <dl className="av-info-grid">
          <dt>Buffered</dt>
          <dd>{playerState.bufferedAhead.toFixed(1)}s</dd>
          <dt>Quality</dt>
          <dd>
            {playerState.qualityLevels.find(q => q.id === playerState.currentQuality)?.label ?? 'Auto'}
          </dd>
          <dt>Speed</dt>
          <dd>{playerState.playbackRate}×</dd>
          <dt>Subtitles</dt>
          <dd>
            {playerState.currentSubtitle === -1
              ? 'Off'
              : playerState.subtitleTracks.find(t => t.id === playerState.currentSubtitle)?.label ?? 'On'}
          </dd>
        </dl>
      </div>

      {/* Expandable streams */}
      {probe && (probe.audio_streams.length > 1 || probe.subtitle_streams.length > 0) && (
        <div className="av-info-section">
          <button
            className="av-info-expand"
            onClick={() => setExpanded(e => !e)}
          >
            {expanded ? '▾' : '▸'} All streams ({probe.audio_streams.length + probe.video_streams.length + probe.subtitle_streams.length})
          </button>
          {expanded && (
            <div className="av-streams">
              {probe.video_streams.map(s => (
                <div key={s.index} className="av-stream-row">
                  <span className="av-stream-type video">VIDEO</span>
                  <span>{s.codec_name.toUpperCase()} {s.width}×{s.height} {s.fps?.toFixed(1)}fps</span>
                </div>
              ))}
              {probe.audio_streams.map(s => (
                <div key={s.index} className="av-stream-row">
                  <span className="av-stream-type audio">AUDIO</span>
                  <span>{s.codec_name.toUpperCase()} {s.channels}ch {s.language ?? ''}</span>
                </div>
              ))}
              {probe.subtitle_streams.map(s => (
                <div key={s.index} className="av-stream-row">
                  <span className="av-stream-type sub">SUB</span>
                  <span>{s.codec_name.toUpperCase()} {s.language ?? ''} {s.title ?? ''}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Keyboard shortcuts */}
      <div className="av-info-section av-info-shortcuts">
        <p className="av-info-sub-title">Keyboard shortcuts</p>
        <dl className="av-kbd-grid">
          <dt><kbd>Space</kbd></dt><dd>Play / Pause</dd>
          <dt><kbd>←</kbd><kbd>→</kbd></dt><dd>Seek ±10s</dd>
          <dt><kbd>↑</kbd><kbd>↓</kbd></dt><dd>Volume</dd>
          <dt><kbd>M</kbd></dt><dd>Mute</dd>
          <dt><kbd>F</kbd></dt><dd>Fullscreen</dd>
          <dt><kbd>I</kbd></dt><dd>Picture-in-Picture</dd>
          <dt><kbd>,</kbd><kbd>.</kbd></dt><dd>Speed −/+</dd>
          <dt><kbd>0</kbd>–<kbd>9</kbd></dt><dd>Jump to %</dd>
        </dl>
      </div>
    </aside>
  )
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function _friendlyContainer(raw: string): string {
  const map: Record<string, string> = {
    'mov,mp4,m4a,3gp,3g2,mj2': 'MP4',
    'matroska,webm': 'MKV',
    webm: 'WebM',
    ogg: 'OGG',
    avi: 'AVI',
    asf: 'WMV/ASF',
    flv: 'FLV',
    mpeg: 'MPEG',
  }
  for (const [k, v] of Object.entries(map)) {
    if (raw.includes(k) || k.includes(raw)) return v
  }
  return raw.toUpperCase() || '—'
}

function _formatDuration(secs: number): string {
  const h = Math.floor(secs / 3600)
  const m = Math.floor((secs % 3600) / 60)
  const s = Math.floor(secs % 60)
  if (h > 0) return `${h}h ${m}m ${s}s`
  if (m > 0) return `${m}m ${s}s`
  return `${s}s`
}
