from __future__ import annotations

import os
from urllib.parse import urlsplit


def windows_system_proxy() -> str | None:
    """Return the user's Windows HTTPS proxy in a form accepted by httpx.

    Chromium-based browsers use the WinINET proxy stored in the registry, while
    httpx normally only honours proxy environment variables.  Reading the same
    setting explicitly keeps the desktop client on the browser's working route.
    """

    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            enabled = int(winreg.QueryValueEx(key, "ProxyEnable")[0])
            raw = str(winreg.QueryValueEx(key, "ProxyServer")[0]).strip()
    except (OSError, TypeError, ValueError):
        return None
    if not enabled or not raw:
        return None

    # WinINET accepts either "host:port" or
    # "http=host:port;https=host:port;socks=host:port".
    entries: dict[str, str] = {}
    if ";" in raw or "=" in raw:
        for item in raw.split(";"):
            scheme, separator, value = item.partition("=")
            if separator and value.strip():
                entries[scheme.strip().casefold()] = value.strip()
        raw = entries.get("https") or entries.get("http") or entries.get("socks") or ""
    if not raw:
        return None
    if "://" not in raw:
        raw = f"http://{raw}"
    parsed = urlsplit(raw)
    if parsed.scheme.casefold() not in {"http", "https", "socks5", "socks5h"}:
        return None
    if not parsed.hostname or parsed.port is None:
        return None
    return raw
