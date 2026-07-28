import { Download, Loader2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { useExport } from '@/api/queries'

interface DownloadButtonProps {
  cik10: string
  ticker: string | null
}

/**
 * Triggers the Excel export.
 */
export function DownloadButton({ cik10, ticker }: DownloadButtonProps) {
  const exportMutation = useExport()
  const filename = `OpenValuation_${ticker ?? cik10}.xlsx`

  const button = (
    <Button
      disabled={exportMutation.isPending}
      onClick={() => exportMutation.mutate({ cik10, filename })}
      className="h-9.5 bg-primary border-primary text-primary-foreground hover:bg-primary/90 hover:text-primary-foreground focus-visible:bg-primary/90 focus-visible:text-primary-foreground"
    >
      {exportMutation.isPending ? <Loader2 className="animate-spin" /> : <Download />} Excel
    </Button>
  )

  if (!exportMutation.isError) return button

  return (
    <Tooltip defaultOpen>
      <TooltipTrigger asChild>{button}</TooltipTrigger>
      <TooltipContent className="max-w-xs">{exportMutation.error.message}</TooltipContent>
    </Tooltip>
  )
}
