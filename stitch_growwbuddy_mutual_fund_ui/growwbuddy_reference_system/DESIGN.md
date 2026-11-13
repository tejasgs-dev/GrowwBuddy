---
name: GrowwBuddy Reference System
colors:
  surface: '#111317'
  surface-dim: '#111317'
  surface-bright: '#37393e'
  surface-container-lowest: '#0c0e12'
  surface-container-low: '#1a1c20'
  surface-container: '#1e2024'
  surface-container-high: '#282a2e'
  surface-container-highest: '#333539'
  on-surface: '#e2e2e8'
  on-surface-variant: '#c6c5d5'
  inverse-surface: '#e2e2e8'
  inverse-on-surface: '#2f3035'
  outline: '#8f8f9f'
  outline-variant: '#454653'
  surface-tint: '#bcc3ff'
  primary: '#bcc3ff'
  on-primary: '#041c94'
  primary-container: '#7c8cff'
  on-primary-container: '#001892'
  inverse-primary: '#4353c3'
  secondary: '#bec7d4'
  on-secondary: '#28313b'
  secondary-container: '#414a54'
  on-secondary-container: '#b0b9c5'
  tertiary: '#fcba5a'
  on-tertiary: '#452b00'
  tertiary-container: '#c58a2e'
  on-tertiary-container: '#422900'
  error: '#ffb4ab'
  on-error: '#690005'
  error-container: '#93000a'
  on-error-container: '#ffdad6'
  primary-fixed: '#dfe0ff'
  primary-fixed-dim: '#bcc3ff'
  on-primary-fixed: '#000c60'
  on-primary-fixed-variant: '#2839aa'
  secondary-fixed: '#dae3f0'
  secondary-fixed-dim: '#bec7d4'
  on-secondary-fixed: '#141c25'
  on-secondary-fixed-variant: '#3f4852'
  tertiary-fixed: '#ffddb3'
  tertiary-fixed-dim: '#fcba5a'
  on-tertiary-fixed: '#291800'
  on-tertiary-fixed-variant: '#633f00'
  background: '#111317'
  on-background: '#e2e2e8'
  surface-variant: '#333539'
typography:
  headline-xl:
    fontFamily: Plus Jakarta Sans
    fontSize: 32px
    fontWeight: '700'
    lineHeight: 40px
    letterSpacing: -0.02em
  headline-xl-mobile:
    fontFamily: Plus Jakarta Sans
    fontSize: 26px
    fontWeight: '700'
    lineHeight: 34px
    letterSpacing: -0.015em
  headline-lg:
    fontFamily: Plus Jakarta Sans
    fontSize: 24px
    fontWeight: '600'
    lineHeight: 32px
    letterSpacing: -0.015em
  headline-lg-mobile:
    fontFamily: Plus Jakarta Sans
    fontSize: 20px
    fontWeight: '600'
    lineHeight: 28px
    letterSpacing: -0.01em
  headline-md:
    fontFamily: Plus Jakarta Sans
    fontSize: 18px
    fontWeight: '600'
    lineHeight: 26px
    letterSpacing: -0.01em
  body-lg:
    fontFamily: Inter
    fontSize: 16px
    fontWeight: '400'
    lineHeight: 26px
    letterSpacing: -0.005em
  body-md:
    fontFamily: Inter
    fontSize: 15px
    fontWeight: '400'
    lineHeight: 24px
    letterSpacing: 0em
  body-sm:
    fontFamily: Inter
    fontSize: 13px
    fontWeight: '400'
    lineHeight: 20px
    letterSpacing: 0em
  label-lg:
    fontFamily: Inter
    fontSize: 14px
    fontWeight: '600'
    lineHeight: 20px
    letterSpacing: 0.01em
  label-md:
    fontFamily: Inter
    fontSize: 12px
    fontWeight: '500'
    lineHeight: 16px
    letterSpacing: 0.02em
  label-sm:
    fontFamily: Inter
    fontSize: 11px
    fontWeight: '600'
    lineHeight: 14px
    letterSpacing: 0.04em
  code-sm:
    fontFamily: JetBrains Mono
    fontSize: 13px
    fontWeight: '400'
    lineHeight: 18px
    letterSpacing: 0em
rounded:
  sm: 0.25rem
  DEFAULT: 0.5rem
  md: 0.75rem
  lg: 1rem
  xl: 1.5rem
  full: 9999px
