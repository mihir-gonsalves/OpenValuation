import { useEffect, useState } from 'react'
import { useServerHealth } from '@/api/queries'

export interface ServerReady {
  /** The backend has answered and searches will succeed. */
  ready: boolean
  /** Still waking up, and slow enough to be worth telling the user about. */
  warming: boolean
}

/**
 * Cold-start awareness for the landing page. The backend runs on a free tier
 * that spins down when idle, so the first request after a nap is slow.
 *
 * `warming` only turns true after a short grace period, so a server that is
 * already warm resolves before the notice would ever show - no flash of "waking up" 
 * on a fast load.
 */
export function useServerReady(): ServerReady {
  const { isSuccess } = useServerHealth()
  const [graceElapsed, setGraceElapsed] = useState(false)

  useEffect(() => {
    if (isSuccess) return
    const id = setTimeout(() => setGraceElapsed(true), 600)
    return () => clearTimeout(id)
  }, [isSuccess])

  return { ready: isSuccess, warming: !isSuccess && graceElapsed }
}
