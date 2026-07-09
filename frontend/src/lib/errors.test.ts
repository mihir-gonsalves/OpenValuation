import { describe, expect, it } from 'vitest'
import { errorCopy } from './errors'

describe('errorCopy', () => {
  it('maps known error codes to friendly copy', () => {
    expect(errorCopy('edgar_not_found').title).toBe('No SEC filer found')
  })
  it('treats http_error as a likely cold start', () => {
    expect(errorCopy('http_error').body).toMatch(/waking up/i)
  })
})