spacing:
  gutter: 1rem
  gutter-desktop: 1.5rem
  margin: 1rem
  margin-tablet: 1.5rem
  margin-desktop: 2.5rem
  space-xs: 0.25rem
  space-sm: 0.5rem
  space-md: 0.75rem
  space-lg: 1rem
  space-xl: 1.5rem
  space-2xl: 2rem
  space-3xl: 3rem
---

## Brand & Style

The design system establishes a quiet, institutional, and factual interface designed specifically for verifiable reference queries across mutual fund schemes. The primary emotional objective is unhurried confidence and forensic clarity—eradicating speculative market hype, aggressive neon indicators, and gamified financial cues.

The aesthetic fuses **Utilitarian Editorialism** with **Dark Modernist Clarity**:
- **Tone:** Objective, disciplined, authoritative, calm.
- **Visual Principle:** High-legibility typographic cadence over decoration. Information hierarchy takes precedence through muted structural layering rather than flashy visual elevation.
- **Target Audience:** Astute retail and institutional investors seeking precise scheme details, expense ratios, portfolio compositions, and risk disclosures without behavioral manipulation.

## Colors

The palette leverages a deep slate-indigo continuum engineered specifically to diminish eye fatigue while ensuring WCAG AAA/AA readability across message streams and tabular data.

### Foundation & Surfaces
- **Canvas Base (`#0F1115`):** Deep charcoal slate backdrop providing absolute quietness.
- **Surface Level 1 (`#171A21`):** Default conversational bubble, bottom app bar, and side panels.
- **Surface Level 2 / Raised Cards (`#1E222B`):** Comparative fund cards, data sheets, and modal sheets.
- **Border / Structural Outline (`#2A2F3A`):** Low-contrast dividers and card perimeters ensuring tactile structure without clutter.

### Typography & Content
- **Text Primary (`#E6E8EC`):** Headings, critical scheme statistics, core response text.
- **Text Secondary (`#9AA3AF`):** Metric labels, timestamps, fund manager metadata.
- **Text Muted (`#6B7280`):** Inactive placeholders, subtle footnote disclosures, helper prompts.

### Functional & Semantic Accents
- **Primary Accent (`#7C8CFF`):** Soft luminous indigo utilized strictly for primary action vectors, user message bubbles, text links, active tabs, and focus halos.
- **Advisory / Restriction (`#F5B454`):** Warm amber for non-advice disclaimers, risk-profile limits, and performance restrictions.
- **PII / Threat Alert (`#F87171`):** Subdued crimson for sensitive identity inputs, data redactions, and error confirmations.
- **System Information (`#60A5FA`):** Technical clarity blue for scheme indexing notices, pipeline status, and sync indicators.
- **Neutral Notice (`#94A3B8`):** Slate for missing data points, archived funds, or unbenchmarked records.

## Typography

The typographic pairing balances structured precision with effortless legibility:
- **Headlines (Plus Jakarta Sans):** Offers crisp, contemporary geometric stability for screen titles, scheme comparisons, and asset allocation section headers.
- **Body & Data (Inter):** Serves factual paragraphs, chatbot dialogue turns, tabular data, and legal disclosures. Set intentionally at 15–16px with generous line spacing (1.6x) to facilitate comfortable reading of numerical disclosures and fund facts.
- **Monospace Code / Data (JetBrains Mono):** Designated for ISIN codes, folio patterns, portfolio percentages, and pure numerical telemetry.

## Layout & Spacing

The system enforces a centered, readable structural layout across single-column conversational views and desktop side-by-side reference surfaces:
- **Mobile (<768px):** Single-column layout with fixed conversational canvas. Messages stack vertically with dynamic spacing; maximum content width is unconstrained within the viewport margins (`1rem`).
- **Tablet (768px–1024px):** Single conversational column bounded to a max-width of 720px to preserve optimal line length (50–70 characters per line).
- **Desktop (>1024px):** Asymmetric split workspace. Left pane (360px fixed) houses the five HDFC scheme anchors, recent query facets, and quick selectors. Right pane (max-width 840px) hosts the active message stream and contextual data drawer.
- **Vertical Rhythm:** Consistent 4px base increment. Chat bubbles use internal `space-md` (`12px`) to `space-lg` (`16px`) padding, while grouping between separate turns uses `space-xl` (`24px`).

## Elevation & Depth

To avoid visual noise and retain an authentic, utilitarian posture, this design system avoids heavy drop shadows or dramatic blur effects. Depth is articulated purely through **Tonal Surface Stacking** and **Subtle Structural Borders**:

