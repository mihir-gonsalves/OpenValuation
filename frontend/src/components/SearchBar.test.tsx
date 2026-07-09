import { describe, expect, it, vi } from 'vitest'
import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { SearchBar } from './SearchBar'
import { renderWithProviders } from '@/test/utils'

describe('SearchBar', () => {
  it('queries on input and lists candidates, then reports the selection', async () => {
    const user = userEvent.setup()
    const onSelect = vi.fn()
    renderWithProviders(<SearchBar onSelect={onSelect} />)

    await user.type(screen.getByPlaceholderText(/search for a company/i), 'apple')

    // Debounced search resolves via MSW to the Apple fixture.
    const item = await screen.findByText('Apple Inc.', undefined, { timeout: 3000 })
    expect(screen.getByText('AAPL')).toBeInTheDocument()

    await user.click(item)
    expect(onSelect).toHaveBeenCalledWith(
      expect.objectContaining({ cik_10: '0000320193', ticker: 'AAPL' }),
    )
  })

  it('shows nothing before the user types', () => {
    renderWithProviders(<SearchBar onSelect={vi.fn()} />)
    expect(screen.queryByText('Apple Inc.')).not.toBeInTheDocument()
  })
})
