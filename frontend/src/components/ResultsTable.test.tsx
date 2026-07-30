import { describe, expect, it } from 'vitest'
import { screen } from '@testing-library/react'
import { ResultsTable } from './ResultsTable'
import { renderWithProviders } from '@/test/utils'
import { aaplFinancials, msftFinancials } from '@/test/fixtures'

describe('ResultsTable', () => {
  it('renders all seven multiple rows', () => {
    renderWithProviders(<ResultsTable periods={aaplFinancials.periods} />)
    for (const label of ['P/E', 'EV/EBITDA', 'EV/EBIT', 'EV/Revenue', 'P/S', 'P/B', 'P/FCF']) {
      expect(screen.getByText(label)).toBeInTheDocument()
    }
  })

  it('formats multiple values with an x suffix', () => {
    renderWithProviders(<ResultsTable periods={aaplFinancials.periods} />)
    expect(screen.getByText('33.1')).toBeInTheDocument()
  })

  it('renders a dash for an N/A multiple (MSFT EV/EBITDA)', () => {
    renderWithProviders(<ResultsTable periods={msftFinancials.periods} />)
    // MSFT has EV/EBITDA = null for all periods.
    expect(screen.getAllByText('—').length).toBeGreaterThan(0)
  })

  it('surfaces a per-period notes badge excluding warnings already shown on a cell', () => {
    // MSFT's period has fallback_tag (also on the pe cell -> filtered
    // from the badge) and ttm_annualized (period-level -> counted).
    renderWithProviders(<ResultsTable periods={msftFinancials.periods} />)
    expect(screen.getByText(/1 note/)).toBeInTheDocument()
  })

  it('renders no badge when all period warnings are duplicated on cells', () => {
    const period = msftFinancials.periods[0]
    const cellOnly = {
      ...period,
      warnings: period.multiples.pe.warnings,
    }
    renderWithProviders(<ResultsTable periods={[cellOnly]} />)
    expect(screen.queryByText(/note/)).not.toBeInTheDocument()
  })

  it('shows an empty message when there are no periods', () => {
    renderWithProviders(<ResultsTable periods={[]} />)
    expect(screen.getByText(/no trailing-twelve-month periods available/i)).toBeInTheDocument()
  })
})
