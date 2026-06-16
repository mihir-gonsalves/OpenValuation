# PHASE_3_SPEC.md - Multiples Engine

**Phase:** 3  **Status:** Complete  **Implemented in:** `backend/app/services/multiples.py`

## What this document is

This is the **decision record** for Phase 3 (the multiples engine). It captures
the non-obvious choices made while implementing `services/multiples.py` - the
guard ordering, the warning-routing contract with the router, and the edge cases
the test suite pins down.

It deliberately does **not** restate what is already authoritative elsewhere:

| You want... | Read instead |
|---|---|
| The seven formulas, the EV definition, EBITDA/FCF construction, the negative/near-zero/negative-book-value rules | `README.md` -> *Multiple Calculation*, *Results Display*; `DESIGN.md` -> *Enterprise Value*, *Multiples* |
| `WarningCode` definitions, the exact `MultipleSet`/`MultipleValue`/`EVComponents` JSON shapes, HTTP error mappings | `PHASE_1_SPEC.md` §1-2 |
| The warning-merge + dedup contract the router owes Phase 3 | `PHASE_1_SPEC.md` §6.2 |
| Field-by-field types of `ExtractedFinancials` / `EVComponents` / `MultipleSet` | `app/models/financials.py` (documented inline) |
| What the upstream `extract_ttm_periods` produces and guarantees | `PHASE_2_SPEC.md` |

The formulas themselves live in `README.md` and `DESIGN.md`. This doc explains
*why the surrounding decisions are the way they are*, not what the formulas are.



## 1. Scope and entry point

Phase 3 turns one `ExtractedFinancials` (one TTM period, Phase 2 output) into a
`MultipleSet` (seven valuation multiples) plus an `EVComponents` breakdown. The
public surface the router depends on is exactly:

```python
def compute_all(financials: ExtractedFinancials) -> tuple[MultipleSet, EVComponents]:
```

`_build_response` in `app/routers/financials.py` already calls this once per
extracted period (the wiring predates Phase 3 - see `PHASE_1_SPEC.md` §6.2). The
seven individual calculators and `compute_enterprise_value` are also public and
independently unit-tested, but `compute_all` is the only entry point the router
uses.

Everything in this module is a **pure function**: `ExtractedFinancials` in, values
and warnings out. No I/O, no `async`, no mutation of the input. Money is always
`Decimal`, the engine never returns a `float`.



## 2. Design decisions and their rationale

These are the choices a reader cannot infer from the formulas alone.

### 2.1 Two universal guards live in one helper, everything else is layered on top

Every multiple shares two rules: *missing operand -> N/A silently*, and
*near-zero denominator -> N/A with `denominator_near_zero`*. Both live in
`_safe_divide(numerator, denominator, denom_name)`. The multiples that need extra
rules (P/B's negative book value, P/FCF's negative FCF, EV/EBITDA's two-input
denominator) apply those rules **first**, then delegate the leftover division to
`_safe_divide`.

**Why a shared helper:** the near-zero threshold (`abs < 0.01`) and the
"missing -> silent N/A" convention must be byte-identical across all seven
multiples. Duplicating the check seven times is how they drift apart. The
`denom_name` argument is what lets one helper still produce a specific message
("Revenue is near zero...", "EBITDA is near zero...").

### 2.2 Missing data is N/A *without* a warning, a fired guard *gets* a warning

A `None` operand returns `(None, [])` - no warning. The reason the operand is
missing was already surfaced upstream: `price_unavailable` for a null price, a
missing-tag `N/A` in the audit panel for a null financial. Re-flagging it on
every dependent multiple would triplicate noise (a null price alone would emit
~6 warnings). By contrast, a guard that *fires on present data* - near-zero
denominator, negative book value - is new information and carries a warning.

This is the cleanest reading of the project's first principle: surface what the
user can't otherwise see, stay quiet about what's already explained.

### 2.3 Market cap gates EV, and EV gates `ev_debt_missing`

`compute_enterprise_value` computes `market_cap = price × shares` first. If
either input is missing, **EV is `None`** and the three EV-based multiples become
N/A. Critically, `ev_debt_missing` is then **suppressed**: the warning means
"EV may be *understated*," and there is no EV to understate when it couldn't be
computed at all. Emitting it alongside a `price_unavailable`-driven N/A would be
misleading. So `ev_debt_missing` fires only when (a) market cap is present *and*
(b) every financial-debt and finance-lease field is `None`.

