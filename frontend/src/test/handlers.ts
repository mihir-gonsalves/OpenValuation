import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { appleSearch, financialsByCik } from './fixtures'

export const handlers = [
  http.post('/api/search', async ({ request }) => {
    const { query } = (await request.json()) as { query: string }
    if (!query?.trim()) return HttpResponse.json({ results: [] })
    return HttpResponse.json(appleSearch)
  }),

  http.get('/api/financials/:cik', ({ params }) => {
    const cik = String(params.cik)
    const data = financialsByCik[cik]
    if (!data) {
      return HttpResponse.json(
        { detail: { error: 'edgar_not_found', message: 'No SEC filer matches that CIK.' } },
        { status: 404 },
      )
    }
    return HttpResponse.json(data)
  }),

  http.get('/api/export/:cik', () => {
    return HttpResponse.json(
      { detail: { error: 'not_implemented', message: 'Excel export is planned for Phase 4.' } },
      { status: 501 },
    )
  }),
]

export const server = setupServer(...handlers)
