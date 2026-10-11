from __future__ import annotations

import logging
import math
import os
import re
import shutil
import threading
import time
import zipfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
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


class SlowDownloadError(DownloadError):
    pass


@dataclass(frozen=True, slots=True)
class _Candidate:
    name: str
    url: str
    bypass_system_proxy: bool
    supports_range: bool = False
    probe_speed: float = 0.0


class _SpeedGuard:
    def __init__(self, *, minimum_bps: int, grace_seconds: float) -> None:
        self.minimum_bps = minimum_bps
        self.grace_seconds = grace_seconds
        self.started = time.monotonic()
        self.transferred = 0
        self.lock = threading.Lock()

    def add(self, amount: int) -> float:
        with self.lock:
            self.transferred += amount
            elapsed = max(time.monotonic() - self.started, 0.001)
            speed = self.transferred / elapsed
            if elapsed >= self.grace_seconds and speed < self.minimum_bps:
                raise SlowDownloadError(
                    f"下载速度持续过低: {speed / 1024:.0f}KB/s"
                )
            return speed


class DownloadManager:
    _PROBE_MIN_SIZE = 4 * 1024 * 1024
    _PROBE_BYTES = 256 * 1024
    _PARALLEL_MIN_SIZE = 16 * 1024 * 1024
    _PARALLEL_PARTS = 4
    _SLOW_MIN_BPS = 256 * 1024
    _SLOW_GRACE_SECONDS = 8.0

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
        self.timeout = httpx.Timeout(
            timeout_seconds,
            connect=min(3, timeout_seconds),
            read=min(15, timeout_seconds),
            write=min(15, timeout_seconds),
            pool=min(5, timeout_seconds),
        )
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
        candidates: list[_Candidate] = []
        if self.cos_enabled and cos_url:
            candidates.append(_Candidate("Tencent COS Direct", cos_url, True))
        candidates.extend(
            _Candidate(source_name, candidate_url, False)
            for source_name, candidate_url in self.source_manager.candidates(url)
        )
        if size >= self._PROBE_MIN_SIZE and self.transport is None:
            candidates = self._rank_candidates(candidates, size)
        attempted: list[str] = []
        for attempt in range(1, self.retries + 1):
            for candidate in candidates:
                attempted.append(candidate.name)
                started = time.monotonic()
                try:
                    logger.info(
                        "尝试下载源 asset_id=%s source=%s url=%s round=%s "
                        "range=%s probe_speed=%.2fMB/s",
                        asset_id,
                        candidate.name,
                        candidate.url,
                        attempt,
                        candidate.supports_range,
                        candidate.probe_speed / 1024 / 1024,
                    )
                    if (
                        candidate.supports_range
                        and size >= self._PARALLEL_MIN_SIZE
                    ):
                        self._download_segmented(
                            asset_id=asset_id,
                            candidate=candidate,
                            expected_size=size,
                            part=part,
                            progress=progress,
                        )
                    else:
                        self._prepare_serial_partial(part)
                        self._download_once(
                            asset_id=asset_id,
                            url=candidate.url,
                            expected_size=size,
                            part=part,
                            progress=progress,
                            bypass_system_proxy=candidate.bypass_system_proxy,
                        )
                    _validate_zip(part)
                    os.replace(part, completed)
                    self._clear_segments(part)
                    logger.info(
                        "文件下载完整性验证完成 asset_id=%s source=%s url=%s "
                        "status=200 size=%s elapsed=%.2fs speed=%.2fMB/s",
                        asset_id,
                        candidate.name,
                        candidate.url,
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
                    self._clear_partial(part)
                    raise
                except IntegrityError as exc:
                    last_error = exc
                    self._clear_partial(part)
                    logger.warning(
                        "下载源文件损坏 asset_id=%s source=%s error=%s",
                        asset_id,
                        candidate.name,
                        type(exc).__name__,
                    )
                except (httpx.HTTPError, OSError, DownloadError) as exc:
                    last_error = exc
                    logger.warning(
                        "下载源失败 asset_id=%s source=%s url=%s status=%s "
                        "error=%s elapsed=%.2fs",
                        asset_id,
                        candidate.name,
                        candidate.url,
                        _http_status(exc),
                        type(exc).__name__,
                        time.monotonic() - started,
                    )
            if attempt < self.retries:
                time.sleep(min(2 ** (attempt - 1), 4))
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
        offset = part.stat().st_size if part.is_file() else 0
        if expected_size and offset > expected_size:
            part.unlink(missing_ok=True)
            offset = 0
        headers = {"Range": f"bytes={offset}-"} if offset else None
        guard = _SpeedGuard(
            minimum_bps=self._SLOW_MIN_BPS,
            grace_seconds=self._SLOW_GRACE_SECONDS,
        )
        with httpx.Client(**self._client_options(bypass_system_proxy)) as client:
            with client.stream("GET", url, headers=headers) as response:
                if response.status_code == 429 or response.status_code >= 500:
                    raise DownloadError(f"HTTP {response.status_code}")
                if response.status_code == 416 and offset == expected_size:
                    return
                response.raise_for_status()
                header_total = int(response.headers.get("content-length") or 0)
                total = expected_size or header_total
                if offset and response.status_code == 206:
                    mode = "ab"
                    downloaded = offset
                    response_total = _content_range_total(response)
                    if expected_size and response_total and response_total != expected_size:
                        raise DownloadError("Content-Range 与 catalog 不一致")
                else:
                    mode = "wb"
                    downloaded = 0
                    offset = 0
                if (
                    expected_size
                    and header_total
                    and response.status_code != 206
                    and header_total != expected_size
                ):
                    raise DownloadError("Content-Length 与 catalog 不一致")
                with part.open(mode) as stream:
                    for chunk in response.iter_bytes(1024 * 1024):
                        if self.cancel_event.is_set():
                            raise DownloadCancelled("下载已取消")
                        downloaded += len(chunk)
                        if expected_size and downloaded > expected_size:
                            raise DownloadError("下载数据超过 catalog 文件大小")
                        stream.write(chunk)
                        now = time.monotonic()
                        speed = guard.add(len(chunk))
                        if progress and (now - last_report >= 0.2 or downloaded == total):
                            progress(
                                DownloadProgress(
                                    asset_id,
                                    downloaded,
                                    total,
                                    speed,
                                )
                            )
                            last_report = now
                    stream.flush()
                    os.fsync(stream.fileno())
        if expected_size and downloaded != expected_size:
            raise DownloadError(f"下载不完整: {downloaded} != {expected_size}")

    def _rank_candidates(
        self, candidates: list[_Candidate], expected_size: int
    ) -> list[_Candidate]:
        measured: list[tuple[int, _Candidate]] = []
        with ThreadPoolExecutor(max_workers=len(candidates)) as executor:
            futures = {
                executor.submit(
                    self._probe_candidate, candidate, expected_size
                ): (index, candidate)
                for index, candidate in enumerate(candidates)
            }
            for future in as_completed(futures):
                index, candidate = futures[future]
                try:
                    speed, supports_range = future.result()
                    measured.append(
                        (
                            index,
                            _Candidate(
                                candidate.name,
                                candidate.url,
                                candidate.bypass_system_proxy,
                                supports_range,
                                speed,
                            ),
                        )
                    )
                except (httpx.HTTPError, OSError, DownloadError) as exc:
                    logger.info(
                        "下载源测速失败 source=%s error=%s",
                        candidate.name,
                        type(exc).__name__,
                    )
                    measured.append((index, candidate))
        measured.sort(
            key=lambda item: (
                item[1].probe_speed <= 0,
                -item[1].probe_speed,
                item[0],
            )
        )
        return [candidate for _, candidate in measured]

    def _probe_candidate(
        self, candidate: _Candidate, expected_size: int
    ) -> tuple[float, bool]:
        end = min(self._PROBE_BYTES, expected_size) - 1
        timeout = httpx.Timeout(4, connect=3)
        options = self._client_options(candidate.bypass_system_proxy)
        options["timeout"] = timeout
        started = time.monotonic()
        downloaded = 0
        with httpx.Client(**options) as client:
            with client.stream(
                "GET", candidate.url, headers={"Range": f"bytes=0-{end}"}
            ) as response:
                if response.status_code not in {200, 206}:
                    raise DownloadError(f"HTTP {response.status_code}")
                for chunk in response.iter_bytes(64 * 1024):
                    downloaded += len(chunk)
                    if downloaded >= self._PROBE_BYTES:
                        break
        if not downloaded:
            raise DownloadError("测速未收到数据")
        elapsed = max(time.monotonic() - started, 0.001)
        return downloaded / elapsed, response.status_code == 206

    def _download_segmented(
        self,
        *,
        asset_id: str,
        candidate: _Candidate,
        expected_size: int,
        part: Path,
        progress: Callable[[DownloadProgress], None] | None,
    ) -> None:
        segment_size = math.ceil(expected_size / self._PARALLEL_PARTS)
        segments = [
            (
                index,
                index * segment_size,
                min(expected_size, (index + 1) * segment_size) - 1,
                Path(f"{part}.{index}"),
            )
            for index in range(self._PARALLEL_PARTS)
            if index * segment_size < expected_size
        ]
        initial = 0
        for _, start, end, segment_path in segments:
            expected = end - start + 1
            if segment_path.is_file() and segment_path.stat().st_size > expected:
                segment_path.unlink()
            initial += segment_path.stat().st_size if segment_path.is_file() else 0
        guard = _SpeedGuard(
            minimum_bps=self._SLOW_MIN_BPS,
            grace_seconds=self._SLOW_GRACE_SECONDS,
        )
        progress_lock = threading.Lock()
        abort = threading.Event()
        downloaded = initial

        def report(amount: int) -> None:
            nonlocal downloaded
            speed = guard.add(amount)
            with progress_lock:
                downloaded += amount
                if progress:
                    progress(
                        DownloadProgress(asset_id, downloaded, expected_size, speed)
                    )

        with ThreadPoolExecutor(max_workers=len(segments)) as executor:
            futures = [
                executor.submit(
                    self._download_range_segment,
                    candidate,
                    start,
                    end,
                    segment_path,
                    report,
                    abort,
                )
                for _, start, end, segment_path in segments
            ]
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception:
                    abort.set()
                    raise

        with part.open("wb") as output:
            for _, _, _, segment_path in segments:
                with segment_path.open("rb") as source:
                    shutil.copyfileobj(source, output, 1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        if part.stat().st_size != expected_size:
            raise DownloadError("分块合并后的文件大小不完整")

    def _download_range_segment(
        self,
        candidate: _Candidate,
        start: int,
        end: int,
        segment_path: Path,
        report: Callable[[int], None],
        abort: threading.Event,
    ) -> None:
        expected = end - start + 1
        current = segment_path.stat().st_size if segment_path.is_file() else 0
        if current == expected:
            return
        headers = {"Range": f"bytes={start + current}-{end}"}
        with httpx.Client(
            **self._client_options(candidate.bypass_system_proxy)
        ) as client:
            with client.stream("GET", candidate.url, headers=headers) as response:
                if response.status_code != 206:
                    raise DownloadError(
                        f"下载源不支持分块续传: HTTP {response.status_code}"
                    )
                with segment_path.open("ab") as stream:
                    for chunk in response.iter_bytes(1024 * 1024):
                        if self.cancel_event.is_set() or abort.is_set():
                            raise DownloadCancelled("下载已取消")
                        if current + len(chunk) > expected:
                            raise DownloadError("分块数据超过预期大小")
                        stream.write(chunk)
                        current += len(chunk)
                        report(len(chunk))
                    stream.flush()
                    os.fsync(stream.fileno())
        if current != expected:
            raise DownloadError(f"分块下载不完整: {current} != {expected}")

    def _client_options(self, bypass_system_proxy: bool) -> dict:
        options = {
            "timeout": self.timeout,
            "follow_redirects": True,
            "headers": {"User-Agent": "oc2-map-manager/1.3"},
            "trust_env": not bypass_system_proxy,
        }
        if self.transport is not None:
            options["transport"] = self.transport
        elif not bypass_system_proxy:
            options["proxy"] = windows_system_proxy()
        return options

    @staticmethod
    def _clear_segments(part: Path) -> None:
        for segment in part.parent.glob(part.name + ".[0-9]*"):
            segment.unlink(missing_ok=True)

    def _clear_partial(self, part: Path) -> None:
        part.unlink(missing_ok=True)
        self._clear_segments(part)

    def _prepare_serial_partial(self, part: Path) -> None:
        first_segment = Path(f"{part}.0")
        if not part.exists() and first_segment.is_file():
            shutil.copyfile(first_segment, part)


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


def _content_range_total(response: httpx.Response) -> int:
    value = response.headers.get("content-range", "")
    match = re.search(r"/(\d+)$", value)
    return int(match.group(1)) if match else 0
