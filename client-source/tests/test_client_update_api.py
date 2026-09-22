from __future__ import annotations

import pytest

from publisher.models import ClientRelease, ClientUpdateAsset
from services.client_update_api import ClientUpdateNotPublished, client_release_payload


def test_client_latest_payload_is_flattened_for_requested_variant() -> None:
    release = ClientRelease(
        latest_version="1.8.0",
        minimum_version="1.7.0",
        mandatory=False,
        release_notes=["修复更新"],
        published_at="2026-09-12T12:00:00Z",
        variants={
            "public": ClientUpdateAsset(
                download_url="https://example.test/public.exe",
                size=123,
                file_name="public.exe",
            ),
            "cos": ClientUpdateAsset(
                download_url="https://example.test/cos.exe",
                size=456,
                file_name="cos.exe",
            ),
        },
    )

    payload = client_release_payload(release, "cos")

    assert payload["version"] == "1.8.0"
    assert payload["download_url"].endswith("cos.exe")
    assert payload["size"] == 456
    assert "sha256" not in payload


def test_client_latest_payload_rejects_missing_release() -> None:
    with pytest.raises(ClientUpdateNotPublished):
        client_release_payload(None, "public")
