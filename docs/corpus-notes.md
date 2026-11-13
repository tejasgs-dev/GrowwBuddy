# GrowwBuddy Corpus Notes

> **Facts-only. No investment advice.**

What we learned about the five Groww scheme pages while building Phase 1, and the choices the parser makes as a result. Read this before changing [parse_groww.py](../backend/growwbuddy/ingest/parse_groww.py) or debugging a wrong fact.

Checked on 12 Nov 2026 against the live pages (data as of 11 Nov 2026).

---

## 1. How the pages deliver data

- All five pages return HTTP 200 to a plain `httpx` request with a generic User-Agent. **No JavaScript rendering is needed**, so Playwright isn't used.
- Each page is a Next.js app. The fund's data is embedded as JSON in `<script id="__NEXT_DATA__">` at `props.pageProps.mfServerSideData`.
- The parser reads an **allow-list** of fields from that JSON. It never scrapes visible text, and a page without the JSON is rejected (a bot-check or error page is never indexed).

## 2. Fields used

| Parsed field | Source in `mfServerSideData` | Notes |
|---|---|---|
| `expense_ratio` | `expense_ratio` | Formatted to 2 decimals (`"1.0"` → `1.00%`, as the page shows). Its date is the newest `historic_fund_expense[].as_on_date`. |
| `expense_ratio_base` | `base_expense_ratio` | The TER **excluding** GST and statutory charges; `expense_ratio` is the figure **including** them. Groww's page has shown either one in its "Expense ratio" box (13 Nov 2026: 0.64% for small cap in a signed-in browser, 0.79% in the HTML our fetcher receives), so both are stored, and the section text and answers always give both with labels. Same date as `expense_ratio`. |
| `expense_ratio_history`, `expense_ratio_base_history` | newest `historic_fund_expense[].expense_ratio` / `.base_expense_ratio` | Check-only (Phase 2): second copies of the two figures. If one differs from its headline field, that fact is stored unverified (EC-FCT-10). A base figure above the total flags both. Never shown to users or indexed. |
| `exit_load` | `exit_load` | Kept as written |
| `min_sip`, `min_lumpsum`, `min_additional` | `min_sip_investment`, `min_investment_amount`, `mini_additional_investment` | Formatted as `₹100` with Indian digit grouping |
| `riskometer` | `return_stats[0].risk` | See quirk 1 |
| `benchmark` / `benchmark_short` | `benchmark_name` / `benchmark` | e.g. "NIFTY Midcap 150 Total Return Index" / "NIFTY Midcap 150 TRI" |
| `fund_managers` | `fund_manager_details[].person_name` | See quirk 2 |
| `tax` | `category_info.tax_impact` | See quirk 3 |
| `objective` | `description` | |
| `category` | `category` + `sub_category` | e.g. "Equity – Mid Cap" |
| `launch_date`, `registrar`, `stamp_duty`, `fund_house` | `launch_date`, `registrar_agent`, `stamp_duty`, `fund_house` | |
| `lock_in` | `lock_in.{years,months,days}` | All null on all five pages, so no lock-in is stored |
| page `as_of_date` | `nav_date` | The NAV/data date. The NAV *value* itself is dropped (performance data, EC-INT-06). |

Identity checks before anything is parsed: `search_id` must equal the URL slug, `plan_type` must be `Direct`, `scheme_type` must be `Growth`, and `fund_house` must be HDFC.

**Dropped on purpose:** every return and rank field (`return_stats` except `risk`, `stats`, `simple_return`, `sip_return`), `analysis` (pros/cons text), `peerComparison`, `holdings`, `nav`, `groww_rating`, `aum`, the AMC's contact details, the `funds_managed` lists, and all of `category_info` except `tax_impact`.

## 3. Data quirks found

1. **`nfo_risk` is stale.** It holds the risk level from launch. For Mid Cap it says "Moderately High", while the page shows **Very High**. `return_stats[0].risk` matches the risk pill shown on all five pages, so it's used instead.
2. **The top-level `fund_manager` is stale.** For Large Cap it says "Prashant Jain", while `fund_manager_details` (shown on the page) lists Rahul Baijal and Dhruv Muchhal.
3. **`category_info` belongs to the wrong category.** Its `sub_type` is "Contra" for the Mid, Small, Large and Multi Cap pages. Only `tax_impact` is used. It's displayed on the page under "Tax", and its text matches each fund's type (equity rules for the equity funds, separate rules for the Silver FoF).
4. **The tax text contains percentages next to the word "returns"** ("returns are taxed at 20%"). The Phase 3 "no return figures" validator rule must not block tax rates, or tax answers will always fall back.
5. **The peer list and the "funds managed" lists include the scheme itself.** This matters only for tests that look for other schemes' names.
6. **Launch date is 01 Jan 2013 for Mid, Small and Large Cap.** That's when Direct plans started, not the original launch date of each scheme. It's stored as the page shows it.

**Sections (Phase 2):** each page gives 12 sections. The old `fund_details` section is split into `category`, `launch_date`, `registrar` and `stamp_duty`, and each one still names its scheme.

## 4. Coverage (task 1.9)

Output of `python -m growwbuddy.ingest --report` on 12 Nov 2026:

| Question type (problem statement) | Mid | Small | Large | Silver FoF | Multi |
|---|---|---|---|---|---|
| Expense ratio | yes | yes | yes | yes | yes |
| Exit load | yes | yes | yes | yes | yes |
| Minimum SIP | yes | yes | yes | yes | yes |
| ELSS lock-in | n/a | n/a | n/a | n/a | n/a |
| Riskometer | yes | yes | yes | yes | yes |
| Benchmark | yes | yes | yes | yes | yes |
| Statement / capital gains download | no | no | no | no | no |

The pages also answer minimum lump sum, fund manager, tax on redemption and investment objective for all five schemes.

## 5. Known Limitations (for the README, task 8.4)

- **ELSS lock-in questions can't be answered.** None of the five schemes is an ELSS fund, and no page states a lock-in. These questions get the out-of-scope reply (EC-INT-12).
- **Statement and capital-gains download questions can't be answered.** No scheme page explains how to do this. They get a polite "not found".
- **Only the Direct – Growth plan is covered.** Questions about Regular plans or IDCW options can't be answered (EC-INT-17).
- **Data is only as current as the last ingest.** The footer shows the page's data date.
- **Facts depend on Groww's embedded page data.** If Groww changes its page structure, ingest fails loudly (parser snapshot tests and identity checks) and the previous parsed version stays in use.
