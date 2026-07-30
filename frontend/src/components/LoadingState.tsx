import { useEffect, useState } from 'react'
import { Loader2 } from 'lucide-react'

/**
 * Fetch spinner. After a few seconds it explains the likely cause of a long
 * wait - so the delay reads as expected rather than broken.
 */
export function LoadingState() {
  const [slow, setSlow] = useState(false)

  useEffect(() => {
    const id = setTimeout(() => setSlow(true), 8000)
    return () => clearTimeout(id)
  }, [])

  return (
    <div
      className="flex flex-col items-center py-20 gap-3 text-center"
      role="status"
      aria-live="polite"
    >
      <Loader2 className="text-primary size-6 animate-spin" />
      <p className="text-foreground text-sm font-medium">Fetching filings, computing multiples...</p>
      {slow && (
        <p className="max-w-sm text-muted-foreground text-xs leading-relaxed">
          The server may be waking from idle. A cold start can take 30-50 seconds. It will respond shortly.
        </p>
      )}
    </div>
  )
}
