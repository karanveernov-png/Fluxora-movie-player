interface ErrorOverlayProps {
  message: string
  onRetry?: () => void
}

export function ErrorOverlay({ message, onRetry }: ErrorOverlayProps) {
  return (
    <div className="overlay error-overlay" role="alert">
      <div className="error-content">
        <svg
          className="error-icon"
          width="40" height="40" viewBox="0 0 24 24"
          fill="none" stroke="currentColor" strokeWidth="1.25"
          strokeLinecap="round" strokeLinejoin="round"
          aria-hidden="true"
        >
          <circle cx="12" cy="12" r="10" />
          <line x1="12" y1="8" x2="12" y2="12" />
          <circle cx="12" cy="16" r="0.5" fill="currentColor" />
        </svg>
        <p className="error-message">
          {message || 'Unable to play this media.'}
        </p>
        {onRetry && (
          <button className="btn-retry" onClick={onRetry} type="button">
            Try again
          </button>
        )}
      </div>
    </div>
  )
}
