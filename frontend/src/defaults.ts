// Built-in copies of the /api/meta text, used until it loads and if it fails, so the
// disclaimer is never missing (EC-UI-12, EV-5-01). The server's values win when available.

import type { Meta } from "./types";

export const DISCLAIMER = "Facts-only. No investment advice.";

const GROWW = "https://groww.in/mutual-funds/";

export const DEFAULT_META: Meta = {
  welcome:
    "Hi! I answer factual questions about five HDFC Mutual Fund schemes on Groww, such as " +
    "their expense ratio, exit load, minimum SIP, benchmark or riskometer. Facts-only. " +
    "No investment advice.",
  example_questions: [
    "What is the expense ratio of HDFC Mid Cap Fund?",
    "What is the exit load of HDFC Small Cap Fund?",
    "What is the minimum SIP amount for HDFC Large Cap Fund?",
  ],
  disclaimer: DISCLAIMER,
  input_hint: "Please don't share PAN, Aadhaar, account numbers, OTPs, email or phone numbers.",
  last_updated: null,
  footer: null,
  schemes: [
    { key: "hdfc-mid-cap", name: "HDFC Mid Cap Fund", url: `${GROWW}hdfc-mid-cap-fund-direct-growth` },
    { key: "hdfc-small-cap", name: "HDFC Small Cap Fund", url: `${GROWW}hdfc-small-cap-fund-direct-growth` },
    { key: "hdfc-large-cap", name: "HDFC Large Cap Fund", url: `${GROWW}hdfc-large-cap-fund-direct-growth` },
    { key: "hdfc-silver-etf-fof", name: "HDFC Silver ETF FoF", url: `${GROWW}hdfc-silver-etf-fof-direct-growth` },
    { key: "hdfc-multi-cap", name: "HDFC Multi Cap Fund", url: `${GROWW}hdfc-multi-cap-fund-direct-growth` },
  ],
};

/** Merge a server response over the defaults, keeping the built-in disclaimer if blank. */
export function mergeMeta(server: Partial<Meta> | null): Meta {
  if (!server) return DEFAULT_META;
  const m = { ...DEFAULT_META, ...server };
  if (!m.disclaimer?.trim()) m.disclaimer = DISCLAIMER;
  if (!Array.isArray(m.example_questions) || m.example_questions.length === 0) {
    m.example_questions = DEFAULT_META.example_questions;
  }
  if (!Array.isArray(m.schemes) || m.schemes.length === 0) m.schemes = DEFAULT_META.schemes;
  return m;
}

const ALLOWED_DOMAIN = "groww.in";

/** Only https links on groww.in (or a subdomain) are rendered (EC-UI-03, EV-5-05). */
export function isAllowedLink(url: string | null | undefined): url is string {
  if (!url) return false;
  try {
    const u = new URL(url);
    const host = u.hostname.toLowerCase();
    return (
      u.protocol === "https:" &&
      !u.username &&
      !u.password &&
      (host === ALLOWED_DOMAIN || host.endsWith(`.${ALLOWED_DOMAIN}`))
    );
  } catch {
    return false;
  }
}
