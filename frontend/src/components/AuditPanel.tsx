import { Fragment, useState } from 'react'
import { ChevronDown, ChevronRight } from 'lucide-react'
import { Collapsible } from 'radix-ui'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow, } from '@/components/ui/table'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { formatAuditValue, formatCompactCurrency, formatUnit, NA } from '@/lib/format'
import type { EVComponents, TTMPeriod } from '@/api/types'

function EVBreakdown({ ev }: { ev: EVComponents }) {
  const rows: [string, number | null][] = [
    ['\u00A0\u00A0 Market cap', ev.market_cap],
    ['+ Long-term debt', ev.long_term_debt],
    ['+ Short-term borrowings', ev.short_term_borrowings],
    ['+ Current portion LT debt', ev.current_portion_lt_debt],
    ['+ Finance lease (current)', ev.finance_lease_current],
    ['+ Finance lease (non-current)', ev.finance_lease_noncurrent],
    ['+ Minority interest', ev.minority_interest],
    ['+ Preferred stock', ev.preferred_stock],
    ['− Cash', ev.cash],
  ]
  return (
    <div className="px-3.5 pt-2.5 pb-5 border-b">
      <p className="eyebrow mb-1.75">Enterprise Value Buildup</p>
      <dl className="tnum grid grid-cols-[1fr_auto] gap-0.75 text-xs">
        {rows.map(([label, value]) => (
          <Fragment key={label}>
            <dt className="text-muted-foreground">{label}</dt>
            <dd className="text-right">{formatCompactCurrency(value)}</dd>
          </Fragment>
        ))}
        <dt className="font-medium">= Enterprise Value</dt>
        <dd className="text-right font-medium">{formatCompactCurrency(ev.enterprise_value)}</dd>
      </dl>
    </div>
  )
}

/**
 * The "auditable" promise of the product: for the selected period, show how each
 * concept was resolved - which XBRL tag fired, whether it was a fallback, its
 * unit, entity context, and the value used.
 */
export function AuditPanel({ periods }: { periods: TTMPeriod[] }) {
  const [open, setOpen] = useState(false)
  const [selected, setSelected] = useState(0)

  if (periods.length === 0) return null
  const period = periods[selected]

  return (
    <Collapsible.Root
      open={open}
      onOpenChange={setOpen}
      className="bg-popover border rounded-md"
    >
      <Collapsible.Trigger className="flex w-full justify-between items-center p-2.5 border border-transparent rounded-md text-sm font-medium transition-colors hover:cursor-pointer hover:bg-accent hover:border-primary/75 hover:ring-1 hover:ring-ring/50 hover:text-accent-foreground focus-visible:outline-none focus-visible:bg-accent focus-visible:border-primary/75 focus-visible:ring-1 focus-visible:ring-ring/50 focus-visible:text-accent-foreground">
        <span>Input Audit</span>
        {open ? (
          <ChevronDown className="text-muted-foreground size-4" />
        ) : (
          <ChevronRight className="text-muted-foreground size-4" />
        )}
      </Collapsible.Trigger>

      <Collapsible.Content className="rounded-b-md overflow-hidden">
        <div className="flex flex-wrap p-3 gap-2">
          {periods.map((p, i) => (
            <Button
              key={p.period_end}
              onClick={() => setSelected(i)}
              className={cn('tnum min-w-20 justify-center rounded-sm text-xs', i === selected && 'bg-accent border-primary/75')}
            >
              {p.label}
            </Button>
          ))}
        </div>

        <EVBreakdown ev={period.ev_components} />

        <Table>
          <TableHeader>
            <TableRow className="border-dashed hover:bg-transparent">
              <TableHead className="min-w-52 px-3.5 align-middle text-left">Concept</TableHead>
              <TableHead className="px-2.5 align-middle text-left">Reported XBRL Tag</TableHead>
              {/* <TableHead className="px-2.5 align-middle text-left">Context</TableHead> */}
              <TableHead className="px-2.5 align-middle text-right">Unit</TableHead>
              <TableHead className="px-3.5 align-middle text-right">Value</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            <TableRow className="border-transparent">
              <TableCell className="p-3.5 font-medium">Price</TableCell>
              <TableCell className="text-muted-foreground text-xs">
                yfinance &middot; Adjusted Close on Next Trading Day After Filing
              </TableCell>
              <TableCell className="text-muted-foreground text-xs text-right">
                {formatUnit('USD/shares')}
              </TableCell>
              <TableCell className="p-3.5 tnum font-medium text-right">
                {formatAuditValue(period.price, 'USD/shares')}
              </TableCell>
            </TableRow>
            {period.extracted.audit.map((entry) => (
              <TableRow key={entry.concept} className="border-transparent">
                <TableCell className="p-3.5 font-medium">
                  <span className="flex items-center gap-2">
                    {entry.concept}
                    {entry.is_fallback && <Badge variant="neutral">fallback</Badge>}
                  </span>
                </TableCell>
                <TableCell className="text-muted-foreground text-xs">
                  {entry.xbrl_tag ?? NA }
                </TableCell>
                {/* <TableCell className="text-muted-foreground text-xs">
                  {entry.entity_context ?? NA }
                </TableCell> */}
                <TableCell className="text-muted-foreground text-xs text-right">
                  {formatUnit(entry.unit)}
                </TableCell>
                <TableCell className={cn("p-3.5 tnum font-medium text-right", entry.value !== null && entry.value < 0 && 'text-destructive')}>
                  {formatAuditValue(entry.value, entry.unit)}
                </TableCell>
              </TableRow>
            ))}
            {period.extracted.audit.length === 0 && (
              <TableRow>
                <TableCell colSpan={5} className="py-6 text-muted-foreground text-sm text-center">
                  No audit entries for this period.
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </Collapsible.Content>
    </Collapsible.Root>
  )
}
