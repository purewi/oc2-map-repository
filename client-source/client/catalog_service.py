from __future__ import annotations

import os
import logging
import re
import hashlib
from pathlib import Path

from publisher.models import Catalog, MapMetadata

from .api_client import ClientApiError, create_catalog_api
from .config import ClientConfig


logger = logging.getLogger(__name__)


class CatalogService:
    def __init__(self, config: ClientConfig) -> None:
        self.config = config
        self.api = create_catalog_api(
            config.cloud_provider,
            config.cloud_repository,
            timeout_seconds=config.timeout_seconds,
        )
        self.cache_path = config.cache_directory / f"catalog-{config.cloud_provider}.json"

    def close(self) -> None:
        self.api.close()

    def refresh(self, *, allow_cached: bool = True) -> tuple[Catalog, bool]:
        try:
            payload = self.api.fetch_catalog_bytes()
            catalog = Catalog.model_validate_json(payload)
            if catalog.schema_version != 1:
                raise ClientApiError(f"不支持 catalog schema {catalog.schema_version}")
            temporary = self.cache_path.with_suffix(".json.tmp")
            temporary.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_bytes(payload)
            os.replace(temporary, self.cache_path)
            logger.info("catalog 下载完成 maps=%s", len(catalog.maps))
            return catalog, False
        except Exception:
            if allow_cached and self.cache_path.is_file():
                logger.warning("catalog 网络更新失败，使用本地缓存")
                return Catalog.model_validate_json(
                    self.cache_path.read_text(encoding="utf-8")
                ), True
            raise

    def preview_path(self, metadata: MapMetadata) -> Path:
        version = re.sub(r"[^A-Za-z0-9._-]+", "-", metadata.version)[:80] or "unknown"
        revision = hashlib.sha256(metadata.preview_url.encode("utf-8")).hexdigest()[:10]
        return (
            self.config.cache_directory
            / "previews"
            / f"{metadata.id}_{version}_{revision}_full.webp"
        )

    def thumbnail_path(self, metadata: MapMetadata) -> Path:
        version = re.sub(r"[^A-Za-z0-9._-]+", "-", metadata.version)[:80] or "unknown"
        url = metadata.thumbnail_url or metadata.preview_url
        revision = hashlib.sha256(url.encode("utf-8")).hexdigest()[:10]
        return (
            self.config.cache_directory
            / "previews"
            / f"{metadata.id}_{version}_{revision}_thumb.webp"
        )

    def ensure_preview(self, metadata: MapMetadata) -> Path:
        path = self.preview_path(metadata)
        if path.is_file() and path.stat().st_size > 0:
            return path
        payload = self.api.download_bytes(metadata.preview_url)
        temporary = path.with_suffix(".webp.tmp")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_bytes(payload)
        os.replace(temporary, path)
        return path

    def ensure_thumbnail(self, metadata: MapMetadata) -> Path:
        path = self.thumbnail_path(metadata)
        if path.is_file() and path.stat().st_size > 0:
            return path
        payload = self.api.download_bytes(
            metadata.thumbnail_url or metadata.preview_url
        )
        temporary = path.with_suffix(".webp.tmp")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_bytes(payload)
        os.replace(temporary, path)
        return path
