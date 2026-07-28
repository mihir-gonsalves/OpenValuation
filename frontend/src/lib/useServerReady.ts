import { useEffect, useState } from 'react'
import { useServerHealth } from '@/api/queries'

export interface ServerReady {
  ready: boolean
  warming: boolean
}

/**
 * Cold-start awareness for the landing page.
 *
 * `warming` only turns true after a short grace period, so a server that is already warm
 * resolves before the notice would ever show - there's no flash of "waking up" on a fast load.
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
