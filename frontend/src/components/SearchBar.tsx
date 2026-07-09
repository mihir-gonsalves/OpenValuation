import { useState } from 'react'
import { Command, CommandInput } from '@/components/ui/command'
import { SearchDropdown } from '@/components/SearchDropdown'
import { useSearch } from '@/api/queries'
import { useDebounce } from '@/lib/useDebounce'
import { cn } from '@/lib/utils'
import type { CompanyCandidate } from '@/api/types'

// "hero" = large empty-state search, "compact" = top-bar search.
const VARIANT_STYLES = {
  hero: { command: 'w-150', input: 'h-12' },
  compact: { command: 'w-75', input: 'h-9' },
}

interface SearchBarProps {
  onSelect: (candidate: CompanyCandidate) => void
  variant?: 'hero' | 'compact'
  className?: string
}

export function SearchBar({ onSelect, variant = 'hero', className }: SearchBarProps) {
  const [query, setQuery] = useState('')
  const [focused, setFocused] = useState(false)
  const debounced = useDebounce(query, 250)

  // Only show the dropdown while the input is focused and there's a query.
  const open = focused && query.trim().length >= 1

  const { data, isLoading, isError } = useSearch(debounced)
  const results = data?.results ?? []

  function handleSelect(candidate: CompanyCandidate) {
    setQuery('')
    onSelect(candidate)
  }

  const styles = VARIANT_STYLES[variant]

  return (
    // shouldFilter=false: results are filtered server-side, cmdk only handles keyboard navigation and selection over the rendered items.
    <Command shouldFilter={false} className={cn(styles.command, className)}>
      <CommandInput
        value={query}
        onValueChange={setQuery}
        placeholder="Search for a company or ticker..."
        className={styles.input}
        onFocus={() => setFocused(true)}
        onBlur={() => setFocused(false)}
      />
      <SearchDropdown
        results={results}
        isLoading={isLoading}
        isError={isError}
        open={open}
        onSelect={handleSelect}
      />
    </Command>
  )
}