"Every debt/lease field" is precisely the five EV debt components:
`long_term_debt`, `short_term_borrowings`, `current_portion_lt_debt`,
`finance_lease_current`, `finance_lease_noncurrent`. Minority interest, preferred
stock, and cash are not debt and do not participate in this check.

### 2.4 `ev_debt_missing` rides every EV multiple and is deduplicated downstream

`compute_all` attaches the EV builder's warnings (i.e. `ev_debt_missing` when it
fires) to **each** of EV/EBITDA, EV/EBIT, and EV/Revenue. This is deliberate and
matches the contract in `PHASE_1_SPEC.md` §6.2: the router merges all per-multiple
warnings into `TTMPeriod.warnings` and runs `dedup_warnings`, which collapses the
three copies into one. Attaching it per-multiple (rather than once on the period)
keeps the warning co-located with the multiples it actually explains, while the
router's dedup prevents triplication in the UI/Excel. P/S and P/B and P/FCF do
**not** carry it - they depend on market cap, not on EV's debt components.

### 2.5 Components are stored raw, the EV total applies the zero convention

`EVComponents` preserves each extracted value as-is - `None` where a tag was
absent - so the audit panel and Excel show exactly what was present. The EV
*total*, however, treats every missing component as a zero balance (`_or_zero`).
This is the documented EV rule (missing -> 0), and keeping the two
representations separate means a reader can see "cash was not reported" in the
breakdown without the total silently reading it as anything other than zero.
A consequence worth stating: summing the displayed (non-null) components does not
necessarily reconstruct the EV total when some were absent.

### 2.6 Negative book value and negative FCF are checked *before* the near-zero guard

`README.md`/`DESIGN.md` mandate this ordering for P/B: negative equity is a
*distinct* condition from a tiny denominator and must be diagnosed first. Phase 3
applies the same "more specific condition wins" ordering to P/FCF's negative-FCF
check. The practical effect shows up only at the overlap (a denominator that is
both negative and within `+/-0.01`): there, P/B reports `negative_book_value` and
P/FCF reports `negative_fcf`, rather than `denominator_near_zero`. The
economically meaningful diagnosis is preferred over the numeric one.

### 2.7 EV/EBITDA requires D&A, it never silently degrades to EV/EBIT

When `depreciation_and_amortization` is `None`, EBITDA is not reconstructed and
EV/EBITDA is N/A - never a silent proxy for EV/EBIT (`README.md` -> *EBITDA
Construction Limitation*). There is no warning code for absent D&A, so this is a
plain N/A. EV/EBIT still computes from operating income alone, and the divergence
(or its absence) between the two multiples is itself the signal to the user.

### 2.8 The basic-EPS label is read off the audit trail, not recomputed

P/E's label becomes `P/E (basic)` when the basic-EPS fallback fired. Phase 3 does
not re-derive this - it reads `is_fallback` from the period's `EPS` audit entry
(`_eps_is_basic`). Phase 2 owns both the fallback decision and the
`fallback_eps_basic` warning (`PHASE_2_SPEC.md` §3.6), Phase 3 owns only the label
change. The `eps_diluted` field already holds basic EPS when diluted was absent,
so the division is identical - only the label differs.

### 2.9 Decimal throughout, no rounding

All arithmetic is `Decimal`. Division uses the default 28-significant-digit
context. The engine does **not** round results - display precision is a Phase 4
formatting concern, and rounding here would lose precision the Excel export needs.
The near-zero threshold is the only magic number, and it is a single module-level
constant (`DENOMINATOR_NEAR_ZERO_THRESHOLD`).



## 3. Warning ownership (Phase 3 vs Phase 2)

`PHASE_2_SPEC.md` §5 lists which codes extraction emits. Phase 3 emits exactly the
four codes reserved for it in that document:

**Raised by Phase 3 (`multiples.py`):**
`ev_debt_missing`, `denominator_near_zero`, `negative_book_value`, `negative_fcf`.

**Not raised by Phase 3** (owned by Phase 2 or never raised): everything else,
including `fallback_eps_basic` (Phase 2 raises it, Phase 3 only flips the label),
`price_unavailable` (Phase 2), and `period_mismatch` (never raised - see
`PHASE_2_SPEC.md` §3.3).

Per-period dedup is applied by the **router** (`dedup_warnings`) over the union of
extraction and multiples warnings - Phase 3 does not dedup its own output, it
relies on §2.4's contract.



## 4. Known limitations (accepted in Phase 3)

1. **No `missing_da` warning.** Absent D&A makes EV/EBITDA N/A with no structured
   flag (§2.8). The README documents the limitation, the divergence from EV/EBIT
   is the user-visible signal.
