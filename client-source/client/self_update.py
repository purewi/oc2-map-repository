from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx

from publisher.models import ClientRelease

from .download_sources import DownloadSourceManager
from .models import DownloadProgress
from .network import windows_system_proxy
from .variant import current_variant


logger = logging.getLogger(__name__)


class ClientUpdateError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ClientUpdateInfo:
    version: str
    download_url: str
    cos_url: str | None
    size: int
    file_name: str
    mandatory: bool
    release_notes: tuple[str, ...]


def compare_client_versions(left: str, right: str) -> int:
    left_value = _semantic_version(left)
    right_value = _semantic_version(right)
    return (left_value > right_value) - (left_value < right_value)


def resolve_client_update(
    release: ClientRelease | None,
    *,
    variant: str,
    local_version: str,
) -> ClientUpdateInfo | None:
    if release is None or not release.latest_version:
        return None
    remote_version = release.latest_version.strip().removeprefix("v")
    if compare_client_versions(remote_version, local_version) <= 0:
        return None
    asset = release.asset_for(variant)
    if asset is None:
        return None
    mandatory = release.mandatory
    if release.minimum_version:
        mandatory = mandatory or compare_client_versions(
            local_version, release.minimum_version
        ) < 0
    return ClientUpdateInfo(
        version=remote_version,
        download_url=asset.download_url,
        cos_url=asset.cos_url,
        size=asset.size,
        file_name=asset.file_name,
        mandatory=mandatory,
        release_notes=tuple(release.notes()),
    )


class ClientSelfUpdater:
    """Download a complete replacement EXE and start it as the update helper."""

    def __init__(
        self,
        cache_directory: Path,
        *,
        timeout_seconds: int = 60,
        retries: int = 3,
        transport: httpx.BaseTransport | None = None,
        source_manager: DownloadSourceManager | None = None,
        executable: Path | None = None,
        frozen: bool | None = None,
    ) -> None:
        self.directory = cache_directory / "client-updates"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.timeout = httpx.Timeout(timeout_seconds, connect=min(8, timeout_seconds))
        self.retries = retries
        self.transport = transport
        self.source_manager = source_manager or DownloadSourceManager()
        self.executable = (executable or Path(sys.executable)).resolve()
        self.frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
        self.cancel_event = threading.Event()

    @property
    def can_apply(self) -> bool:
        return os.name == "nt" and self.frozen and self.executable.suffix.casefold() == ".exe"

    def cancel(self) -> None:
        self.cancel_event.set()

    def download(
        self,
        info: ClientUpdateInfo,
        progress: Callable[[DownloadProgress], None] | None = None,
    ) -> Path:
        safe_version = re.sub(r"[^0-9A-Za-z._-]+", "-", info.version)[:40]
        safe_variant = current_variant().key
        completed = self.directory / f"OC2MapManager-{safe_variant}-v{safe_version}.exe"
        part = completed.with_suffix(".exe.part")
        if _is_complete_executable(completed, info.size):
            return completed
        completed.unlink(missing_ok=True)
        self.cancel_event.clear()
        candidates: list[tuple[str, str, bool]] = []
        if current_variant().cos_enabled and info.cos_url:
            candidates.append(("Tencent COS Direct", info.cos_url, True))
        candidates.extend(
            (name, url, False)
            for name, url in self.source_manager.candidates(info.download_url)
        )
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            for source_name, url, bypass_proxy in candidates:
                part.unlink(missing_ok=True)
                try:
                    logger.info(
                        "[CLIENT_UPDATE] downloading version=%s source=%s round=%s",
                        info.version,
                        source_name,
                        attempt,
                    )
                    self._download_once(
                        url=url,
                        expected_size=info.size,
                        destination=part,
                        progress=progress,
                        bypass_system_proxy=bypass_proxy,
                    )
                    _validate_executable(part, info.size)
                    os.replace(part, completed)
                    logger.info(
                        "[CLIENT_UPDATE] download complete version=%s source=%s size=%s",
                        info.version,
                        source_name,
                        info.size,
                    )
                    return completed
                except (httpx.HTTPError, OSError, ClientUpdateError) as exc:
                    last_error = exc
                    part.unlink(missing_ok=True)
                    logger.warning(
                        "[CLIENT_UPDATE] source failed version=%s source=%s error_type=%s",
                        info.version,
                        source_name,
                        type(exc).__name__,
                    )
            if attempt < self.retries:
                time.sleep(min(2 ** (attempt - 1), 4))
        raise ClientUpdateError(
            f"客户端更新包下载失败: {type(last_error).__name__}"
        ) from last_error

    def _download_once(
        self,
        *,
        url: str,
        expected_size: int,
        destination: Path,
        progress: Callable[[DownloadProgress], None] | None,
        bypass_system_proxy: bool,
    ) -> None:
        options: dict[str, object] = {
            "timeout": self.timeout,
            "follow_redirects": True,
            "headers": {"User-Agent": "oc2-map-manager-self-update"},
            "trust_env": not bypass_system_proxy,
        }
        if self.transport is not None:
            options["transport"] = self.transport
        elif not bypass_system_proxy:
            options["proxy"] = windows_system_proxy()
        started = time.monotonic()
        downloaded = 0
        last_report = started
        with httpx.Client(**options) as client:
            with client.stream("GET", url) as response:
                if response.status_code == 429 or response.status_code >= 500:
                    raise ClientUpdateError(f"HTTP {response.status_code}")
                response.raise_for_status()
                header_size = int(response.headers.get("content-length") or 0)
                if header_size and header_size != expected_size:
                    raise ClientUpdateError("更新包 Content-Length 与发布信息不一致")
                with destination.open("xb") as stream:
                    for chunk in response.iter_bytes(1024 * 1024):
                        if self.cancel_event.is_set():
                            raise ClientUpdateError("客户端更新已取消")
                        downloaded += len(chunk)
                        if downloaded > expected_size:
                            raise ClientUpdateError("更新包数据超过声明大小")
                        stream.write(chunk)
                        now = time.monotonic()
                        if progress and (
                            now - last_report >= 0.2 or downloaded == expected_size
                        ):
                            progress(
                                DownloadProgress(
                                    "client-update",
                                    downloaded,
                                    expected_size,
                                    downloaded / max(now - started, 0.001),
                                )
                            )
                            last_report = now
                    stream.flush()
                    os.fsync(stream.fileno())
        if downloaded != expected_size:
            raise ClientUpdateError(
                f"客户端更新包下载不完整: {downloaded} != {expected_size}"
            )

    def start_apply(self, archive: Path, info: ClientUpdateInfo) -> None:
        if not self.can_apply:
            raise ClientUpdateError("源码运行模式不能自动替换客户端，请使用单文件版")
        _validate_executable(archive, info.size)
        subprocess.Popen(
            [
                str(archive.resolve()),
                "--apply-client-update",
                str(os.getpid()),
                str(self.executable),
                str(info.size),
                str(os.getppid()),
            ],
            cwd=str(archive.resolve().parent),
            close_fds=True,
        )
        logger.info("[CLIENT_UPDATE] starting updater version=%s", info.version)


