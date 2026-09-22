from __future__ import annotations

import logging
import os
import re
import threading
import time
import zipfile
from collections.abc import Callable
from pathlib import Path

import httpx

from publisher.models import MapMetadata

from .models import DownloadProgress
from .download_sources import DownloadSourceManager
from .network import windows_system_proxy


logger = logging.getLogger(__name__)


class DownloadError(RuntimeError):
    pass


class DownloadCancelled(DownloadError):
    pass


class IntegrityError(DownloadError):
    """Retained for compatibility with callers from older client builds."""

    pass


class DownloadManager:
    def __init__(
        self,
        cache_directory: Path,
        *,
        timeout_seconds: int = 60,
        retries: int = 3,
        transport: httpx.BaseTransport | None = None,
        source_manager: DownloadSourceManager | None = None,
        cos_enabled: bool = True,
    ) -> None:
        self.directory = cache_directory / "downloads"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.timeout = httpx.Timeout(timeout_seconds, connect=min(8, timeout_seconds))
        self.retries = retries
        self.transport = transport
        self.source_manager = source_manager or DownloadSourceManager()
        self.cos_enabled = cos_enabled
        self.cancel_event = threading.Event()

    def cancel(self) -> None:
        self.cancel_event.set()

    def reset_cancel(self) -> None:
        self.cancel_event.clear()

    def download(
        self,
        metadata: MapMetadata,
        progress: Callable[[DownloadProgress], None] | None = None,
        *,
        force: bool = False,
    ) -> Path:
        return self.download_asset(
            asset_id=metadata.id,
            version=metadata.version,
            url=metadata.download_url,
            cos_url=metadata.cos_url,
            size=metadata.size,
            sha256=metadata.sha256,
            progress=progress,
            force=force,
        )

    def download_asset(
        self,
        *,
        asset_id: str,
        version: str,
        url: str,
        cos_url: str | None = None,
        size: int,
        sha256: str,
        progress: Callable[[DownloadProgress], None] | None = None,
        force: bool = False,
    ) -> Path:
        safe_version = re.sub(r"[^A-Za-z0-9._-]+", "-", version)[:80] or "unknown"
        safe_id = re.sub(r"[^A-Za-z0-9._-]+", "-", asset_id)[:100] or "asset"
        completed = self.directory / f"{safe_id}_{safe_version}.zip"
        part = completed.with_suffix(".zip.part")
        if not force and completed.is_file() and _is_complete_zip(completed, size):
            return completed
        completed.unlink(missing_ok=True)
        last_error: Exception | None = None
        self.reset_cancel()
        logger.info("文件下载开始 asset_id=%s version=%s", asset_id, version)
        candidates: list[tuple[str, str, bool]] = []
        if self.cos_enabled and cos_url:
            candidates.append(("Tencent COS Direct", cos_url, True))
        candidates.extend(
            (source_name, candidate_url, False)
            for source_name, candidate_url in self.source_manager.candidates(url)
        )
        attempted: list[str] = []
        for attempt in range(1, self.retries + 1):
            for source_name, candidate_url, bypass_system_proxy in candidates:
                part.unlink(missing_ok=True)
                attempted.append(source_name)
                started = time.monotonic()
                try:
                    logger.info(
                        "尝试下载源 asset_id=%s source=%s url=%s round=%s",
                        asset_id,
                        source_name,
                        candidate_url,
                        attempt,
                    )
                    self._download_once(
                        asset_id=asset_id,
                        url=candidate_url,
                        expected_size=size,
                        part=part,
                        progress=progress,
                        bypass_system_proxy=bypass_system_proxy,
                    )
                    _validate_zip(part)
                    os.replace(part, completed)
                    logger.info(
                        "文件下载完整性验证完成 asset_id=%s source=%s url=%s "
                        "status=200 size=%s elapsed=%.2fs speed=%.2fMB/s",
                        asset_id,
                        source_name,
                        candidate_url,
                        completed.stat().st_size,
                        max(time.monotonic() - started, 0.001),
                        completed.stat().st_size
                        / max(time.monotonic() - started, 0.001)
                        / 1024
                        / 1024,
                    )
                    return completed
                except DownloadCancelled:
                    logger.warning("文件下载取消 asset_id=%s", asset_id)
                    part.unlink(missing_ok=True)
                    raise
                except (httpx.HTTPError, OSError, DownloadError) as exc:
                    last_error = exc
                    part.unlink(missing_ok=True)
                    logger.warning(
                        "下载源失败 asset_id=%s source=%s url=%s status=%s "
                        "error=%s elapsed=%.2fs",
                        asset_id,
                        source_name,
                        candidate_url,
                        _http_status(exc),
                        type(exc).__name__,
                        time.monotonic() - started,
                    )
            if attempt < self.retries:
                time.sleep(min(2 ** (attempt - 1), 4))
        completed.unlink(missing_ok=True)
        names = ", ".join(dict.fromkeys(attempted))
        raise DownloadError(
            f"所有下载源均失败（{names}）: {type(last_error).__name__}"
        ) from last_error

    def _download_once(
        self,
        *,
        asset_id: str,
        url: str,
        expected_size: int,
        part: Path,
        progress: Callable[[DownloadProgress], None] | None,
        bypass_system_proxy: bool = False,
    ) -> None:
        started = time.monotonic()
        last_report = started
        downloaded = 0
        client_options = {
            "timeout": self.timeout,
            "follow_redirects": True,
            "headers": {"User-Agent": "oc2-map-manager/1.3"},
            "trust_env": not bypass_system_proxy,
        }
        if self.transport is not None:
            client_options["transport"] = self.transport
        elif not bypass_system_proxy:
            client_options["proxy"] = windows_system_proxy()
        with httpx.Client(**client_options) as client:
            with client.stream("GET", url) as response:
                if response.status_code == 429 or response.status_code >= 500:
                    raise DownloadError(f"HTTP {response.status_code}")
                response.raise_for_status()
                header_total = int(response.headers.get("content-length") or 0)
                total = expected_size or header_total
                if expected_size and header_total and header_total != expected_size:
                    raise DownloadError("Content-Length 与 catalog 不一致")
                with part.open("xb") as stream:
                    for chunk in response.iter_bytes(1024 * 1024):
                        if self.cancel_event.is_set():
                            raise DownloadCancelled("下载已取消")
                        downloaded += len(chunk)
                        if expected_size and downloaded > expected_size:
                            raise DownloadError("下载数据超过 catalog 文件大小")
                        stream.write(chunk)
                        now = time.monotonic()
                        if progress and (now - last_report >= 0.2 or downloaded == total):
                            progress(
                                DownloadProgress(
                                    asset_id,
                                    downloaded,
                                    total,
                                    downloaded / max(now - started, 0.001),
                                )
                            )
                            last_report = now
                    stream.flush()
                    os.fsync(stream.fileno())
        if expected_size and downloaded != expected_size:
            raise DownloadError(f"下载不完整: {downloaded} != {expected_size}")


def _is_complete_zip(path: Path, expected_size: int) -> bool:
    if expected_size and path.stat().st_size != expected_size:
        return False
    try:
        _validate_zip(path)
    except IntegrityError:
        return False
    return True


def _validate_zip(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            bad_member = archive.testzip()
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise IntegrityError("下载文件不是完整的 ZIP") from exc
    if bad_member is not None:
        raise IntegrityError(f"ZIP CRC 检查失败: {bad_member}")


def _http_status(exc: Exception) -> int | str:
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", "-")
