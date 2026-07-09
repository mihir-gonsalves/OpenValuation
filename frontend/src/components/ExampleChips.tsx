import { Button } from '@/components/ui/button'

/**
 * Quick-start chips for the landing page. CIKs are stable SEC identifiers, so
 * hardcoding a few well-known filers lets a first-time visitor load a real
 * result in one click - no typing, and a gentle hint at what the tool returns.
 */
const EXAMPLES: { ticker: string; cik10: string }[] = [
  { ticker: 'AAPL', cik10: '0000320193' },
  { ticker: 'MSFT', cik10: '0000789019' },
  { ticker: 'NVDA', cik10: '0001045810' },
  { ticker: 'TSLA', cik10: '0001318605' },
]

export function ExampleChips({ onPick }: { onPick: (cik10: string) => void }) {
  return (
    <div className="flex items-center gap-2">
      <span className="eyebrow">Try:</span>
      {EXAMPLES.map(({ ticker, cik10 }) => (
        <Button
          key={cik10}
          onClick={() => onPick(cik10)}
          className="text-xs"
        >
          {ticker}
        </Button>
      ))}
    </div>
  )
}
