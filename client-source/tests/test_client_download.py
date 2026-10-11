from __future__ import annotations

import hashlib
import io
import re
import zipfile
from pathlib import Path

import httpx
import pytest

from client.download_manager import DownloadError, DownloadManager, _Candidate
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


def test_existing_partial_file_is_resumed_with_http_range(tmp_path: Path) -> None:
    payload = zip_payload(b"resume payload" * 1000)
    item = metadata(payload)
    partial_size = len(payload) // 3
    part = tmp_path / "downloads" / f"{item.id}_{item.version}.zip.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(payload[:partial_size])
    ranges = []

    def handler(request: httpx.Request) -> httpx.Response:
        ranges.append(request.headers.get("range"))
        return httpx.Response(
            206,
            content=payload[partial_size:],
            headers={
                "Content-Length": str(len(payload) - partial_size),
                "Content-Range": (
                    f"bytes {partial_size}-{len(payload) - 1}/{len(payload)}"
                ),
            },
        )

    manager = DownloadManager(
        tmp_path,
        retries=1,
        transport=httpx.MockTransport(handler),
        source_manager=_sources(tmp_path),
    )
    assert manager.download(item).read_bytes() == payload
    assert ranges == [f"bytes={partial_size}-"]


def test_probe_results_can_move_github_direct_ahead_of_slow_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = DownloadManager(tmp_path, source_manager=_sources(tmp_path))
    candidates = [
        _Candidate("proxy", "https://proxy.test/map.zip", False),
        _Candidate("GitHub Direct", "https://github.com/map.zip", False),
    ]

    def probe(candidate, expected_size):
        del expected_size
        return ((8 if candidate.name == "GitHub Direct" else 1) * 1024 * 1024, True)

    monkeypatch.setattr(manager, "_probe_candidate", probe)
    ranked = manager._rank_candidates(candidates, 20 * 1024 * 1024)
    assert [item.name for item in ranked] == ["GitHub Direct", "proxy"]


def test_large_range_download_uses_four_resumable_segments(tmp_path: Path) -> None:
    payload = zip_payload(b"x" * (17 * 1024 * 1024))
    ranges = []

    def handler(request: httpx.Request) -> httpx.Response:
        value = request.headers["range"]
        ranges.append(value)
        match = re.fullmatch(r"bytes=(\d+)-(\d+)", value)
        assert match is not None
        start, end = (int(part) for part in match.groups())
        return httpx.Response(
            206,
            content=payload[start : end + 1],
            headers={
                "Content-Length": str(end - start + 1),
                "Content-Range": f"bytes {start}-{end}/{len(payload)}",
            },
        )

    manager = DownloadManager(
        tmp_path,
        transport=httpx.MockTransport(handler),
    )
    part = tmp_path / "downloads" / "parallel.zip.part"
    manager._download_segmented(
        asset_id="parallel-test",
        candidate=_Candidate(
            "range-source", "https://range.test/map.zip", False, True
        ),
        expected_size=len(payload),
        part=part,
        progress=None,
    )

    assert part.read_bytes() == payload
    assert len(ranges) == 4
