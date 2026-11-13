import { describe, expect, it } from "vitest";
import shared from "../generated/pii-patterns.json";
import { checkPii, normaliseDigits, piiWarning, verhoeffValid } from "../pii";

// A Verhoeff-valid 12-digit number starting 2-9 (same algorithm as the backend tests).
const AADHAAR = "234123412346";

describe("shared PII patterns (EV-5-07)", () => {
  it("every exported pattern compiles as a JavaScript RegExp", () => {
    for (const [name, p] of Object.entries(shared.patterns)) {
      expect(() => new RegExp(p.source, p.flags), name).not.toThrow();
    }
  });
});

describe("checkPii mirrors the server guard (EV-5-06)", () => {
  it.each([
    ["My PAN is ABCDE1234F", "pan"],
    ["pan abcde1234f please", "pan"],
    ["PAN ABCDE 1234 F", "pan"],
    [`aadhaar ${AADHAAR}`, "aadhaar"],
    ["aadhaar 2341 2341 2346", "aadhaar"],
    ["call me on 9876543210", "phone"],
    ["+91 98765 43210", "phone"],
    ["mail me at me@example.com", "email"],
    ["me (at) example (dot) com", "email"],
    ["my otp is 482913", "otp"],
    ["My OTP is 482913, please complete my SIP", "otp"], // found by the Phase 6 eval
    ["my otp is 482913.", "otp"],
    ["folio no 12345678", "account"],
    ["account number: 1234-5678-90", "account"],
    ["phone ९८७६५४३२१०", "phone"],
  ])("flags %s", (text, type) => {
    expect(checkPii(text)).toContain(type);
  });

  it.each([
    "What is the expense ratio of HDFC Mid Cap Fund?",
    "Minimum SIP is ₹500",
    "Is ₹1000000000 a large AUM?",
    "Exit load in 2026",
    "Rs. 9876543210 lump sum",
    "What is the scheme code 123456?",
    "PIN code 560001",
    "navigate to the HDFC Small Cap page",
    "234123412345", // fails Verhoeff
  ])("does not flag %s", (text) => {
    expect(checkPii(text)).toEqual([]);
  });

  it("Verhoeff matches known values", () => {
    expect(verhoeffValid(AADHAAR)).toBe(true);
    expect(verhoeffValid("234123412345")).toBe(false);
  });

  it("normalises non-ASCII digits", () => {
    expect(normaliseDigits("१२३ ０１２")).toBe("123 012");
  });

  it("warning names the types, never the value", () => {
    const w = piiWarning(checkPii("PAN ABCDE1234F, phone 9876543210"))!;
    expect(w).toContain("PAN");
    expect(w).toContain("phone number");
    expect(w).not.toContain("ABCDE1234F");
    expect(w).not.toContain("9876543210");
    expect(piiWarning([])).toBeNull();
  });
});