2. **Shares/EPS basis mismatch is unflagged.** Market cap uses point-in-time
   basic shares while P/E uses (weighted-average) diluted EPS. This is a
   documented, intentional inconsistency (`README.md` -> *EPS TTM Bridge*,
   `DESIGN.md` -> *Treasury Stock Method*), not something Phase 3 warns about.
3. **EV understatement from partial debt tagging is unflagged.** `ev_debt_missing`
   fires only when *all* debt/lease tags are absent. A company that tags some but
   not all of its debt produces an EV that may still be understated with no
   warning - the same accepted tradeoff documented for EV in `DESIGN.md`.



## 5. Test strategy

Two layers, mirroring Phase 2, in `tests/test_multiples.py`.

### 5.1 Pure-function tests (synthetic inputs, no fixtures, no I/O)

These are the regression oracles. Each calculator is exercised across its full
matrix:

- **P/E:** positive, negative EPS (negative multiple), basic-fallback label,
  missing price/EPS (silent N/A), near-zero EPS (`denominator_near_zero`), and the
  exact-threshold boundary (`0.01` is *valid* - the guard is strict `<`).
- **EV/EBITDA:** the bridge, missing D&A (N/A, no proxy), missing operating
  income, negative EBITDA (negative multiple), near-zero EBITDA, missing EV.
- **EV/EBIT, EV/Revenue, P/S:** basic division, negatives where valid, near-zero,
  missing operands.
- **P/B:** basic, negative equity (`negative_book_value`), the negative-AND-
  near-zero overlap (negative wins), near-zero positive equity, missing operands.
- **P/FCF:** basic, negative FCF (`negative_fcf`), zero FCF (near-zero), near-zero
  positive FCF, missing OCF/CapEx.
- **`compute_enterprise_value`:** full composition arithmetic, missing components
  as zero, net-cash company (negative EV), `ev_debt_missing` on/off, and the
  market-cap-gates-EV-and-the-warning rule (§2.3).
- **`compute_all`:** every multiple populated against a hand-computed oracle,
  label read from the audit trail (both directions), the `ev_debt_missing`
  per-EV-multiple attachment + dedup-to-one assertion (§2.4), price-unavailable
  making every multiple N/A, and the Decimal-never-float invariant.

### 5.2 Real-data path (companyfacts -> `extract_ttm_periods` -> `compute_all`)

Three fixtures prove the engine consumes real Phase 2 output, with price mocked
at `price_svc.get_prices` (the batch fetch) so a fixed close drives the math:

| Fixture | What it pins down |
|---|---|
| **AAPL** FY2025 | Market cap, P/E (from the hand-verified TTM EPS 7.46, plain label), P/S, P/B all match `market_cap`/`price` oracles; real debt -> **no** `ev_debt_missing`; EV multiples computable. |
| **SNOW** FY2026 | Negative TTM EPS (-3.95) -> a valid **negative** P/E. |
| **MSFT** FY2025 | Both D&A tags absent -> EV/EBITDA is N/A while EV/EBIT still computes (§2.8) on the one real fixture where `da = None` everywhere. |

The mock target matters: extraction calls `price_svc.get_prices(ticker, dates)`
(a single batch download returning `{date: Decimal | None}`), not the per-date
`get_price`. Patching the wrong one lets real yfinance run and silently nulls
every price-dependent multiple.



## 6. What Phase 4 consumes

Phase 4 (Excel export, `services/workbook.py`, and the React frontend) depends on:

- The `MultipleSet` / `MultipleValue` / `EVComponents` shapes (`PHASE_1_SPEC.md`
  §1.2, `app/models/financials.py`) - already stable and now populated.
- `EVComponents` carrying both the itemised inputs and the `enterprise_value`
  total, so the Excel *Calculations* sheet can write the EV formula as a live sum
  of *Raw Financials* cells rather than a hardcoded number.
- The per-period `warnings` union the router builds (extraction + multiples,
  deduplicated) for the *Summary* sheet and the UI warning chips.

The seven calculators are pure and deterministic, so the Excel *Calculations*
sheet can reproduce them as formulas with no risk of divergence from the backend -
the formulas and these functions implement the same arithmetic.

To run Phase 3 in isolation, reshare `multiples.py`, the `financials`/`errors`
models, `xbrl_warnings.dedup_warnings`, and (for the real-data tests) `xbrl.py`,
`xbrl_maps.py`, plus the AAPL/SNOW/MSFT fixtures.
