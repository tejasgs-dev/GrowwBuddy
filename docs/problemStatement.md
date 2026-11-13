# Problem Statement: Mutual Fund FAQ Assistant (Facts-Only Q&A)

> **Facts-only. No investment advice.**

## Overview

GrowwBuddy is a **facts-only FAQ assistant** for mutual fund schemes, using Groww as the reference product context. It answers objective, verifiable questions about mutual funds by retrieving information only from official public sources: AMC (Asset Management Company) websites, AMFI and SEBI.

The assistant must **never** give investment advice, opinions or recommendations. Every response carries one clear source link and follows the rules below on clarity, accuracy and compliance.

**Guiding principle:** accuracy over intelligence. Users should get only verified, source-backed information, with no advisory bias and no speculation.

## Objective

Design and build a lightweight **Retrieval-Augmented Generation (RAG)** assistant that:

- Answers factual questions about mutual fund schemes
- Draws on a curated corpus of official documents
- Gives short, source-backed answers

## Target Users

- Retail investors comparing mutual fund schemes
- Customer support and content teams handling repetitive mutual fund questions

## Scope of Work

### 1. Corpus Definition

- **AMC:** HDFC Mutual Fund
- **Schemes (Groww reference pages):**

| Scheme | Category | Reference URL |
|---|---|---|
| HDFC Mid Cap Fund – Direct Growth | Mid Cap | https://groww.in/mutual-funds/hdfc-mid-cap-fund-direct-growth |
| HDFC Small Cap Fund – Direct Growth | Small Cap | https://groww.in/mutual-funds/hdfc-small-cap-fund-direct-growth |
| HDFC Large Cap Fund – Direct Growth | Large Cap | https://groww.in/mutual-funds/hdfc-large-cap-fund-direct-growth |
| HDFC Silver ETF FoF – Direct Growth | Fund of Funds (Silver ETF) | https://groww.in/mutual-funds/hdfc-silver-etf-fof-direct-growth |
| HDFC Multi Cap Fund – Direct Growth | Multi Cap | https://groww.in/mutual-funds/hdfc-multi-cap-fund-direct-growth |

### 2. FAQ Assistant Requirements

**Supported factual questions include:**

- Expense ratio of a scheme
- Exit load details
- Minimum SIP amount
- ELSS lock-in period
- Riskometer classification
- Benchmark index
- How to download statements or capital gains reports

**Every answer must:**

- Be **at most 3 sentences**
- Include **exactly one** citation link
- End with the footer: `Last updated from sources: <date>`

### 3. Refusal Handling

The assistant must refuse non-factual or advisory questions, for example:

- "Should I invest in this fund?"
- "Which fund is better?"

**A refusal must:**

- Be polite and clearly worded
- Restate that the assistant answers factual questions only
- Give a relevant educational link (e.g. an AMFI or SEBI investor-education page)

### 4. User Interface (Minimal)

The interface must include:

- A welcome message
- Three example questions
- A visible disclaimer: **"Facts-only. No investment advice."**
- Support for **multiple independent chat threads** running at the same time

## Constraints

### Data and Sources

- Use only official public sources (AMC, AMFI, SEBI)
- Do not use third-party blogs or aggregator websites

### Privacy and Security

Do **not** collect, store or process:

- PAN or Aadhaar numbers
- Account numbers
- OTPs
- Email addresses or phone numbers

### Content Restrictions

- No investment advice or recommendations
- No performance comparisons or return calculations
- For performance questions, give only a link to the official factsheet

### Transparency

- Answers must be short, factual and verifiable
- Every answer must include a source link and a last-updated date

## Expected Deliverables

1. **README** covering:
   - Setup instructions
   - Selected AMC and schemes
   - Architecture overview (RAG approach)
   - Known limitations
2. **Disclaimer snippet:** "Facts-only. No investment advice."
3. **Multi-thread RAG chatbot** that handles several independent conversations at once

## Success Criteria

- [ ] Factual mutual fund information is retrieved accurately
- [ ] Answers stay strictly factual
- [ ] Every answer carries a valid source citation
- [ ] Advisory questions are refused properly
- [ ] The interface is clean, minimal and easy to use

## Open Questions / Notes

- **Source conflict:** the corpus lists Groww scheme pages, but the constraints forbid aggregator sites and require official AMC/AMFI/SEBI sources. One possible approach: use the Groww pages only to identify the schemes, then build the corpus and citations from HDFC Mutual Fund's official documents (SID, KIM, factsheets), AMFI and SEBI. This should be decided before ingestion starts.
- **ELSS:** none of the five listed schemes is an ELSS fund, so ELSS lock-in questions will need a general AMFI/SEBI source, or the corpus will need an HDFC ELSS scheme added.
