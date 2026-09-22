from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

import httpx
import pytest

from client.download_manager import DownloadError, DownloadManager
from client.download_sources import DownloadSourceManager
from publisher.models import MapMetadata


def metadata(payload: bytes, *, sha256: str | None = None) -> MapMetadata:
    return MapMetadata(
        id="map-123456789abc",
        name="Map",
        author="Chef",
        version="1.0",
        description="",
        preview_url="https://example.test/preview.webp",
        download_url="https://example.test/map.zip",
        file_name="map.zip",
        size=len(payload),
        sha256=sha256 or hashlib.sha256(payload).hexdigest(),
        updated_at="2026-09-05T00:00:00Z",
        map_key="map",
    )


def zip_payload(payload: bytes = b"map") -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("map/info_map", payload)
    return output.getvalue()


def test_download_progress_and_zip_integrity(tmp_path: Path) -> None:
    payload = zip_payload(b"zip payload" * 1000)
    manager = DownloadManager(
        tmp_path,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                content=payload,
                headers={"Content-Length": str(len(payload))},
            )
        ),
    )
    progress = []
    path = manager.download(metadata(payload), progress.append)
    assert path.read_bytes() == payload
    assert progress[-1].downloaded == len(payload)
    assert not path.with_suffix(".zip.part").exists()


def test_invalid_zip_is_deleted_and_never_installed(tmp_path: Path) -> None:
    payload = b"corrupt"
    manager = DownloadManager(
        tmp_path,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=payload)),
    )
    with pytest.raises(DownloadError, match="所有下载源均失败"):
        manager.download(metadata(payload, sha256="a" * 64))
    assert not list((tmp_path / "downloads").glob("*.zip*"))


def _sources(tmp_path: Path) -> DownloadSourceManager:
    path = tmp_path / "proxy_sources.json"
    path.write_text(
        '[{"name":"proxy","urlTemplate":"https://proxy.test/{url}","enabled":true},'
        '{"name":"GitHub Direct","urlTemplate":"{url}","enabled":true}]',
        encoding="utf-8",
    )
    return DownloadSourceManager(path)


def test_default_download_sources_follow_measured_mainland_order() -> None:
    manager = DownloadSourceManager()
    names = [
        name
        for name, _ in manager.candidates(
            "https://github.com/owner/maps/releases/download/v1/map.zip"
        )
    ]
    assert names == ["ghfast.top", "ghproxy.net", "GitHub Direct"]


def test_failed_proxy_download_switches_to_direct(tmp_path: Path) -> None:
    payload = zip_payload(b"valid zip bytes")
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        if request.url.host == "proxy.test":
            return httpx.Response(503)
        return httpx.Response(200, content=payload)

    item = metadata(payload)
    item.download_url = "https://github.com/owner/maps/releases/download/v1/map.zip"
    manager = DownloadManager(
        tmp_path,
        retries=1,
        transport=httpx.MockTransport(handler),
        source_manager=_sources(tmp_path),
    )
    assert manager.download(item).read_bytes() == payload
    assert seen == ["proxy.test", "github.com"]


def test_broken_zip_on_proxy_switches_to_direct(tmp_path: Path) -> None:
    payload = zip_payload(b"correct payload")
    corrupt = b"NOPE" + payload[4:]

    def handler(request: httpx.Request) -> httpx.Response:
        body = corrupt if request.url.host == "proxy.test" else payload
        return httpx.Response(200, content=body)

    item = metadata(payload)
    item.download_url = "https://github.com/owner/maps/releases/download/v1/map.zip"
    manager = DownloadManager(
        tmp_path,
        retries=1,
        transport=httpx.MockTransport(handler),
        source_manager=_sources(tmp_path),
    )
    assert manager.download(item).read_bytes() == payload


def test_different_complete_zip_is_not_rejected_by_hash(tmp_path: Path) -> None:
    payload = zip_payload(b"correct payload")
    different = zip_payload(b"wrong__payload!")
    assert len(payload) == len(different)
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        return httpx.Response(200, content=different)

    item = metadata(payload)
    item.download_url = "https://github.com/owner/maps/releases/download/v1/map.zip"
    manager = DownloadManager(
        tmp_path,
        retries=1,
        transport=httpx.MockTransport(handler),
        source_manager=_sources(tmp_path),
    )
    assert manager.download(item).read_bytes() == different
    assert seen == ["proxy.test"]


def test_cos_is_first_and_failure_falls_back_to_existing_sources(tmp_path: Path) -> None:
    payload = zip_payload(b"valid cos fallback")
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        if request.url.host == "cos.example.test":
            return httpx.Response(503)
        return httpx.Response(200, content=payload)

    item = metadata(payload)
    item.cos_url = "https://cos.example.test/maps/map.zip"
    item.download_url = "https://github.com/owner/maps/releases/download/v1/map.zip"
    manager = DownloadManager(
        tmp_path,
        retries=1,
        transport=httpx.MockTransport(handler),
        source_manager=_sources(tmp_path),
    )
    assert manager.download(item).read_bytes() == payload
    assert seen == ["cos.example.test", "proxy.test"]


def test_broken_cos_zip_is_deleted_then_github_source_is_used(tmp_path: Path) -> None:
    payload = zip_payload(b"correct payload")
    corrupt = b"NOPE" + payload[4:]
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        body = corrupt if request.url.host == "cos.example.test" else payload
        return httpx.Response(200, content=body)

    item = metadata(payload)
    item.cos_url = "https://cos.example.test/maps/map.zip"
    item.download_url = "https://github.com/owner/maps/releases/download/v1/map.zip"
    manager = DownloadManager(
        tmp_path,
        retries=1,
        transport=httpx.MockTransport(handler),
        source_manager=_sources(tmp_path),
    )
    assert manager.download(item).read_bytes() == payload
    assert seen == ["cos.example.test", "proxy.test"]


def test_public_client_ignores_cos_url(tmp_path: Path) -> None:
    payload = zip_payload(b"public client payload")
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        return httpx.Response(200, content=payload)

    item = metadata(payload)
    item.cos_url = "https://cos.example.test/maps/map.zip"
    item.download_url = "https://github.com/owner/maps/releases/download/v1/map.zip"
    manager = DownloadManager(
        tmp_path,
        retries=1,
        transport=httpx.MockTransport(handler),
        source_manager=_sources(tmp_path),
        cos_enabled=False,
    )
    assert manager.download(item).read_bytes() == payload
    assert seen == ["proxy.test"]