def run_update_helper_if_requested(argv: Sequence[str]) -> int | None:
    try:
        index = argv.index("--apply-client-update")
    except ValueError:
        return None
    try:
        old_pid = int(argv[index + 1])
        target = Path(argv[index + 2]).resolve()
        expected_size = int(argv[index + 3])
    except (IndexError, TypeError, ValueError) as exc:
        raise ClientUpdateError("客户端更新助手参数无效") from exc
    old_parent_pid = None
    if len(argv) > index + 4:
        try:
            old_parent_pid = int(argv[index + 4])
        except ValueError as exc:
            raise ClientUpdateError("客户端更新助手父进程参数无效") from exc
    return apply_client_update(
        source=Path(sys.executable).resolve(),
        target=target,
        old_pid=old_pid,
        old_parent_pid=old_parent_pid,
        expected_size=expected_size,
    )


def apply_client_update(
    *,
    source: Path,
    target: Path,
    old_pid: int,
    expected_size: int,
    old_parent_pid: int | None = None,
) -> int:
    source = source.resolve()
    target = target.resolve()
    if source == target or target.suffix.casefold() != ".exe":
        raise ClientUpdateError("客户端更新目标无效")
    _validate_executable(source, expected_size)
    if not _wait_for_old_processes((old_pid, old_parent_pid), 90):
        raise ClientUpdateError("等待旧客户端退出超时")

    backup: Path | None = None
    replaced = False
    relocated = False
    launch_target = target
    try:
        if target.is_file():
            backup = _backup_client(target, source)
        try:
            _install_in_place(source, target, expected_size)
            replaced = True
        except PermissionError:
            logger.warning(
                "[CLIENT_UPDATE] target directory denied replacement; relocating target=%s",
                target,
                exc_info=True,
            )
            if backup is not None:
                try:
                    _restore_backup(backup, target)
                except OSError:
                    logger.warning(
                        "[CLIENT_UPDATE] original target could not be restored before relocation",
                        exc_info=True,
                    )
            launch_target = _relocated_target(target)
            launch_target.parent.mkdir(parents=True, exist_ok=True)
            _copy_executable(source, launch_target, expected_size)
            relocated = True
        subprocess.Popen(
            [
                str(launch_target),
                "--client-update-finished",
                *(["--client-update-relocated"] if relocated else []),
            ],
            cwd=str(launch_target.parent),
            close_fds=True,
        )
        return 0
    except Exception as exc:
        if backup is not None and (replaced or not target.exists()):
            try:
                _restore_backup(backup, target)
            except OSError:
                logger.exception("[CLIENT_UPDATE] restoring previous client failed")
        raise ClientUpdateError(
            f"客户端替换失败，已尝试恢复旧版本: {type(exc).__name__}"
        ) from exc


