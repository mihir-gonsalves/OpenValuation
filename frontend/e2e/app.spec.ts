import { expect, test } from '@playwright/test'

const SEARCH = {
  results: [{ cik_10: '0000320193', name: 'Apple Inc.', ticker: 'AAPL' }],
}

function multiple(label: string, value: number | null) {
  return { value, label, warnings: [] }
}

const FINANCIALS = {
  company: {
    cik_10: '0000320193',
    name: 'Apple Inc.',
    ticker: 'AAPL',
    sic: '3571',
    sic_description: 'Electronic Computers',
    exchange: 'Nasdaq',
    is_financial: false,
    is_capital_intensive: true,
  },
  periods: [
    {
      filing_date: '2024-11-01',
      period_end: '2024-09-28',
      price: 220,
      multiples: {
        ev_revenue: multiple('EV/Revenue', 7.8),
        ev_ebitda: multiple('EV/EBITDA', 21.4),
        ev_ebit: multiple('EV/EBIT', 25.1),
        pe: multiple('P/E', 33.1),
        pfcf: multiple('P/FCF', 26.3),
        ps: multiple('P/S', 8.5),
        pb: multiple('P/B', 45.2),
      },
      ev_components: {
        market_cap: 3.4e12,
        long_term_debt: null,
        short_term_borrowings: null,
        current_portion_lt_debt: null,
        finance_lease_current: null,
        finance_lease_noncurrent: null,
        minority_interest: null,
        preferred_stock: null,
        cash: null,
        enterprise_value: 3.49e12,
      },
      extracted: {
        filing_date: '2024-11-01',
        period_end: '2024-09-28',
        price: 220,
        shares_outstanding: 1.52e10,
        eps_diluted: 6.6,
        revenue: 3.9e11,
        operating_income: null,
        depreciation_and_amortization: null,
        net_income: null,
        operating_cash_flow: null,
        capex: null,
        total_assets: null,
        stockholders_equity: null,
        long_term_debt: null,
        short_term_borrowings: null,
        current_portion_lt_debt: null,
        finance_lease_current: null,
        finance_lease_noncurrent: null,
        minority_interest: null,
        preferred_stock: null,
        cash: null,
        audit: [
          {
            concept: 'Revenue',
            xbrl_tag: 'RevenueFromContractWithCustomerExcludingAssessedTax',
            is_fallback: false,
            unit: 'USD',
            entity_context: 'consolidated',
            value: 3.9e11,
          },
        ],
        warnings: [],
      },
      warnings: [],
    },
  ],
  cached_at: '2026-06-16T12:00:00Z',
  data_as_of: '2026-06-16T12:01:00Z',
}

test('search a company and view its multiples', async ({ page }) => {
  await page.route('**/health', (route) => route.fulfill({ json: { status: 'ok' } }))
  await page.route('**/api/search', (route) =>
    route.fulfill({ json: SEARCH }),
  )
  await page.route('**/api/financials/**', (route) =>
    route.fulfill({ json: FINANCIALS }),
  )

  await page.goto('/')
  await page.getByPlaceholder(/search a company/i).fill('apple')
  await page.getByText('Apple Inc.').click()

  // URL reflects the selection (shareable link).
  await expect(page).toHaveURL(/cik=0000320193/)

  // Company header + the multiples table render.
  await expect(page.getByRole('heading', { name: 'Apple Inc.' })).toBeVisible()
  await expect(page.getByText('P/E', { exact: true })).toBeVisible()
  await expect(page.getByText('33.1')).toBeVisible()

  // Audit panel expands.
  await page.getByRole('button', { name: 'Input audit' }).click()
  await expect(page.getByText('RevenueFromContractWithCustomerExcludingAssessedTax')).toBeVisible()

  // Actions present.
  await expect(page.getByRole('button', { name: /copy link/i })).toBeVisible()
})

test('deep link with ?cik= auto-loads the company', async ({ page }) => {
  await page.route('**/api/financials/**', (route) => route.fulfill({ json: FINANCIALS }))
  await page.goto('/?cik=0000320193')
  await expect(page.getByRole('heading', { name: 'Apple Inc.' })).toBeVisible()
  await expect(page.getByText('33.1')).toBeVisible()
})
