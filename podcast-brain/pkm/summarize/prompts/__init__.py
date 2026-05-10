from __future__ import annotations

import importlib.resources

_VALID_STYLES = {"informational", "banter", "narrative"}


def load_summary_prompt(style: str) -> str:
    if style not in _VALID_STYLES:
        raise FileNotFoundError(f"No summary prompt for style '{style}'")
    pkg = importlib.resources.files("pkm.summarize.prompts.styles")
    return (pkg / f"{style}.md").read_text(encoding="utf-8")
