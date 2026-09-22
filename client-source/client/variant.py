from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ClientVariant:
    key: str
    display_name: str
    badge_text: str
    cos_enabled: bool


def current_variant() -> ClientVariant:
    if os.getenv("OC2_CLIENT_VARIANT", "cos").strip().casefold() == "public":
        return ClientVariant(
            key="public",
            display_name="公开版（GitHub 线路）",
            badge_text="公开版",
            cos_enabled=False,
        )
    return ClientVariant(
        key="cos",
        display_name="COS 专享版",
        badge_text="COS 专享版 · 国内高速线路",
        cos_enabled=True,
    )
