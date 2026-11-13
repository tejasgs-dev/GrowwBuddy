"""Fixed reply templates (task 3.2, architecture §8).

Refusals, performance replies and warnings come from code, never from the model, so their
wording is reviewed and consistent. Links are only ever one of the five Groww scheme pages
(task 1.2): the scheme named, else the scheme from the previous turn, else no link.

The formatter (task 3.11) adds the citation and footer; these functions only return text.
"""

from __future__ import annotations

from growwbuddy.constants import SCHEME_KEYS, SCHEME_NAMES

_SCHEME_LIST = (
    ", ".join(SCHEME_NAMES[k] for k in SCHEME_KEYS[:-1]) + " and " + SCHEME_NAMES[SCHEME_KEYS[-1]]
)
_TOPICS = "expense ratio, exit load, minimum SIP, benchmark or riskometer"


def _name(scheme: str | None) -> str:
    return SCHEME_NAMES[scheme] if scheme else "the scheme"


def advisory(scheme: str | None) -> str:
    about = f" about {_name(scheme)}" if scheme else ""
    # The last sentence covers mixed questions such as "exit load, and should I invest?"
    # (EC-INT-01): the whole message is refused, and the factual part can be asked alone.
    return (
        "I can only share facts about mutual fund schemes, so I can't advise on whether to "
        "invest, whether a fund is right for you, or how it might do. For advice, please speak "
        "to a SEBI-registered investment adviser. You can still ask me a factual question"
        f"{about} on its own, such as its exit load or expense ratio."
    )


def comparison(scheme: str | None) -> str:
    return (
        "I can't compare schemes or say which one is better. I can share facts about one "
        f"scheme at a time, such as its {_TOPICS}."
    )


def performance(scheme: str | None) -> str:
    if scheme:
        return (
            "I don't share returns, NAV or other performance figures. You can see the latest "
            f"figures for {_name(scheme)} on its Groww page."
        )
    return (
        "I don't share returns, NAV or other performance figures. Each scheme's Groww page "
        "shows its latest figures."
    )


def out_of_scope() -> str:
    return (
        f"I can only answer factual questions about five HDFC Mutual Fund schemes: {_SCHEME_LIST}. "
        f"Try asking about their {_TOPICS}."
    )


def elss() -> str:
    return (
        "None of the five schemes I cover is an ELSS (tax-saving) fund, so I can't answer "
        f"questions about ELSS lock-in or tax benefits. I cover {_SCHEME_LIST}."
    )


def personal() -> str:
    return (
        "I can't see your account, units or SIPs, and I don't need any personal details. "
        "Please check those in your Groww app. I can answer factual questions about the five "
        "HDFC schemes I cover."
    )


def internals() -> str:
    return (
        "I can't share how I work internally. I can answer factual questions about five HDFC "
        f"Mutual Fund schemes, such as their {_TOPICS}."
    )


def not_found(scheme: str | None) -> str:
    if scheme:
        return (
            f"I couldn't find that in the Groww page for {_name(scheme)}. You can check the "
            "scheme's details on its Groww page."
        )
    return "I couldn't find that in my sources."


def plan_not_covered(scheme: str | None) -> str:
    return (
        f"I only cover the Direct plan, Growth option of {_name(scheme)}, so I can't answer "
        "about Regular plans or IDCW options. You can check the scheme's Groww page for the "
        "Direct – Growth details."
    )


def fallback(scheme: str | None) -> str:
    """Safe fixed message when no verified answer could be produced (§6.5)."""
    if scheme:
        return (
            "I couldn't find a verified answer to that in my sources. You can check the "
            "scheme's Groww page."
        )
    return "I couldn't find a verified answer to that in my sources."


PII = (
    "For your security, please don't share personal details such as PAN, Aadhaar, account "
    "numbers, OTPs, email or phone numbers. I can only answer general factual questions about "
    "mutual fund schemes."
)

BUSY = "GrowwBuddy is busy right now. Please try again shortly."
UNAVAILABLE = "GrowwBuddy can't answer that right now. Please try again in a few minutes."
TOO_LONG = "That message is too long. Please ask one short question (up to 1,000 characters)."

WELCOME = (
    "Hi! I answer factual questions about five HDFC Mutual Fund schemes on Groww, such as "
    f"their {_TOPICS}. Facts-only. No investment advice."
)
THANKS = "You're welcome! Ask me another factual question any time."


def which_scheme() -> str:
    return f"Which scheme do you mean? I cover {_SCHEME_LIST}."


def one_scheme_at_a_time(schemes: list[str]) -> str:
    names = " or ".join(SCHEME_NAMES[s] for s in schemes)
    return f"I can answer about one scheme at a time. Which one do you mean: {names}?"
