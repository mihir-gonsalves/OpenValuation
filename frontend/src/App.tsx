import { Link, useSearchParams } from 'react-router-dom'
import type { CompanyCandidate } from '@/api/types'
import { useFinancials } from '@/api/queries'
import { SearchBar } from '@/components/SearchBar'
import { CompanyHeader } from '@/components/CompanyHeader'
import { ResultsTable } from '@/components/ResultsTable'
import { AuditPanel } from '@/components/AuditPanel'
import { CopyLinkButton } from '@/components/CopyLinkButton'
import { DownloadButton } from '@/components/DownloadButton'
import { LoadingState } from '@/components/LoadingState'
import { ErrorMessage } from '@/components/ErrorMessage'
import { ExampleChips } from '@/components/ExampleChips'
import { WarmingNotice } from '@/components/WarmingNotice'
import { useServerReady } from '@/lib/useServerReady'

function Wordmark() {
  return (
    <Link to="/" className="font-display font-semibold text-xl tracking-tight">
      OpenValuation
    </Link>
  )
}

export default function App() {
  const [searchParams, setSearchParams] = useSearchParams()
  const cik = searchParams.get('cik')

  // URL is the single source of truth: selecting a company sets ?cik, and the
  // financials query keys off it - so a shared /?cik=... link auto-loads.
  const { ready, warming } = useServerReady()
  const { data, isLoading, isError, error, refetch } = useFinancials(cik)

  function selectCompany(c: CompanyCandidate) {
    setSearchParams({ cik: c.cik_10 })
  }

  // Empty state: a masthead, the search, and a status zone that adapts to the
  // server's readiness (cold-start notice, or quick-start chips once warm).
  if (!cik) {
    return (
      <main className="flex flex-col min-h-svh justify-center items-center text-center gap-6">
        <h1 className="font-display font-semibold text-4xl tracking-tight">
          OpenValuation
        </h1>
        <div className="rule-engraved w-20 -m-3" />
        <p className="max-w-md text-sm text-muted-foreground leading-relaxed">
          Trailing-twelve-month multiples for any (GAAP-compliant) U.S. public company,
          computed straight from SEC EDGAR XBRL filings.
        </p>

        <SearchBar onSelect={selectCompany} variant="hero" />

        {warming ? (<WarmingNotice />) : ready ? (<ExampleChips onPick={(cik10) => setSearchParams({ cik: cik10 })} />) : null}
      </main>
    )
  }

  return (
    <div className="flex flex-col mx-auto max-w-5xl px-4">
      <header className="flex items-center justify-between py-3 border-b">
        <Wordmark />
        <div className="flex items-center gap-3">
          <SearchBar onSelect={selectCompany} variant="compact" />
          {data && (
            <>
              <CopyLinkButton />
              <DownloadButton cik10={data.company.cik_10} ticker={data.company.ticker} />
            </>
          )}
        </div>
      </header>

      <main className="flex flex-col gap-6 pt-4 pb-6.75">
        {isLoading && <LoadingState />}
        {isError && <ErrorMessage error={error} onRetry={() => refetch()} />}
        {data && (
          <>
            <CompanyHeader company={data.company} dataAsOf={data.data_as_of} />
            <ResultsTable periods={data.periods} />
            <AuditPanel key={data.company.cik_10} periods={data.periods} />
          </>
        )}
      </main>

      <footer className="flex justify-between py-3 border-t text-xs text-muted-foreground">
        <p>Sources: SEC EDGAR (XBRL) and Yahoo Finance (prices).</p>
        <Link to="/learn" className="underline">Learn More</Link>
      </footer>
    </div>
  )
}
