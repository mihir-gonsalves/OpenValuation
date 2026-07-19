import { ArrowLeft } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { Button } from '@/components/ui/button'

/** One question + its plain-language answer. */
function Entry({ q, children }: { q: string; children: React.ReactNode }) {
  return (
    <section className="flex flex-col gap-2">
      <h2 className="font-display font-semibold text-base tracking-tight">{q}</h2>
      <div className="flex flex-col gap-2 text-sm text-muted-foreground leading-relaxed">
        {children}
      </div>
    </section>
  )
}

/**
 * A plain-English glossary for people new to the terms this tool uses - cold
 * starts, TTM, and what each multiple actually measures. Reachable at /learn
 * (e.g. from the cold-start notice).
 */
export function Learn() {
  const navigate = useNavigate()

  // Go back where the user came from, fall back to home if /learn was opened
  // directly (a shared link, a new tab) and there's no history to pop.
  function goBack() {
    if (window.history.length > 1) navigate(-1)
    else navigate('/')
  }

  return (
    <div className="flex flex-col mx-auto max-w-3xl p-6 gap-12">
      <div>
        <Button onClick={goBack} aria-label="Go back to the previous page">
          <ArrowLeft />
          Back
        </Button>
      </div>

      <header className="flex flex-col gap-2">
        <h1 className="font-display font-semibold text-3xl tracking-tight">
          Questions & Terminology
        </h1>
        <p className="text-sm text-muted-foreground leading-relaxed">
          A plain-language guide to the words and numbers you'll see on this site.
        </p>
      </header>

      <div className="flex flex-col gap-10">
        <Entry q="What is OpenValuation?">
          <p>
            OpenValuation is a free tool that calculates how expensive a U.S. public company's
            stock is relative to its own business - including its earnings, sales, cash
            flow, and so on. The tool pulls the numbers straight from the financial
            reports companies file with the U.S. Securities and Exchange Commission (SEC), so
            there's nothing to sign up for and no opinions baked in.
          </p>
        </Entry>

        <Entry q='Why does it say "Waking the server"? (Cold starts)'>
          <p>
            OpenValuation is built entirely on free-tier infrastructure. To keep the site free,
            one tradeoff accepted is that the server (the part that does the math) goes to sleep after
            15 minutes of inactivity. The first request made after it falls asleep has to wake it up
            which takes a few extra seconds. This one-time delay is called a <strong>cold start</strong>.
          </p>
          <p>
            You don't need to do anything - the notice just explains why the
            very first lookup can feel slow. Once it's awake, everything is
            quick.
          </p>
          <p>
            <a
              href="https://github.com/mihir-gonsalves/OpenValuation/blob/main/DESIGN.md"
              target="_blank"
              rel="noopener noreferrer"
              className="underline"
            >
              Read more about all engineering architecture decisions, tradeoffs, and their rationale
            </a>
            .
          </p>
        </Entry>

        <Entry q="What does TTM (trailing twelve months) mean?">
          <p>
            <strong>TTM</strong> stands for <em>trailing twelve months</em> - the
            most recent 12 months of business, ending at the company's latest
            report. Companies file reports every three months, so TTM adds up the four
            most recent quarters to give you an up-to-date, full-year picture without requiring
            the official annual report.
          </p>
        </Entry>

        <Entry q="What is a valuation multiple?">
          <p>
            A <strong>multiple</strong> compares a company's price to something
            it actually produces - like its profit or its sales. It answers
            "how much am I paying for each dollar of X?" For example, a P/E of 20 means
            investors are paying $20 for every $1 the company earns in a year.
          </p>
          <p>
            Multiples are most useful when comparing similar companies, or the same
            company over time. On its own a multiple is neither good nor bad. A higher
            multiple can mean the market expects more growth or that the stock is pricey.
          </p>
        </Entry>

        <Entry q="What is Enterprise Value (EV)?">
          <p>
            <strong>Enterprise value</strong> is the price to buy the whole
            company outright: its stock-market value, plus the debt you'd inherit,
            minus the cash already in its bank account (which you'd get to keep).
            It's a fuller "sticker price" than share price alone, which is why the
            EV multiples use it.
          </p>
        </Entry>

        <Entry q="What do the specific multiples mean?">
          <p>
            OpenValuation shows seven multiples:
          </p>
          <ul className="flex flex-col pl-4.5 gap-2 list-disc marker:text-border">
            <li>
              <strong>EV/Revenue</strong> - Enterprise value per dollar of sales.
              Like P/S (below), but includes debt too.
            </li>
            <li>
              <strong>EV/EBITDA</strong> - Enterprise value per dollar of {" "}
              <em>EBITDA</em> (earnings before interest, taxes, and the paper
              cost of aging equipment). A measure commonly used to compare core
              profitability across companies with different debt and tax situations.
            </li>
            <li>
              <strong>EV/EBIT</strong> - Same idea as EV/EBITDA, but using {" "}
              <em>EBIT</em> (earnings before interest and taxes), which does
              subtract that equipment cost.
            </li>
            <li>
              <strong>P/E</strong> - Price to Earnings. Price per dollar of
              profit. The most common yardstick for "how expensive" a stock is.
            </li>
            <li>
              <strong>P/FCF</strong> - Price to Free Cash Flow. Price per dollar
              of actual cash left over after a company pays its operating expenses
              and reinvests in the business.
            </li>
            <li>
              <strong>P/S</strong> - Price to Sales. Price per dollar of revenue.
              Handy for companies that aren't profitable yet.
            </li>
            <li>
              <strong>P/B</strong> - Price to Book. Price per dollar of
              <em> book value</em>, which is roughly what the company would be
              worth on paper if it sold everything and paid off its debts.
            </li>
          </ul>
        </Entry>

        <Entry q="What do the notes and dotted-underlined numbers mean?">
          <p>
            Some numbers carry a small note (hover to read it). These flag
            things worth knowing - for instance a value estimated from partial
            data, or a figure that's negative or unusually small so the multiple
            may be misleading. They aren't errors, they're just honesty about the
            data. A dash simply means the company didn't report what's needed to
            compute that value.
          </p>
        </Entry>

        <Entry q="Where does the data come from?">
          <p>
            Financials come from companies' official filings to the SEC
            (specifically, XBRL data available through the {" "}
            <a
              href="https://www.sec.gov/search-filings"
              target="_blank"
              rel="noopener noreferrer"
              className="underline"
            >
              SEC's EDGAR
            </a>
            {" "} system), and stock prices come from Yahoo Finance. Every number
            is as-reported under U.S. accounting rules (GAAP) - OpenValuation does
            no smoothing or adjusting of its own. It serves for learning and research
            purposes only, not financial advice.
          </p>
        </Entry>
      </div>

      <div className="flex mx-auto pt-6 text-xs text-muted-foreground leading-relaxed">
        <p>
          Made with &#10084;&#65039; by {" "}
          <a
            href="https://mihirgonsalves.com/"
            target="_blank"
            rel="noopener noreferrer"
            className="underline"
          >
            Mihir Gonsalves
          </a>
        </p>
      </div>
    </div>
  )
}
