import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { Badge } from '@/components/ui/badge'
import { formatDateTime } from '@/lib/format'
import type { CompanyMeta } from '@/api/types'

interface CompanyHeaderProps {
  company: CompanyMeta
  dataAsOf: string
}

export function CompanyHeader({ company, dataAsOf }: CompanyHeaderProps) {
  const { name, ticker, exchange, sic, sic_description, is_financial } = company

  return (
    <main className="flex flex-col gap-2 -mb-0.5">
      <div className="flex justify-between items-baseline">
        <h1 className="font-display font-semibold text-[1.625rem]">
          {name}
        </h1>
        <span className="eyebrow text-sm">
          {[ticker, exchange].filter(Boolean).join(' · ')}
        </span>
      </div>

      <div className="flex justify-between items-baseline">
        <div className="flex gap-2 text-xs text-muted-foreground">
          {sic && (
            <span className="tnum">
              SIC {sic}
              {sic_description ? ` · ${sic_description}` : ''}
            </span>
          )}

          {is_financial && (
            <Tooltip>
              <TooltipTrigger asChild>
                <Badge tabIndex={0}>
                  Financial Sector
                </Badge>
              </TooltipTrigger>
              <TooltipContent>
                Conventional debt/equity multiples map poorly onto bank, insurer, and REIT balance sheets. Interpret these multiples with caution.
              </TooltipContent>
            </Tooltip>
          )}
        </div>
        <p className="tnum text-muted-foreground text-xs">
          Data as of {formatDateTime(dataAsOf)}
        </p>
      </div>
    </main>
  )
}
