# PHASE_3_SPEC.md

**Scope:** Multiples Engine  
**Status:** Complete  
**Implemented in:** `backend/app/services/multiples.py`

## What this document is

The decision record for the multiples engine. It captures the guard ordering, the warning-routing contract with the router, and the one test gotcha that costs an afternoon if missed.

It does not restate what is authoritative elsewhere:

| You want... | Read instead |
|---|---|
| The seven formulas, the EV definition, EBITDA and FCF construction, the guard rules | `README.md`, `DESIGN.md` |
| Shapes of `MultipleSet` / `MultipleValue` / `EVComponents` | `app/models/financials.py` |
| The warning merge and dedup the router owes this phase | `PHASE_1_SPEC.md` §6 |

The entry point is `compute_all(financials: ExtractedFinancials) -> tuple[MultipleSet, EVComponents]`. The seven calculators and `compute_enterprise_value` are public and independently unit-tested, but `compute_all` is the only one the router calls. Every function in the module is pure: no I/O, no `async`, no mutation of the input, and money is always `Decimal`.

## 1. Design decisions

### 1.1 The two universal guards live in one helper

Every multiple shares "missing operand becomes silent N/A" and "near-zero denominator becomes N/A with a warning". Both live in `_safe_divide(numerator, denominator, denom_name)`. The three multiples with extra rules (P/B's negative equity, P/FCF's negative FCF, EV/EBITDA's two-input denominator) apply those first, then delegate the division.

The threshold and the silent-N/A convention must be byte-identical across all seven. Duplicating the check seven times is how they drift apart. The `denom_name` argument is what lets one helper still say "Revenue is near zero" rather than something generic.

### 1.2 Missing data is N/A without a warning, a fired guard gets one

A `None` operand returns `(None, [])`. The reason it is missing was already surfaced upstream, as `price_unavailable` or as an N/A row in the audit panel. Re-flagging it on every dependent multiple would turn one null price into roughly six warnings. A guard that fires on data that is actually present, like a near-zero denominator or negative equity, is new information and warns.

### 1.3 Market cap gates EV, and EV gates `ev_debt_missing`

If price or shares is missing, EV is `None` and the three EV multiples are N/A. `ev_debt_missing` is then suppressed. The warning means "EV may be understated", and there is no EV to understate when it could not be computed at all. It fires only when market cap is present and all five debt and lease components are `None`: `long_term_debt`, `short_term_borrowings`, `current_portion_lt_debt`, `finance_lease_current`, `finance_lease_noncurrent`. Minority interest, preferred stock, and cash are not debt and do not participate.

### 1.4 `ev_debt_missing` rides every EV multiple and is deduplicated downstream

`compute_all` attaches it to each of EV/Revenue, EV/EBITDA, and EV/EBIT. This keeps the warning co-located with the multiples it explains, and the router's dedup collapses the three copies into one before the UI or Excel sees them. P/S, P/B, and P/FCF do not carry it, since they depend on market cap rather than on EV's debt components.

### 1.5 Components are stored raw, the total applies the zero convention

`EVComponents` preserves each extracted value as-is, `None` where the tag was absent, so the audit panel shows exactly what was reported. The EV total treats missing components as zero, via `_or_zero`. Keeping the two representations separate lets a reader see "cash was not reported" without the total reading it as anything but zero. The consequence worth stating: summing the displayed non-null components does not necessarily reconstruct the total.

### 1.6 Negative book value and negative FCF are checked before the near-zero guard

The more specific condition wins. This only shows up at the overlap, a denominator that is both negative and within +/-0.01, where P/B reports `negative_book_value` and P/FCF reports `negative_fcf` rather than `denominator_near_zero`. The economically meaningful diagnosis beats the numeric one.

### 1.7 EV/EBITDA requires D&A and never degrades to EV/EBIT

When D&A is `None`, EBITDA is not reconstructed and EV/EBITDA is N/A rather than a silent proxy. There is no warning code for absent D&A, so this is a plain N/A. EV/EBIT still computes, and the divergence between the two, or its absence, is itself the signal.

### 1.8 The basic-EPS label is read off the audit trail

P/E becomes `P/E (basic)` by reading `is_fallback` from the period's EPS audit entry (`_eps_is_basic`), not by re-deriving the fallback. Phase 2 owns the decision and the warning, this phase owns only the label. `eps_diluted` already holds basic EPS when diluted was absent, so the division is identical and only the label differs.

### 1.9 Decimal throughout, no rounding

Division uses the default 28-digit context and results are never rounded. Display precision is a frontend concern, and rounding here would lose precision the Excel export needs. `DENOMINATOR_NEAR_ZERO_THRESHOLD` is the only magic number.

## 2. Warning ownership

This phase raises exactly the four codes `PHASE_2_SPEC.md` reserves for it: `ev_debt_missing`, `denominator_near_zero`, `negative_book_value`, `negative_fcf`. Everything else belongs to extraction, including `fallback_eps_basic`, where Phase 3 only flips the label. The engine does not dedup its own output. It relies on the router doing so over the merged union, which is what §1.4 depends on.

## 3. Known limitations (accepted)

1. **No `missing_da` warning.** Absent D&A makes EV/EBITDA N/A with no structured flag. The divergence from EV/EBIT is the user-visible signal.
2. **Shares and EPS basis mismatch is unflagged.** Market cap uses point-in-time basic shares while P/E uses weighted-average diluted EPS. Documented and intentional, not something this phase warns about.
3. **Partial debt tagging is unflagged.** `ev_debt_missing` fires only when all debt and lease tags are absent, so a company that tags some but not all of its debt produces a possibly understated EV with no warning.

## 4. What the pure-function tests pin

Each calculator is exercised across its full matrix in `tests/test_multiples.py`, with synthetic inputs and no I/O. The cases worth knowing exist:

- The threshold boundary. A denominator of exactly `0.01` is **valid**, since the guard is a strict `<`.
- Valid negative multiples, from negative EPS, negative EBITDA, and a net-cash company's negative EV, all of which must survive rather than become N/A.
- Both directions of the P/E label, read from the audit trail.
- The `ev_debt_missing` attachment to all three EV multiples together with the assertion that dedup collapses it to one.
- A missing price nulling every multiple, and the Decimal-never-float invariant.

## 5. The test gotcha

Real-data tests patch `price_svc.get_prices(ticker, dates)`, the single batch download that extraction actually calls, not the per-date `get_price`. Patching the wrong one lets real yfinance run and silently nulls every price-dependent multiple, so the assertions still "pass" against a set of N/As.

| Fixture | What it pins |
|---|---|
| AAPL FY2025 | The populated path: P/E built on the hand-verified TTM EPS of 7.46 with the plain label, and the absence of `ev_debt_missing` against real debt. |
| SNOW FY2026 | A valid negative P/E from TTM EPS of -3.95. |
| MSFT FY2025 | EV/EBITDA N/A while EV/EBIT computes, on the one real fixture with no D&A. |
