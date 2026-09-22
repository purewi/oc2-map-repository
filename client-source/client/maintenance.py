"""Read-only comparison of cloud inventory and the bot's local evidence."""
from __future__ import annotations

import json
import re
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath

from publisher.versioning import compare_versions
from publisher.models import Catalog
from .catalog_service import CatalogService
from .config import ClientConfig, app_data_directory


# User-supplied reference table, not a claim about today's live author versions.
REFERENCE = {
    "blni": ("小no", "no", "2.1"), "choubao": ("抽宝", "O", "0.1"),
    "dtsimple": ("simple", "DarkTemplar", "4.1"), "fruit": ("果果泡泡趴", "FruitCuddled", "2.0"),
    "grand_chefs": ("举子的晚宴", "特级", "0.5"), "hhm": ("胡乱盛宴", "呼呼喵", "0.17"),
    "jonny": ("职业鼠鼠的测图", "Jonny", "0.32"), "ha": ("HaHa餐厅", "littleHa", "0.6.1"),
    "the_constant": ("Overstarved", "派＆毛＆嘉", "1.3"), "patrick": ("生的派大星关卡集", "Patrick", "7.4"),
    "pinji": ("贫瘠的美食街", "贫瘠地", "0.4"), "qichi": ("七池的地图系列", "七池", "0.5.6"),
    "qie": ("切切的关卡集", "qieqie", "0.2"), "rw": ("RW_关卡合集", "RW", "0.6"),
    "shz_custommaps": ("霍格华夫", "SHZ", "0.3.5"), "wenr": ("小闻食光", "WenRen", "0.8"),
    "yl": ("夜凉作坊", "夜凉", "0.2"), "xia": ("夏", "不辑", "0.1"),
    "dmm": ("呆喵喵小厨房", "呆喵喵", "0.6.2"), "tianxin": ("天心", "天心", "1.4"),
    "chan": ("婵的美食铺", "chanchan", "0.1.0"), "ark": ("小刻食堂", "伪人君", "0.2"),
    "squirrel_kitchen": ("小松鼠厨房", "1", "0.1"), "gaoda": ("胡闹糕中", "GAODA", "1.8.1"),
    "daxue": ("胡闹大学", "GAODA", "0.3.1"), "li": ("菊花梨的关卡集", "JuHuaLi", "1.2"),
}

# The catalog uses the map's real internal key, which does not always match the
# short key used in the maintainer's original checklist.
REFERENCE_KEY_ALIASES = {
    "qieqie": "qie",
    "newtianxin": "tianxin",
}


# Fixed sources supplied by the maintainer.  These live in the maintenance
# settings after the first launch, so every field remains user-editable and a
# deleted entry is not silently recreated.
DEFAULT_EXTERNAL_AUTHORS = {
    "baidu-uywv": {
        "author": "待确认（网盘用户 139*****101）",
        "map_name": "待首次确认",
        "channel": "百度网盘",
        "url": "https://pan.baidu.com/s/1cU36rTufqIIJK9XrFH0BDw?pwd=uywv",
        "current_version": "",
        "last_checked": "",
        "interval_days": "14",
        "note": "固定外部发布源；首次打开后补全作者、地图和版本。",
    },
    "baidu-86it": {
        "author": "待确认（网盘用户 阿伪***猫猫）",
        "map_name": "待首次确认",
        "channel": "百度网盘",
        "url": "https://pan.baidu.com/s/1Nvn6Vh_5hoJx-5XJiK0MAA?pwd=86it#list/path=%2F",
        "current_version": "",
        "last_checked": "",
        "interval_days": "14",
        "note": "固定外部发布源；公开页面无法读取文件名，首次打开后补全。",
    },
    "mediafire-dtsimple": {
        "author": "DarkTemplar",
        "map_name": "simple / DTsimple",
        "channel": "MediaFire",
        "url": "https://www.mediafire.com/file/rct4rtvrdkwg79o/DTsimple.zip/file",
        "current_version": "4.1",
        "last_checked": "",
        "interval_days": "14",
        "note": "页面标题已确认为 DTsimple；版本号可在每次检查后手工更新。",
    },
}