def _wait_for_old_processes(
    pids: Sequence[int | None], timeout_seconds: int
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    for pid in dict.fromkeys(pids):
        if pid is None or pid <= 0 or pid == os.getpid():
            continue
        remaining = max(0, int(deadline - time.monotonic()))
        if remaining == 0 or not _wait_for_process(pid, remaining):
            return False
    return True


def _backup_client(target: Path, source: Path) -> Path:
    preferred = target.with_name(target.name + ".previous")
    try:
        _safe_unlink(preferred)
        shutil.copy2(target, preferred)
        return preferred
    except OSError:
        logger.warning(
            "[CLIENT_UPDATE] target directory denied backup; using update cache",
            exc_info=True,
        )
    fallback = source.parent / "previous" / target.name
    fallback.parent.mkdir(parents=True, exist_ok=True)
    _safe_unlink(fallback)
    shutil.copy2(target, fallback)
    return fallback


def _install_in_place(
    source: Path, target: Path, expected_size: int, timeout_seconds: int = 30
) -> None:
    incoming = target.with_name(target.name + ".updating")
    _safe_unlink(incoming)
    try:
        shutil.copy2(source, incoming)
        _validate_executable(incoming, expected_size)
        deadline = time.monotonic() + timeout_seconds
        last_error: PermissionError | None = None
        while True:
            try:
                os.replace(incoming, target)
                return
            except PermissionError as exc:
                last_error = exc
                try:
                    _copy_executable(source, target, expected_size)
                    return
                except PermissionError as copy_error:
                    last_error = copy_error
                if time.monotonic() >= deadline:
                    raise last_error
                time.sleep(0.5)
    finally:
        _safe_unlink(incoming)


def _copy_executable(source: Path, target: Path, expected_size: int) -> None:
    with source.open("rb") as input_stream, target.open("wb") as output_stream:
        shutil.copyfileobj(input_stream, output_stream, 1024 * 1024)
        output_stream.flush()
        os.fsync(output_stream.fileno())
    _validate_executable(target, expected_size)


def _restore_backup(backup: Path, target: Path) -> None:
    _copy_executable(backup, target, backup.stat().st_size)


def _relocated_target(original: Path) -> Path:
    root = Path(os.getenv("LOCALAPPDATA") or original.parent) / "OC2MapManager" / "app"
    name = (
        "OC2MapManager-COS-VIP.exe"
        if "cos" in original.name.casefold()
        else "OC2MapManager-Public.exe"
    )
    return root / name


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("[CLIENT_UPDATE] temporary file cleanup deferred path=%s", path)


def cleanup_previous_client(
    executable: Path | None = None,
    *,
    attempts: int = 5,
    retry_delay_seconds: float = 0.2,
) -> bool:
    """Delete the rollback copy only after the replacement client is running."""

    target = (executable or Path(sys.executable)).resolve()
    previous = target.with_name(target.name + ".previous")
    if not previous.exists():
        return False
    for attempt in range(1, max(attempts, 1) + 1):
        try:
            previous.unlink()
            logger.info("[CLIENT_UPDATE] previous client removed path=%s", previous)
            return True
        except FileNotFoundError:
            return False
        except OSError:
            if attempt >= max(attempts, 1):
                logger.warning(
                    "[CLIENT_UPDATE] previous client cleanup failed path=%s",
                    previous,
                    exc_info=True,
                )
                return False
            time.sleep(max(retry_delay_seconds, 0))
    return False


def _semantic_version(value: str) -> tuple[int, int, int]:
    normalized = value.strip().removeprefix("v")
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", normalized)
    if match is None:
        raise ClientUpdateError(f"客户端版本号无效: {value}")
    return tuple(int(item) for item in match.groups())  # type: ignore[return-value]


def _is_complete_executable(path: Path, expected_size: int) -> bool:
    try:
        _validate_executable(path, expected_size)
    except (OSError, ClientUpdateError):
        return False
    return True


def _validate_executable(path: Path, expected_size: int) -> None:
    if not path.is_file() or path.stat().st_size != expected_size:
        raise ClientUpdateError("客户端更新包文件大小不完整")
    with path.open("rb") as stream:
        if stream.read(2) != b"MZ":
            raise ClientUpdateError("客户端更新包不是有效的 Windows EXE")
        stream.seek(0x3C)
        raw_offset = stream.read(4)
        if len(raw_offset) != 4:
            raise ClientUpdateError("客户端更新包 PE 头不完整")
        offset = int.from_bytes(raw_offset, "little")
        if offset < 0x40 or offset > min(expected_size - 4, 16 * 1024 * 1024):
            raise ClientUpdateError("客户端更新包 PE 头偏移无效")
        stream.seek(offset)
        if stream.read(4) != b"PE\0\0":
            raise ClientUpdateError("客户端更新包 PE 签名无效")


def _wait_for_process(pid: int, timeout_seconds: int) -> bool:
    if os.name != "nt":
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                return True
            time.sleep(0.1)
        return False

    import ctypes

    synchronize = 0x00100000
    wait_object_0 = 0
    handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, pid)
    if not handle:
        return True
    try:
        result = ctypes.windll.kernel32.WaitForSingleObject(
            handle, timeout_seconds * 1000
        )
        return result == wait_object_0
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)
