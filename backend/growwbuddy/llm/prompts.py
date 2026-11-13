"""Load the static system prompts from backend/prompts/ (task 3.6).

They hold no dates, IDs or per-request text, so they stay byte-identical across calls and
Groq can cache them (§7.5). Cached tokens also don't count toward Groq's rate limits.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"


@cache
def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8").strip()