def package_version(name: str) -> str:
    found = re.findall(r"(?<!\d)(\d+(?:[._-]\d+){1,3})(?!\d)", name)
    return found[-1].replace("_", ".").replace("-", ".") if found else ""


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def settings_path() -> Path:
    return app_data_directory() / "maintenance-settings.json"


def load_settings() -> dict:
    value = _read_json(settings_path())
    value.setdefault("server_directory", "D:/codex/testspace/mapbot")
    value.setdefault("notes", {})
    if "external_authors" not in value:
        # JSON round-trip gives the settings their own copy.
        value["external_authors"] = json.loads(json.dumps(DEFAULT_EXTERNAL_AUTHORS, ensure_ascii=False))
    elif not isinstance(value["external_authors"], dict):
        value["external_authors"] = {}
    return value


def snapshot(config: ClientConfig, settings: dict) -> dict:
    # The ordinary client may be open alongside this window; isolate atomic
    # catalog-cache writes so the two processes do not share the same .tmp file.
    service = CatalogService(config.model_copy(update={"cache_directory": config.cache_directory / "maintenance"}))
    cloud_error = ""
    try:
        try:
            catalog, cached = service.refresh()
        except Exception as exc:
            cloud_error = f"云端暂不可访问：{exc}"
            # First launch can reuse the ordinary client's last verified catalog.
            fallback = config.cache_directory / f"catalog-{config.cloud_provider}.json"
            try:
                catalog = Catalog.model_validate_json(fallback.read_bytes())
            except (OSError, ValueError):
                catalog = Catalog()
            cached = True
    finally:
        service.close()
    root = Path(settings["server_directory"])
    database = root / "data/maps.db"
    records, scans = [], []
    database_error = ""
    try:
        with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=5) as connection:
            connection.row_factory = sqlite3.Row
            records = [dict(x) for x in connection.execute(
                "SELECT id,file_name,status,last_error,archive_path,sha256,group_id,qq_upload_time,completed_at FROM map_files ORDER BY id DESC"
            )]
            scans = [dict(x) for x in connection.execute("SELECT name,last_scan_at FROM groups")]
    except sqlite3.Error as exc:
        database_error = f"无法读取服务端数据库：{exc}"

    index_path = app_data_directory() / "maintenance-archive-index.json"
    index = _read_json(index_path)
    by_key: dict[str, list[dict]] = {}
    for record in records:
        path = Path(record["archive_path"]) if record["archive_path"] else None
        keys = []
        if path and path.is_file():
            stat = path.stat()
            signature = f"{stat.st_size}:{stat.st_mtime_ns}"
            previous = index.get(str(path), {})
            if previous.get("signature") == signature:
                keys = previous.get("keys", [])
            else:
                try:
                    with zipfile.ZipFile(path) as archive:
                        keys = list({PurePosixPath(n.replace('\\', '/')).name[5:].casefold()
                                     for n in archive.namelist()
                                     if PurePosixPath(n.replace('\\', '/')).name.casefold().startswith('info_')})
                except (OSError, zipfile.BadZipFile):
                    pass
                index[str(path)] = {"signature": signature, "keys": keys}
        record["package_version"] = package_version(record["file_name"])
        for key in keys:
            by_key.setdefault(key, []).append(record)
    save_json(index_path, index)
    rows = []
    covered = set()
    for metadata in catalog.maps:
        identity_keys = [metadata.map_key or metadata.id, *metadata.previous_map_keys]
        canonical_keys = [REFERENCE_KEY_ALIASES.get(key, key) for key in identity_keys]
        key = next((key for key in canonical_keys if key in REFERENCE), canonical_keys[0])
        covered.update(canonical_keys)
        lookup_keys = set([*identity_keys, *canonical_keys])
        related = [record for lookup in lookup_keys for record in by_key.get(lookup, [])]
        rows.append(_row(key, metadata.name, metadata.author, metadata, related, settings))
    for key, (name, author, _) in REFERENCE.items():
        if key not in covered:
            rows.append(_row(key, name, author, None, by_key.get(key, []), settings))
    if cached:
        for row in rows:
            if row["status"] == "未收录":
                row["status"] = "缓存未收录 · 云端待核实"
            elif row["status"] == "已同步":
                row["status"] = "与缓存一致 · 云端待核实"
    # Missing/unreadable archives still appear as evidence, never as confirmed identity.
    problems = [r for r in records if r["status"] in {"FAILED", "FAILED_INTERRUPTED", "INVALID"}]
    for row in rows:
        row["hints"] = [r for r in problems if _filename_matches(r["file_name"], row["key"], row["name"])]
        if not row["observed_file"] and row["hints"]:
            hint = row["hints"][0]
            row["observed_file"] = hint["file_name"]
            row["record_status"] = hint["status"]
            row["note"] = (row["note"] + "；文件名疑似关联：" + (hint["last_error"] or "待核对")).strip("；")
    rows.sort(key=lambda r: (r["status"] == "已同步", r["author"].casefold(), r["name"]))
    return {"rows": rows, "problems": problems, "cached": cached, "database_error": database_error, "cloud_error": cloud_error,
            "scans": scans, "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "cloud_maps": len(catalog.maps), "records": len(records)}


def _filename_matches(filename: str, key: str, name: str) -> bool:
    if len(name) > 1 and name.casefold() in filename.casefold():
        return True
    return bool(re.search(r"(?<![a-z])" + re.escape(key) + r"(?![a-z])", filename.casefold()))


def _row(key, name, author, metadata, related, settings):
    # Filename versions are evidence to review, not guaranteed author metadata.
    # Prefer the most recent successfully processed group package.  Sorting by
    # its version text made an old test package named "天心2.0" permanently
    # outrank the author's later real 1.4 rollback/update.
    related.sort(
        key=lambda item: (
            item.get("status") == "COMPLETED",
            item.get("qq_upload_time") or 0,
            item.get("id") or 0,
        ),
        reverse=True,
    )
    observed = related[0] if related else None
    notes = settings["notes"].get(key, {})
    if not notes:
        notes = next(
            (
                settings["notes"][alias]
                for alias, canonical in REFERENCE_KEY_ALIASES.items()
                if canonical == key and alias in settings["notes"]
            ),
            {},
        )
    expected = notes.get("expected", REFERENCE.get(key, ("", "", ""))[2])
    embedded = metadata.embedded_version or "" if metadata else ""
    cloud = metadata.source_version or embedded or metadata.version if metadata else ""
    status = "未收录" if metadata is None else "未见群记录"
    note = notes.get("note", "")
    if metadata:
        if metadata.availability_notice:
            status = "新版暂不提供"
            note = metadata.availability_notice + ("；" + note if note else "")
        elif observed and observed["sha256"] == metadata.sha256:
            status = "已同步"
        elif observed and observed["package_version"] and compare_versions(observed["package_version"], cloud) > 0:
            status = "疑似漏更（群包版本）"
        elif observed:
            status = "内容不同待核对"
        if expected and compare_versions(expected, cloud) > 0 and not metadata.availability_notice:
            status = "参考版本高于云端"
    return {"key": key, "name": name, "author": author, "cloud": cloud, "embedded": embedded,
            "expected": expected, "observed": observed["package_version"] if observed else "",
            "observed_file": observed["file_name"] if observed else "", "cloud_file": metadata.file_name if metadata else "",
            "record_status": observed["status"] if observed else "", "record_id": observed["id"] if observed else "",
            "status": status, "note": note, "download_url": metadata.download_url if metadata else ""}
