from __future__ import annotations

import importlib.resources

_VALID_STYLES = {"informational", "banter", "narrative"}


def load_system_prompt(style: str) -> str:
    if style not in _VALID_STYLES:
        raise FileNotFoundError(f"No system prompt for style '{style}'")
    # importlib.resources.files keeps this working when installed as a wheel.
    pkg = importlib.resources.files("pkm.extract.prompts.styles")
    return (pkg / f"{style}.md").read_text(encoding="utf-8")
