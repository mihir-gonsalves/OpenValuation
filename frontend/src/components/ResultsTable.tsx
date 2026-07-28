import type React from 'react'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow, } from '@/components/ui/table'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { Badge } from '@/components/ui/badge'
import { Sparkline } from '@/components/Sparkline'
import { cn } from '@/lib/utils'
import { dedupeWarnings, formatDate, formatMultiple } from '@/lib/format'
import { MULTIPLE_KEYS, type MultipleKey, type MultipleValue, type TTMPeriod, type Warning, } from '@/api/types'

const ROW_LABELS: Record<MultipleKey, string> = {
  ev_revenue: 'EV/Revenue',
  ev_ebitda: 'EV/EBITDA',
  ev_ebit: 'EV/EBIT',
  pe: 'P/E',
  pfcf: 'P/FCF',
  ps: 'P/S',
  pb: 'P/B',
}

// Shared sticky-first-column styling so the label column stays visible on scroll.
const stickyCol = 'sticky left-0 bg-popover shadow-[inset_-1px_0_0_var(--border)] font-medium'

function WarningTooltip({ trigger, warnings }: {
  trigger: React.ReactNode
  warnings: Warning[]
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>{trigger}</TooltipTrigger>
      <TooltipContent className="flex flex-col max-w-xs">
        {warnings.map((w) => (
          <p key={w.code}>{w.message}</p>
        ))}
      </TooltipContent>
    </Tooltip>
  )
}

function MultipleCell({ mv }: { mv: MultipleValue }) {
  const warnings = dedupeWarnings(mv.warnings)
  const hasWarnings = warnings.length > 0
  const isNegative = mv.value !== null && mv.value < 0
  const text = formatMultiple(mv.value)

  const content = (
    <span
      className={cn(
        'tnum',
        mv.value === null && 'text-muted-foreground',
        isNegative && 'text-destructive',
        hasWarnings && 'underline underline-offset-7 decoration-dotted decoration-muted-foreground/50',
      )}
    >
      {text}
    </span>
  )

  if (!hasWarnings) return content

  return (
    <WarningTooltip
      trigger={
        <button className="cursor-default">
          {content}
        </button>
      }
      warnings={warnings}
    />
  )
}

function PeriodWarnings({ period }: { period: TTMPeriod }) {
  const warnings = dedupeWarnings(period.warnings)
  if (warnings.length === 0) return null
  return (
    <WarningTooltip
      trigger={
        <Badge tabIndex={0}>
          {warnings.length} note{warnings.length > 1 ? 's' : ''}
        </Badge>
      }
      warnings={warnings}
    />
  )
}

export function ResultsTable({ periods }: { periods: TTMPeriod[] }) {
  if (periods.length === 0) {
    return (
      <p className="py-20 text-sm text-muted-foreground text-center">
        No trailing-twelve-month periods available for this company.
      </p>
    )
  }

  // Sparkline reads oldest -> newest, the table shows most-recent-first.
  const oldestFirst = [...periods].reverse()

  return (
    <div className="max-w-full w-fit border rounded-md overflow-hidden">
      <Table>
        <TableHeader>
          <TableRow className="hover:bg-transparent">
            <TableHead className={cn(stickyCol)}>
              <div className="relative h-full">
                <svg
                  className="absolute w-full h-full text-border"
                  aria-hidden
                >
                  <line
                    x1="0"
                    y1="0"
                    x2="100%"
                    y2="100%"
                    stroke="currentColor"
                    strokeWidth="1"
                  />
                </svg>
                <span className="eyebrow absolute top-1.5 right-2.5">Period</span>
                <span className="eyebrow absolute bottom-1.5 left-2.5">Multiple</span>
              </div>
            </TableHead>
            {periods.map((p, i) => (
              <TableHead
                key={p.period_end}
                className="px-4"
              >
                <div className="flex flex-col items-end gap-0.75 py-1.5">
                  <span className={cn('eyebrow')}>
                    {String(i + 1).padStart(2, '0')}
                  </span>
                  <span className="tnum text-xs">
                    {p.label}
                  </span>
                  <span className="tnum text-[0.6875rem] text-muted-foreground">
                    filed {formatDate(p.filing_date)}
                  </span>
                  <PeriodWarnings period={p} />
                </div>
              </TableHead>
            ))}
            <TableHead className={cn('border-l')}>
              <div className="relative h-full">
                <svg
                  className="absolute w-full h-full text-border"
                  aria-hidden
                >
                  <line
                    x1="0"
                    y1="100%"
                    x2="100%"
                    y2="0"
                    stroke="currentColor"
                    strokeWidth="1"
                  />
                </svg>
                <span className="eyebrow absolute top-1.5 left-2.5">Period</span>
                <span className="eyebrow absolute bottom-1.5 right-2.5">Trend</span>
              </div>
            </TableHead>
          </TableRow>
        </TableHeader>

        <TableBody>
          {MULTIPLE_KEYS.map((key) => {
            const trend = oldestFirst.map((p) => p.multiples[key].value)
            return (
              <TableRow key={key}>
                <TableCell className={cn(stickyCol, '[tr:hover_&]:bg-current-row')}>{ROW_LABELS[key]}</TableCell>
                {periods.map((p) => (
                  <TableCell
                    key={p.period_end}
                    className="px-4 text-right"
                  >
                    <MultipleCell mv={p.multiples[key]} />
                  </TableCell>
                ))}
                <TableCell className="border-l">
                  <div className="flex justify-center">
                    <Sparkline values={trend} aria-label={`${ROW_LABELS[key]} trend`} />
                  </div>
                </TableCell>
              </TableRow>
            )
          })}
        </TableBody>
      </Table>
      <p className="px-2.5 py-1.5 bg-popover border-t text-[0.6875rem] text-muted-foreground">
        All values are GAAP, computed from reported XBRL with no non-recurring adjustments.
      </p>
    </div>
  )
}
