from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


class DownloadSourceConfigurationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DownloadSource:
    name: str
    url_template: str

    def build_url(self, official_url: str) -> str:
        return self.url_template.replace("{url}", official_url)


class DownloadSourceManager:
    """Build ordered download candidates from one editable JSON file."""

    def __init__(self, config_path: Path | None = None) -> None:
        self.config_path = config_path or default_proxy_sources_path()
        self.sources = _load_sources(self.config_path)

    def candidates(self, official_url: str) -> list[tuple[str, str]]:
        parsed = urlsplit(official_url)
        if parsed.scheme != "https" or parsed.hostname is None:
            raise DownloadSourceConfigurationError("manifest 下载地址必须是 HTTPS")
        # Proxy templates are only appropriate for public GitHub downloads.
        if parsed.hostname.casefold() not in {"github.com", "www.github.com"}:
            return [("Direct", official_url)]
        result: list[tuple[str, str]] = []
        seen: set[str] = set()
        for source in self.sources:
            candidate = source.build_url(official_url)
            if candidate not in seen:
                result.append((source.name, candidate))
                seen.add(candidate)
        return result


def default_proxy_sources_path() -> Path:
    if getattr(sys, "frozen", False):
        external = Path(sys.executable).resolve().parent / "proxy_sources.json"
        if external.is_file():
            return external
        bundle = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        return bundle / "proxy_sources.json"
    return Path(__file__).resolve().parents[1] / "proxy_sources.json"


def _load_sources(path: Path) -> tuple[DownloadSource, ...]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DownloadSourceConfigurationError(
            f"无法读取下载源配置: {path}"
        ) from exc
    if not isinstance(payload, list):
        raise DownloadSourceConfigurationError("proxy_sources.json 根节点必须是数组")
    result: list[DownloadSource] = []
    for item in payload:
        if not isinstance(item, dict) or not item.get("enabled", True):
            continue
        name = str(item.get("name") or "").strip()
        template = str(item.get("urlTemplate") or "").strip()
        if not name or template.count("{url}") != 1:
            raise DownloadSourceConfigurationError("下载源必须包含 name 和一个 {url}")
        sample = template.replace("{url}", "https://github.com/o/r/releases/download/t/a.zip")
        if urlsplit(sample).scheme != "https":
            raise DownloadSourceConfigurationError(f"下载源 {name} 必须使用 HTTPS")
        result.append(DownloadSource(name, template))
    if not result:
        raise DownloadSourceConfigurationError("没有启用的下载源")
    return tuple(result)
