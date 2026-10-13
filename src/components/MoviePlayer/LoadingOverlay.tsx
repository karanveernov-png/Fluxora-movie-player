interface LoadingOverlayProps {
  visible: boolean
}

export function LoadingOverlay({ visible }: LoadingOverlayProps) {
  if (!visible) return null
  return (
    <div className="overlay loading-overlay" aria-label="Buffering" role="status">
      <div className="spinner" />
    </div>
  )
}
