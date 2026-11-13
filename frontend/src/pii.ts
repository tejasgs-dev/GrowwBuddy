// Client-side PII pre-check (task 5.8). A courtesy warning only: the server guard decides
// (EC-PII-16). The regexes come from the backend via a generated file (EV-5-07); this
// module mirrors the small amount of logic around them in pipeline/pii_guard.py.

import shared from "./generated/pii-patterns.json";

export type PiiType = "pan" | "aadhaar" | "phone" | "email" | "otp" | "account";

type PatternName = keyof typeof shared.patterns;

function rx(name: PatternName, global = false): RegExp {
  const p = shared.patterns[name];
  return new RegExp(p.source, p.flags + (global ? "g" : ""));
}

const NEAR: number = shared.near;

// Zero code points of Unicode decimal digit blocks used in India and nearby; NFKC handles
// full-width digits. Mirrors `normalise_digits` closely enough for a warning.
const DIGIT_ZEROS = [
  0x0660, 0x06f0, 0x0966, 0x09e6, 0x0a66, 0x0ae6, 0x0b66, 0x0be6, 0x0c66, 0x0ce6, 0x0d66,
  0x0de6, 0x0e50, 0x0ed0, 0x0f20, 0x1040, 0xff10,
];

export function normaliseDigits(text: string): string {
  let out = "";
  for (const ch of text) {
    const cp = ch.codePointAt(0)!;
    const zero = DIGIT_ZEROS.find((z) => cp >= z && cp <= z + 9);
    out += zero === undefined ? ch : String(cp - zero);
  }
  return out.normalize("NFKC");
}

// Verhoeff checksum (Aadhaar), same tables as the backend.
const D = [
  [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
  [1, 2, 3, 4, 0, 6, 7, 8, 9, 5],
  [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
  [3, 4, 0, 1, 2, 8, 9, 5, 6, 7],
  [4, 0, 1, 2, 3, 9, 5, 6, 7, 8],
  [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
  [6, 5, 9, 8, 7, 1, 0, 4, 3, 2],
  [7, 6, 5, 9, 8, 2, 1, 0, 4, 3],
  [8, 7, 6, 5, 9, 3, 2, 1, 0, 4],
  [9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
];
const P = [
  [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
  [1, 5, 7, 6, 2, 8, 3, 0, 9, 4],
  [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
  [8, 9, 1, 6, 0, 4, 3, 5, 2, 7],
  [9, 4, 5, 3, 1, 2, 6, 8, 7, 0],
  [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
  [2, 7, 9, 3, 8, 0, 6, 4, 1, 5],
  [7, 0, 4, 6, 9, 1, 3, 2, 5, 8],
];

export function verhoeffValid(num: string): boolean {
  let c = 0;
  const digits = num.split("").reverse();
  digits.forEach((ch, i) => {
    c = D[c][P[i % 8][Number(ch)]];
  });
  return c === 0;
}

const AMOUNT_BEFORE = rx("amount_before");
const AMOUNT_AFTER = rx("amount_after");

function isAmount(text: string, start: number, end: number): boolean {
  return (
    AMOUNT_BEFORE.test(text.slice(Math.max(0, start - 6), start)) ||
    AMOUNT_AFTER.test(text.slice(end, end + 12))
  );
}

function* matches(name: PatternName, text: string): Generator<RegExpExecArray> {
  const re = rx(name, true);
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    yield m;
    if (m[0] === "") re.lastIndex++;
  }
}

function hasAadhaar(t: string): boolean {
  for (const m of matches("aadhaar", t)) {
    const digits = m.slice(1, 4).join("");
    if (verhoeffValid(digits) && !isAmount(t, m.index, m.index + m[0].length)) return true;
  }
  return false;
}

function hasPhone(t: string): boolean {
  for (const m of matches("phone", t)) {
    if (!isAmount(t, m.index, m.index + m[0].length)) return true;
  }
  return false;
}

function hasOtp(t: string): boolean {
  const word = rx("otp_word");
  for (const m of matches("short_number", t)) {
    const window = t.slice(Math.max(0, m.index - NEAR), m.index + m[0].length + NEAR);
    if (word.test(window)) return true;
  }
  return false;
}

function hasAccount(t: string): boolean {
  for (const m of matches("account", t)) {
    if ((m[1].match(/\d/g) ?? []).length >= 6) return true;
  }
  return false;
}

/** The PII types found, in the backend's order. Never returns the matched text. */
export function checkPii(text: string): PiiType[] {
  const t = normaliseDigits(text);
  const found: PiiType[] = [];
  if (rx("pan").test(t) || rx("pan_spaced").test(t)) found.push("pan");
  if (hasAadhaar(t)) found.push("aadhaar");
  if (hasPhone(t)) found.push("phone");
  if (rx("email").test(t) || rx("email_disguised").test(t)) found.push("email");
  if (hasOtp(t)) found.push("otp");
  if (hasAccount(t)) found.push("account");
  return found;
}

export const PII_LABELS: Record<PiiType, string> = {
  pan: "PAN",
  aadhaar: "Aadhaar number",
  phone: "phone number",
  email: "email address",
  otp: "OTP",
  account: "account or folio number",
};

export function piiWarning(types: PiiType[]): string | null {
  if (types.length === 0) return null;
  const names = types.map((t) => PII_LABELS[t]);
  const list =
    names.length === 1 ? names[0] : `${names.slice(0, -1).join(", ")} and ${names.at(-1)}`;
  return `Possible personal details detected (${list}). Please remove them before sending.`;
}
