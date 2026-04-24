"""
core/flags.py — Feature flag and config reader for InstaAgent.

Reads config.yaml on every call (hot-reload — changes take effect
on the next orchestration cycle without restarting the process).
"""

import os
from dataclasses import dataclass
from typing import Any, Dict

import yaml

CONFIG_PATH = "config.yaml"


@dataclass
class Flags:
    """Typed representation of the [flags] section in config.yaml."""

    human_approval: bool = False
    trend_scraping: bool = False
    engagement_analysis: bool = True
    auto_caption: bool = True
    posting_enabled: bool = True


def load_config(path: str = CONFIG_PATH) -> Dict[str, Any]:
    """Load and return the full config.yaml as a dict."""
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def get_flags(path: str = CONFIG_PATH) -> Flags:
    """
    Load feature flags from config.yaml.

    Always re-reads the file — callers get updated flags on every call
    without restarting the process (hot-reload support).
    """
    try:
        config = load_config(path)
        raw = config.get("flags", {})
        return Flags(
            human_approval=bool(raw.get("human_approval", False)),
            trend_scraping=bool(raw.get("trend_scraping", False)),
            engagement_analysis=bool(raw.get("engagement_analysis", True)),
            auto_caption=bool(raw.get("auto_caption", True)),
            posting_enabled=bool(raw.get("posting_enabled", True)),
        )
    except FileNotFoundError:
        from core.logger import get_logger
        get_logger("Flags").error(
            f"config.yaml not found at '{path}'. Using safe defaults (posting disabled)."
        )
        return Flags(posting_enabled=False)
    except Exception as e:
        from core.logger import get_logger
        get_logger("Flags").error(f"Failed to parse config.yaml: {e}. Using safe defaults.")
        return Flags(posting_enabled=False)


def get_config(path: str = CONFIG_PATH) -> Dict[str, Any]:
    """Return full config dict. Alias for load_config with error handling."""
    try:
        return load_config(path)
    except Exception:
        return {}


def is_enabled(flag_name: str, path: str = CONFIG_PATH) -> bool:
    """Check a single feature flag by name."""
    flags = get_flags(path)
    return bool(getattr(flags, flag_name, False))
