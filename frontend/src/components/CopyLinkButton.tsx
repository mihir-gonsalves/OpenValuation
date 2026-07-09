import { useEffect, useRef, useState } from 'react'
import { Check, Link2 } from 'lucide-react'
import { Button } from '@/components/ui/button'

/** Copies the current URL (which carries ?cik=) for sharing a result. */
export function CopyLinkButton() {
  const [copied, setCopied] = useState(false)
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined)

  useEffect(() => () => clearTimeout(timer.current), [])

  async function copy() {
    try {
      await navigator.clipboard.writeText(window.location.href)
      setCopied(true)
      timer.current = setTimeout(() => setCopied(false), 1500)
    } catch {
      // Clipboard blocked (e.g. insecure context) - silently ignore.
    }
  }

  return (
    <Button
      onClick={copy}
      aria-label="Copy link to this page"
      className="h-9.5"
    >
      {copied ? <Check /> : <Link2 />}
      {copied ? 'Copied' : 'Copy link'}
    </Button>
  )
}
