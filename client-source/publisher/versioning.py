from __future__ import annotations

import re
from datetime import datetime

from .models import MapVersion


_SOURCE_VERSION = re.compile(r"(?i)(?<!\d)v?(\d+(?:[._-]\d+){1,3})(?!\d)")


def extract_source_version(file_name: str) -> str | None:
    matches = _SOURCE_VERSION.findall(file_name)
    if not matches:
        return None
    parts = re.split(r"[._-]", matches[-1])
    # An eight-digit suffix after a three-part version is an upload date,
    # not a fourth version component: v0.2.1_20260915 -> 0.2.1.
    if len(parts) == 4 and len(parts[-1]) == 8 and parts[-1].startswith("20"):
        parts.pop()
    return ".".join(parts)


def sortable_version_tokens(version: str) -> tuple[tuple[int, int | str], ...]:
    numeric = re.fullmatch(r"[vV]?(\d+)[._-](\d+)", version.strip())
    if numeric is not None:
        major, minor = numeric.groups()
        # Some map authors write a patch release compactly: ``0.162`` means
        # ``0.16.2`` rather than a SemVer minor component numbered 162.  Keep
        # ordinary two-part versions (notably 0.9 -> 0.10) unchanged, while
        # expanding that established three-digit compact form.
        if major == "0" and len(minor) == 3:
            return ((1, 0), (1, int(minor[:-1])), (1, int(minor[-1])))

    tokens: list[tuple[int, int | str]] = []
    for token in re.findall(r"\d+|[a-z]+", version.casefold()):
        tokens.append((1, int(token)) if token.isdigit() else (0, token))
    return tuple(tokens)


def compare_versions(left: str, right: str) -> int:
    left_tokens = sortable_version_tokens(left)
    right_tokens = sortable_version_tokens(right)
    return (left_tokens > right_tokens) - (left_tokens < right_tokens)


def choose_version(
    *,
    source_version: str | None,
    published_at: datetime,
    existing: list[MapVersion],
) -> str:
    existing_versions = {item.version for item in existing}
    if source_version and source_version not in existing_versions:
        return source_version

    base = published_at.strftime("%Y.%m.%d")
    revision = 1
    while f"{base}.{revision}" in existing_versions:
        revision += 1
    return f"{base}.{revision}"
