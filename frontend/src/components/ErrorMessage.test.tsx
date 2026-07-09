import { describe, expect, it, vi } from 'vitest'
import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ErrorMessage } from './ErrorMessage'
import { renderWithProviders } from '@/test/utils'
import { ApiError } from '@/api/client'

describe('ErrorMessage', () => {
  it('maps a 404 edgar_not_found to friendly copy and hides retry', () => {
    const onRetry = vi.fn()
    renderWithProviders(
      <ErrorMessage error={new ApiError(404, 'edgar_not_found', 'nope')} onRetry={onRetry} />,
    )
    expect(screen.getByText('No SEC filer found')).toBeInTheDocument()
    // 4xx is not retryable.
    expect(screen.queryByRole('button', { name: /try again/i })).not.toBeInTheDocument()
  })

  it('offers retry for a server error', async () => {
    const user = userEvent.setup()
    const onRetry = vi.fn()
    renderWithProviders(
      <ErrorMessage error={new ApiError(503, 'edgar_timeout', 'slow')} onRetry={onRetry} />,
    )
    await user.click(screen.getByRole('button', { name: /try again/i }))
    expect(onRetry).toHaveBeenCalledOnce()
  })
})
