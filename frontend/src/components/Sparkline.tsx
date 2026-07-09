import { cn } from '@/lib/utils'

interface SparklineProps {
  /** Values ordered oldest -> newest. */
  values: (number | null)[]
  width?: number
  height?: number
  className?: string
  'aria-label'?: string
}

/**
 * Minimal, flat inline-SVG sparkline- a single hairline stroke plus an end dot. 
 * Gaps (null values) break the line so missing TTM periods read as missing rather than interpolated.
 */
export function Sparkline({
  values,
  width = 100,
  height = 28,
  className,
  'aria-label': ariaLabel,
}: SparklineProps) {
  const nums = values.filter((v): v is number => v !== null && !Number.isNaN(v))

  // Need at least two real points to draw a trend.
  if (nums.length < 2) {
    return (
      <svg
        width={width}
        height={height}
        className={cn('text-muted-foreground/50', className)}
        role="img"
        aria-label={ariaLabel ?? 'No trend data'}
      >
        <line
          x1={0}
          y1={height / 2}
          x2={width}
          y2={height / 2}
          stroke="currentColor"
          strokeWidth={1}
          strokeDasharray="4 4"
        />
      </svg>
    )
  }

  const min = Math.min(...nums)
  const max = Math.max(...nums)
  const span = max - min || 1
  const pad = 2
  const stepX = (width - pad * 2) / (values.length - 1)

  const points = values.map((v, i) => {
    if (v === null || Number.isNaN(v)) return null
    const x = pad + i * stepX
    const y = pad + (1 - (v - min) / span) * (height - pad * 2)
    return { x, y }
  })

  // Split into contiguous runs of non-null points.
  const segments: { x: number; y: number }[][] = []
  let current: { x: number; y: number }[] = []
  for (const p of points) {
    if (p) {
      current.push(p)
    } else if (current.length) {
      segments.push(current)
      current = []
    }
  }
  if (current.length) segments.push(current)

  const last = points.filter(Boolean).at(-1)!

  return (
    <svg
      width={width}
      height={height}
      className={cn('text-trend', className)}
      role="img"
      aria-label={ariaLabel ?? 'Trend across periods'}
    >
      {segments.map((seg, i) =>
        seg.length === 1 ? (
          <circle key={i} cx={seg[0].x} cy={seg[0].y} r={1.25} fill="currentColor" />
        ) : (
          <polyline
            key={i}
            fill="none"
            stroke="currentColor"
            strokeWidth={1.25}
            strokeLinejoin="round"
            strokeLinecap="round"
            points={seg.map((p) => `${p.x},${p.y}`).join(' ')}
          />
        ),
      )}
      <circle cx={last.x} cy={last.y} r={1.75} fill="currentColor" />
    </svg>
  )
}
