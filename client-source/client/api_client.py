from __future__ import annotations

import time
from typing import Any

import httpx

from .network import windows_system_proxy
from .download_sources import DownloadSourceManager


class ClientApiError(RuntimeError):
    pass


class CatalogApi:
    provider_name = "云端"
    prefer_direct = False

    def __init__(self, repository: str, *, timeout_seconds: int = 60) -> None:
        repository = repository.strip().strip("/")
        if repository.endswith(".git"):
            repository = repository[:-4]
        if len(repository.split("/")) != 2:
            raise ValueError(f"{self.provider_name} 仓库必须填写为 owner/repository")
        self.repository = repository
        self._client_options: dict[str, Any] = {
            "timeout": httpx.Timeout(timeout_seconds, connect=min(8, timeout_seconds)),
            "follow_redirects": True,
            "headers": {"User-Agent": "oc2-map-manager/1.4"},
        }
        self.system_proxy = windows_system_proxy()
        self.client = self._new_primary_client()

    def close(self) -> None:
        self.client.close()

    def fetch_catalog_bytes(self) -> bytes:
        raise NotImplementedError

    def download_bytes(self, url: str, *, maximum: int = 25 * 1024 * 1024) -> bytes:
        try:
            with self._new_primary_client() as preview_client:
                with preview_client.stream("GET", url) as response:
                    response.raise_for_status()
                    result = bytearray()
                    for chunk in response.iter_bytes():
                        result.extend(chunk)
                        if len(result) > maximum:
                            raise ClientApiError("预览文件超过大小限制")
                    return bytes(result)
        except httpx.HTTPError as exc:
            raise ClientApiError(f"资源下载失败: {type(exc).__name__}") from exc

    def _new_primary_client(self) -> httpx.Client:
        if self.prefer_direct:
            return httpx.Client(**self._client_options, trust_env=False)
        return httpx.Client(**self._client_options, proxy=self.system_proxy)

    def _get(self, url: str, **kwargs: Any) -> httpx.Response:
        """Use the preferred mainland route and one bounded fallback route."""

        try:
            return self.client.get(url, **kwargs)
        except httpx.HTTPError as primary_error:
            if self.prefer_direct and self.system_proxy:
                with httpx.Client(
                    **self._client_options, proxy=self.system_proxy
                ) as fallback:
                    return fallback.get(url, **kwargs)
            if not self.prefer_direct and self.system_proxy is not None:
                with httpx.Client(**self._client_options) as fallback:
                    return fallback.get(url, **kwargs)
            raise primary_error

    def _download_catalog(self, url: str) -> bytes:
        try:
            response = self._get(url)
            response.raise_for_status()
            return response.content
        except httpx.HTTPError as exc:
            raise ClientApiError(f"catalog 下载失败: {type(exc).__name__}") from exc


