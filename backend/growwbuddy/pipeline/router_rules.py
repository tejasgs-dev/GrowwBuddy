"""Stage 1 of the router: cheap keyword rules (task 3.3, architecture §6.2).

These decide the obvious cases with no model call, which also saves the tight Groq token
budget. Anything they don't decide goes to the LLM classifier (task 3.7).

Order matters, and errs towards refusing:
- injection and comparison are checked before advisory;
- advisory before performance;
- tax questions are never "performance", because the tax text itself says
  "returns are taxed at 20%" (corpus-notes quirk 4).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from growwbuddy.retrieval.headers import RetrievalHeaders, mentioned_schemes

RuleKind = Literal[
    "greeting",
    "thanks",
    "advisory",
    "comparison",
    "performance",
    "elss",
    "out_of_scope",
    "personal",
    "internals",
    "plan_not_covered",
    "not_in_corpus",
    "multi_scheme",
]


@dataclass(frozen=True)
class RuleDecision:
    kind: RuleKind
    schemes: list[str] = field(default_factory=list)  # in-scope schemes named in the text

    @property
    def scheme(self) -> str | None:
        return self.schemes[0] if self.schemes else None


def _rx(*patterns: str) -> re.Pattern[str]:
    return re.compile("|".join(f"(?:{p})" for p in patterns), re.IGNORECASE)


_GREETING = _rx(
    r"^\s*(hi+|hello|hey|hiya|namaste|namaskar|good (morning|afternoon|evening))"
    r"( there| groww ?buddy)?[\s!.,?]*$",
    r"^\s*(who (are|made|built) you|what can you do|what do you do|help)[\s?!.]*$",
)
_THANKS = _rx(
    r"^\s*(ok(ay)?[, ]*)?(thanks?( you)?( so much| a lot)?|thx|ty|dhanyavaad?|shukriya)[\s!.]*$"
)

# EC-INT-29, 30: attempts to switch off the rules are treated as advisory and refused.
_INJECTION = _rx(
    r"\bignore\b.{0,30}\b(rules?|instructions?|guidelines?|prompt|restrictions?)\b",
    r"\b(forget|disregard|override|bypass)\b.{0,30}\b(rules?|instructions?|guidelines?)\b",
    r"\bpretend (you('| a)?re|you are|to be)\b",
    r"\b(act|behave) (as|like) (a|an|my)\b",
    r"\brole[- ]?play\b",
    r"\bjailbreak\b",
    r"\bdeveloper mode\b",
)
# EC-INT-31
_INTERNALS = _rx(
    r"\b(system|hidden) prompt\b",
    r"\byour (instructions|prompt|rules|guidelines|configuration|source code)\b",
    r"\bwhat (llm|model|ai) (are you|do you use)\b",
)
# EC-INT-32: no personal-data features. ("my statement" is handled as not-in-corpus.)
_PERSONAL = _rx(
    r"\bmy (units?|sips?|portfolio|account|folio|holdings?|investments?|balance|money|"
    r"redemptions?|orders?|mandate|kyc)\b",
    r"\bhow many units\b",
    r"\bcheck my\b",
)

_COMPARE_WORDS = _rx(
    r"\bvs\.?\b",
    r"\bversus\b",
    r"\bcompar(e|ed|es|ing|ison)\b",
    r"\bdifference between\b",
    r"\b(better|worse|cheaper|costlier|safer|riskier) than\b",
)
_COMPARE_IF_TWO = _rx(  # comparison only when two schemes are named (EC-INT-02)
    r"\bwhich\b",
    r"\b(better|best|lower|higher|cheaper|costlier|safer|riskier|more|less)\b",
)

_ADVISORY = _rx(
    r"\bshould (i|we|one|my)\b",
    r"\bshall (i|we)\b",
    r"\b(recommend|recommendation|suggest|suggestion|advice|advise|adviser|advisor)\w*\b",
    r"\bbest\b",
    r"\bbetter\b",
    r"\bworth (it|investing|buying|holding)\b",
    r"\b(good|great|bad|ideal|wise)\b(?! (morning|afternoon|evening|night|year))",
    r"\bright (for|fund|scheme|time|choice)\b",
    r"\b(safe|safer|suitable|attractive|profitable)\b",
    r"\bbeginners?\b",
    r"\bwill (it|this|the fund|silver|the nav|nav|the market|markets?|prices?)\b.{0,20}"
    r"\b(go up|go down|rise|fall|grow|crash|increase|decrease|recover|perform)\b",
    r"\b(predict|prediction|forecast|outlook|expected return)\w*\b",
    r"\b(buy|sell|invest|exit|redeem|switch|hold)\b.{0,15}\b(now|today|or not|or wait)\b",
    r"\btime to (buy|sell|invest|exit|enter)\b",
    # Hinglish
    r"\b(chahiye|accha|acha|achha|sahi) (hai|rahega)\b",
    r"\b(lu|lun|loon|karu|karun|kharidu) kya\b",
    r"\binvest karna chahiye\b",
)

_TAX = _rx(r"\btax(es|ed|ation|able)?\b", r"\bltcg\b", r"\bstcg\b", r"\bcapital gains?\b")
_PERFORMANCE = _rx(
    r"\breturns?\b",
    r"\breturned\b",
    r"\b(cagr|xirr|irr|alpha|sharpe|annuali[sz]ed)\b",
    r"\bperform(s|ed|ing|ance)?\b",
    r"\bnav\b",  # word boundary: "navigate" doesn't match
    r"\bnet asset value\b",
    r"\b(unit|current|today'?s|latest|share) price\b",
    r"\bprice (of|per unit)\b",
    r"\bgrew\b",
    r"\bgrown\b",
    r"\bhow much\b.{0,30}\b(grow|gain|earn|make|get|give)\w*\b",
    r"\bwhat will i get\b",
    r"\b(good|bad|best|worst) year\b",
    r"\btrack record\b",
    r"\b\d+\s*-?\s*(y|yr|yrs|year|years|month|months)\b.{0,10}\b(return|cagr|performance)\b",
)

_ELSS = _rx(r"\belss\b", r"\btax[- ]?sav(ing|er|ers)\b", r"\b80 ?c\b", r"\bsection 80\b")

_OTHER_AMC = _rx(
    r"\b(sbi|icici|axis|nippon|kotak|mirae|parag parikh|ppfas|uti|aditya birla|absl|tata|dsp|"
    r"quant|motilal|franklin|templeton|edelweiss|bandhan|canara|invesco|sundaram|baroda|hsbc|"
    r"lic|pgim|union|whiteoak|navi|zerodha|bajaj finserv|360 one|mahindra manulife)\b"
)
_OTHER_HDFC = _rx(
    r"\blarge\s*(and|&)\s*mid\s*-?\s*cap\b",
    r"\bhdfc\s+(flexi|focused|balanced|hybrid|liquid|gilt|debt|overnight|money market|"
    r"arbitrage|index|nifty|sensex|defence|banking|pharma|technology|infra\w*|dividend|value|"
    r"children|retirement|gold|equity savings|corporate bond|short term|ultra short|"
    r"credit risk|dynamic|business cycle|manufacturing|transportation|housing|mnc|"
    r"multi[- ]?asset|small and mid)\b",
)
_NOT_SECURITIES = _rx(
    r"\b(bitcoin|crypto\w*|ethereum|stock price|share price|ipo|fixed deposit|"
    r"insurance|loan|credit card)\b"
)

# EC-INT-17: only Direct - Growth is covered.
_PLAN = _rx(
    r"\bregular (plan|option)\b",
    r"\bidcw\b",
    r"\bdividend (option|plan|payout)\b",
    r"\b(payout|reinvestment) option\b",
)
# On no page (coverage report): AUM, holdings, statements. "not found" with no model call.
_NOT_IN_CORPUS = _rx(
    r"\baum\b",
    r"\bassets under management\b",
    r"\b(fund|corpus) size\b",
    r"\bholdings?\b",
    r"\btop (stocks|companies|holdings)\b",
    r"\b(sector|asset) allocation\b",
    r"\bportfolio\b",
    r"\b(capital gains?|account|cas|consolidated|transaction) statement\b",
    r"\bdownload\b.{0,20}\bstatement\b",
)
_ALL_SCHEMES = _rx(r"\ball (five|5|the|of the)?\s*(funds|schemes)\b", r"\beach (fund|scheme)\b")


def decide(text: str, headers: RetrievalHeaders | None = None) -> RuleDecision | None:
    """A decision for obvious cases, or None to hand over to the LLM router."""
    schemes = mentioned_schemes(text, headers)

    def d(kind: RuleKind) -> RuleDecision:
        return RuleDecision(kind, schemes)

    if _GREETING.search(text):
        return d("greeting")
    if _THANKS.search(text):
        return d("thanks")
    if _INJECTION.search(text):
        return d("advisory")
    if _INTERNALS.search(text):
        return d("internals")
    if _PERSONAL.search(text):
        return d("personal")
    if _COMPARE_WORDS.search(text) or (len(schemes) >= 2 and _COMPARE_IF_TWO.search(text)):
        return d("comparison")
    if _ADVISORY.search(text):
        return d("advisory")
    if _PERFORMANCE.search(text) and not _TAX.search(text):
        return d("performance")
    if _ELSS.search(text):
        return d("elss")
    if _OTHER_HDFC.search(text) or _OTHER_AMC.search(text) or _NOT_SECURITIES.search(text):
        return d("out_of_scope")
    if _PLAN.search(text):
        return d("plan_not_covered")
    if _NOT_IN_CORPUS.search(text):
        return d("not_in_corpus")
    if len(schemes) >= 2 or _ALL_SCHEMES.search(text):
        return d("multi_scheme")
    return None