1. **Ground Zero (Base Canvas - `#0F1115`):** The non-interactive infinite backdrop behind all content.
2. **Layer 1 (Chat Stream & System Controls - `#171A21`):** Inset input bar, standard message bubbles, and system containers. Enclosed with a 1px border of `#2A2F3A`.
3. **Layer 2 (Raised Reference Cards & Flyouts - `#1E222B`):** Scheme performance summaries, asset breakdowns, NAV updates, and comparison dialogs. Framed with a 1px border of `#2A2F3A` and a faint ambient grounding shadow: `0px 4px 16px rgba(0, 0, 0, 0.35)`.
4. **Active Focus & Selection:** Elevation does not translate into vertical lift. Instead, active selection is signaled by a border transition to `#7C8CFF` alongside an ultra-soft focus rim: `0 0 0 1px #7C8CFF`.

## Shapes

The interface embraces a disciplined geometry anchored by standard **12px (`rounded-lg` equivalent)** corners:
- **Message Cards & Fact Containers:** Uniform 12px border radius provides approachable structure without appearing cartoonish.
- **Input Fields & Search Bars:** 10px to 12px radius, aligning with the primary card architecture.
- **Chips, Badges, & Action Tags:** Fully rounded pill shapes (`9999px`) to create clear semantic differentiation from rectangular data cards.
- **Buttons:** 8px to 10px radius for micro-actions; 12px for primary container-level buttons.

## Components

### 1. Message Cards & Conversational Streams
- **User Prompts:** Right-aligned or distinctive high-tier container using `#1E222B` with a subtle primary border (`#7C8CFF` at 40% opacity). Text in `#E6E8EC`.
- **System Bot Responses:** Left-aligned or centered, utilizing `#171A21` bounded by `#2A2F3A`. Generous 16px padding, crisp 12px radius. Responses strictly compartmentalize facts, numerical figures, and disclaimers using internal divider lines (`#2A2F3A`).
- **Citation & Source Chips:** Bottom-anchored inside bot cards, utilizing `code-sm` font in `#9AA3AF` over `#0F1115` badge capsules to confirm fund prospectus origins.

### 2. Buttons & Action Links
- **Primary Action (Scheme Selection / Drill-down):** Solid `#7C8CFF` fill with dark contrasting text (`#0F1115`, weight 600). Hover state lightens to `#909EFF`. Active compression effect: 98% scale.
- **Secondary Action (Copy, Clear, Query Shift):** Background transparent, border 1px `#2A2F3A`, text `#E6E8EC`. Hover state triggers surface shift to `#1E222B` and border shift to `#7C8CFF`.
- **Ghost Action:** No border, text `#9AA3AF`, hover to `#E6E8EC`.

### 3. Filter & Scheme Chips
- **Status:** Unselected chips feature `#171A21` background, `#2A2F3A` border, and `#9AA3AF` text.
- **Active State:** Fill `#1E222B`, border 1px solid `#7C8CFF`, text `#7C8CFF`.
- **Geometry:** 28px height, horizontal padding 12px, border-radius `9999px`.

### 4. Input Fields & Chat Composers
- **Container:** Persistent docked bar, surface `#171A21`, 1px border `#2A2F3A`. Internal padding 12px 16px.
- **Input Area:** Unbordered transparent field, placeholder color `#6B7280`, typed text `#E6E8EC`.
- **Action Accessory:** Send icon turns from `#6B7280` to `#7C8CFF` when input is valid. Focus-visible applies a 1px inner glow of `#7C8CFF`.

### 5. Fund Scheme Comparison Cards
- **Architecture:** Compact grid cards in `#1E222B` bordered by `#2A2F3A`.
- **Header:** Scheme name in `headline-md` (`Plus Jakarta Sans`), category badge (e.g., "Large Cap", "Flexi Cap") rendered in pill badge.
- **Metric Row:** 3-column layout featuring NAV, 3Y/5Y Trailing Return, and Expense Ratio. Metrics rendered in `Plus Jakarta Sans` 18px (`#E6E8EC`), with sub-labels in `Inter` 11px uppercase (`#9AA3AF`).

### 6. Semantic Banner Callouts
- **Advisory/Refusal Banner:** Background `#1F1A14`, border 1px solid `#F5B454` (30% opacity), text `#F5B454`, paired with a crisp information glyph. Used strictly when queries solicit market timing predictions or unapproved investment advice.
- **PII Warning Banner:** Background `#221617`, border 1px solid `#F87171` (30% opacity), text `#F87171`. Displayed when a user inputs personal folios, phone numbers, or PAN identifiers.