class GiteeCatalogApi(CatalogApi):
    provider_name = "Gitee"
    prefer_direct = True

    def fetch_catalog_bytes(self) -> bytes:
        releases_url = f"https://gitee.com/api/v5/repos/{self.repository}/releases"
        try:
            response = self._get(
                releases_url,
                params={"per_page": 10, "page": 1, "direction": "desc"},
            )
        except httpx.HTTPError as exc:
            raise ClientApiError(f"连接 Gitee 失败: {type(exc).__name__}") from exc
        if response.status_code == 404:
            raise ClientApiError("Gitee 地图仓库不存在或还没有 Release")
        if response.status_code >= 400:
            raise ClientApiError(f"Gitee API 返回 HTTP {response.status_code}")
        try:
            releases = response.json()
        except ValueError as exc:
            raise ClientApiError("Gitee Release 响应不是 JSON") from exc
        if not isinstance(releases, list):
            raise ClientApiError("Gitee Release 响应格式无效")

        # Gitee user attachments live behind a separate endpoint. Searching a few
        # newest releases also keeps the previous catalog usable during an upload.
        for release in releases:
            if not isinstance(release, dict) or not release.get("id"):
                continue
            attachments_url = (
                f"https://gitee.com/api/v5/repos/{self.repository}/releases/"
                f"{release['id']}/attach_files"
            )
            try:
                attachments_response = self._get(
                    attachments_url, params={"per_page": 100, "direction": "desc"}
                )
            except httpx.HTTPError:
                continue
            if attachments_response.status_code >= 400:
                continue
            try:
                attachments = attachments_response.json()
            except ValueError:
                continue
            if not isinstance(attachments, list):
                continue
            asset = next(
                (
                    item
                    for item in attachments
                    if isinstance(item, dict)
                    and (item.get("name") or item.get("file_name")) == "catalog.json"
                ),
                None,
            )
            if asset is None:
                continue
            download_url = str(asset.get("browser_download_url") or "")
            if not download_url and asset.get("id"):
                download_url = f"{attachments_url}/{asset['id']}/download"
            if download_url:
                return self._download_catalog(download_url)
        raise ClientApiError("最近的 Gitee Release 中没有完整的 catalog.json")


class GitHubCatalogApi(CatalogApi):
    provider_name = "GitHub"

    def __init__(self, repository: str, *, timeout_seconds: int = 60) -> None:
        super().__init__(repository, timeout_seconds=timeout_seconds)
        self.sources = DownloadSourceManager()
        self.client.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )

    def fetch_catalog_bytes(self) -> bytes:
        manifest_url = (
            f"https://github.com/{self.repository}/releases/latest/download/manifest.json"
            f"?oc2_refresh={int(time.time() // 60)}"
        )
        errors: list[str] = []
        for source_name, candidate_url in self.sources.candidates(manifest_url):
            try:
                response = self._get(candidate_url)
                if response.status_code == 200:
                    return response.content
                errors.append(f"{source_name}: HTTP {response.status_code}")
            except httpx.HTTPError as exc:
                errors.append(f"{source_name}: {type(exc).__name__}")

        # Compatibility with releases created by versions before manifest.json.
        url = f"https://api.github.com/repos/{self.repository}/releases/latest"
        try:
            response = self._get(url)
        except httpx.HTTPError as exc:
            raise ClientApiError(f"连接 GitHub 失败: {type(exc).__name__}") from exc
        if response.status_code == 403 and response.headers.get("x-ratelimit-remaining") == "0":
            raise ClientApiError("GitHub 匿名 API 额度已用完，请稍后重试")
        if response.status_code == 404:
            raise ClientApiError("地图仓库还没有已发布的 Release")
        if response.status_code >= 400:
            raise ClientApiError(f"GitHub API 返回 HTTP {response.status_code}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise ClientApiError("GitHub Release 响应格式无效")
        assets = payload.get("assets")
        if not isinstance(assets, list):
            raise ClientApiError("GitHub Release 缺少 assets")
        asset = next(
            (
                item
                for preferred in ("manifest.json", "catalog.json")
                for item in assets
                if isinstance(item, dict) and item.get("name") == preferred
            ),
            None,
        )
        if asset is None:
            detail = "; ".join(errors)
            raise ClientApiError(f"最新 Release 缺少 manifest.json/catalog.json；{detail}")
        download_url = str(asset.get("browser_download_url") or "")
        if not download_url:
            raise ClientApiError("catalog asset 缺少下载地址")
        return self._download_catalog(download_url)


def create_catalog_api(
    provider: str, repository: str, *, timeout_seconds: int = 60
) -> CatalogApi:
    normalized = provider.strip().casefold()
    if normalized == "gitee":
        return GiteeCatalogApi(repository, timeout_seconds=timeout_seconds)
    if normalized == "github":
        return GitHubCatalogApi(repository, timeout_seconds=timeout_seconds)
    raise ValueError(f"不支持的云端类型: {provider}")
