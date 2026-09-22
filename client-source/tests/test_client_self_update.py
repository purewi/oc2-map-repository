from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from client.self_update import (
    ClientSelfUpdater,
    ClientUpdateError,
    ClientUpdateInfo,
    apply_client_update,
    cleanup_previous_client,
    compare_client_versions,
    resolve_client_update,
)
from client.download_sources import DownloadSourceManager
from publisher.models import ClientRelease, ClientUpdateAsset


def pe_payload(size: int = 512, marker: bytes = b"") -> bytes:
    payload = bytearray(b"\0" * size)
    payload[0:2] = b"MZ"
    payload[0x3C:0x40] = (0x80).to_bytes(4, "little")
    payload[0x80:0x84] = b"PE\0\0"
    payload[0x100 : 0x100 + len(marker)] = marker
    return bytes(payload)


def release(*, mandatory: bool = False, minimum: str | None = None) -> ClientRelease:
    return ClientRelease(
        latest_version="1.10.0",
        minimum_version=minimum,
        mandatory=mandatory,
        release_notes=["修复下载问题"],
        variants={
            "public": ClientUpdateAsset(
                download_url="https://github.com/owner/repo/releases/download/v/client.exe",
                size=512,
                file_name="client.exe",
            )
        },
    )


def test_client_semver_comparison_is_numeric() -> None:
    assert compare_client_versions("1.10.0", "1.9.0") > 0
    assert compare_client_versions("2.0.0", "1.99.99") > 0
    with pytest.raises(ClientUpdateError):
        compare_client_versions("1.2", "1.2.0")


def test_resolve_update_uses_variant_and_minimum_version() -> None:
    info = resolve_client_update(
        release(minimum="1.8.0"), variant="public", local_version="1.7.0"
    )
    assert info is not None
    assert info.version == "1.10.0"
    assert info.mandatory
    assert info.release_notes == ("修复下载问题",)
    assert resolve_client_update(
        release(), variant="public", local_version="1.10.0"
    ) is None


def test_client_update_uses_size_and_pe_integrity_without_hash(tmp_path: Path) -> None:
    valid = pe_payload(marker=b"new")
    invalid = b"NO" + valid[2:]
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        content = invalid if request.url.host == "proxy.test" else valid
        return httpx.Response(200, content=content)

    sources_file = tmp_path / "proxy_sources.json"
    sources_file.write_text(
        '[{"name":"proxy","urlTemplate":"https://proxy.test/{url}","enabled":true},'
        '{"name":"GitHub Direct","urlTemplate":"{url}","enabled":true}]',
        encoding="utf-8",
    )
    updater = ClientSelfUpdater(
        tmp_path,
        retries=1,
        transport=httpx.MockTransport(handler),
        source_manager=DownloadSourceManager(sources_file),
    )
    info = ClientUpdateInfo(
        version="1.8.0",
        download_url="https://github.com/owner/repo/releases/download/v/client.exe",
        cos_url=None,
        size=len(valid),
        file_name="client.exe",
        mandatory=False,
        release_notes=(),
    )
    assert updater.download(info).read_bytes() == valid
    assert seen == ["proxy.test", "github.com"]


def test_apply_client_update_keeps_previous_copy(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "downloaded.exe"
    target = tmp_path / "OC2MapManager.exe"
    source.write_bytes(pe_payload(marker=b"new"))
    old = pe_payload(marker=b"old")
    target.write_bytes(old)
    launches = []
    monkeypatch.setattr("client.self_update._wait_for_process", lambda *_: True)
    monkeypatch.setattr(
        "client.self_update.subprocess.Popen",
        lambda args, **kwargs: launches.append((args, kwargs)),
    )

    assert apply_client_update(
        source=source,
        target=target,
        old_pid=123,
        expected_size=source.stat().st_size,
    ) == 0
    assert target.read_bytes() == source.read_bytes()
    assert target.with_name(target.name + ".previous").read_bytes() == old
    assert launches[0][0][0] == str(target)


def test_apply_failure_restores_previous_client(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "downloaded.exe"
    target = tmp_path / "OC2MapManager.exe"
    source.write_bytes(pe_payload(marker=b"new"))
    old = pe_payload(marker=b"old")
    target.write_bytes(old)
    monkeypatch.setattr("client.self_update._wait_for_process", lambda *_: True)
    monkeypatch.setattr(
        "client.self_update.subprocess.Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("launch failed")),
    )

    with pytest.raises(ClientUpdateError, match="已尝试恢复旧版本"):
        apply_client_update(
            source=source,
            target=target,
            old_pid=123,
            expected_size=source.stat().st_size,
        )
    assert target.read_bytes() == old


def test_apply_falls_back_to_direct_copy_when_atomic_replace_is_denied(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "downloaded.exe"
    target = tmp_path / "OC2MapManager.exe"
    source.write_bytes(pe_payload(marker=b"new"))
    target.write_bytes(pe_payload(marker=b"old"))
    real_replace = __import__("os").replace
    launches = []

    def deny_target_replace(source_path, destination_path):
        if Path(destination_path) == target:
            raise PermissionError(5, "denied")
        return real_replace(source_path, destination_path)

    monkeypatch.setattr("client.self_update._wait_for_old_processes", lambda *_: True)
    monkeypatch.setattr("client.self_update.os.replace", deny_target_replace)
    monkeypatch.setattr(
        "client.self_update.subprocess.Popen",
        lambda args, **kwargs: launches.append((args, kwargs)),
    )

    assert apply_client_update(
        source=source,
        target=target,
        old_pid=123,
        expected_size=source.stat().st_size,
    ) == 0
    assert target.read_bytes() == source.read_bytes()
    assert not target.with_name(target.name + ".previous.tmp").exists()
    assert launches[0][0][0] == str(target)


def test_apply_relocates_when_original_directory_cannot_be_written(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "downloaded.exe"
    target = tmp_path / "wechat" / "OC2MapManager-Public-v1.8.0-win64.exe"
    relocated = tmp_path / "app" / "OC2MapManager-Public.exe"
    source.write_bytes(pe_payload(marker=b"new"))
    target.parent.mkdir()
    old = pe_payload(marker=b"old")
    target.write_bytes(old)
    launches = []
    monkeypatch.setattr("client.self_update._wait_for_old_processes", lambda *_: True)
    monkeypatch.setattr(
        "client.self_update._install_in_place",
        lambda *_: (_ for _ in ()).throw(PermissionError(5, "denied")),
    )
    monkeypatch.setattr("client.self_update._relocated_target", lambda _: relocated)
    monkeypatch.setattr(
        "client.self_update.subprocess.Popen",
        lambda args, **kwargs: launches.append((args, kwargs)),
    )

    assert apply_client_update(
        source=source,
        target=target,
        old_pid=123,
        expected_size=source.stat().st_size,
    ) == 0
    assert target.read_bytes() == old
    assert relocated.read_bytes() == source.read_bytes()
    assert launches[0][0] == [
        str(relocated),
        "--client-update-finished",
        "--client-update-relocated",
    ]


def test_cleanup_previous_client_after_successful_restart(tmp_path: Path) -> None:
    executable = tmp_path / "OC2MapManager.exe"
    previous = tmp_path / "OC2MapManager.exe.previous"
    executable.write_bytes(pe_payload(marker=b"new"))
    previous.write_bytes(pe_payload(marker=b"old"))

    assert cleanup_previous_client(executable, retry_delay_seconds=0)
    assert not previous.exists()
    assert not cleanup_previous_client(executable, retry_delay_seconds=0)
