import { Link } from 'react-router-dom'

/**
 * Shown on the landing page while the backend wakes from idle. The
 * search stays usable underneath - this only sets the expectation that the
 * first request takes a moment, so the wait reads as expected, not broken.
 */
export function WarmingNotice() {
  return (
    <div
      className="flex items-center p-3 gap-3 bg-popover border rounded-md text-xs text-muted-foreground"
      role="status"
      aria-live="polite"
    >
      <div
        aria-hidden="true"
        className="relative rounded-full size-2 bg-primary before:absolute before:rounded-full before:inset-0 before:bg-primary/50 before:animate-ping"
      />
      <p>
        <strong className="font-medium text-foreground">Waking the server.</strong>{" "}
        <Link to="/learn" className="underline">
          Learn more
        </Link>
        .
      </p>
    </div>
  )
}
