import { CommandEmpty, CommandGroup, CommandItem, CommandList } from '@/components/ui/command'
import type { CompanyCandidate } from '@/api/types'

interface SearchDropdownProps {
  results: CompanyCandidate[]
  isLoading: boolean
  isError: boolean
  open: boolean
  onSelect: (candidate: CompanyCandidate) => void
}

export function SearchDropdown({
  results,
  isLoading,
  isError,
  open,
  onSelect,
}: SearchDropdownProps) {
  if (!open) return null

  return (
    // Prevent the input from blurring when an item is clicked, so focus-gated
    // visibility doesn't hide the list before the selection registers.
    <CommandList onMouseDown={(e) => e.preventDefault()}>
      {isLoading && (
        <div className="px-3 py-6 text-sm text-muted-foreground text-center">Searching...</div>
      )}

      {isError && (
        <div className="px-3 py-6 text-sm text-destructive text-center">Search failed - please retry.</div>
      )}

      {!isLoading && !isError && (
        <>
          <CommandEmpty>No matching companies.</CommandEmpty>
          <CommandGroup>
            {results.map((c) => (
              <CommandItem
                key={c.cik_10}
                value={`${c.ticker} ${c.name} ${c.cik_10}`}
                onSelect={() => onSelect(c)}
              >
                <span className="truncate">{c.name}</span>
                <span className="text-muted-foreground">{c.ticker}</span>
              </CommandItem>
            ))}
          </CommandGroup>
        </>
      )}
    </CommandList>
  )
}
