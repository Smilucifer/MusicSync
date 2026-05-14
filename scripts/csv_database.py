"""CSV song identity database — CRUD for csv/song_mappings.csv."""
import csv
import os
import io
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


COLUMNS = [
    "netease_id", "qq_id",
    "name", "artist", "album",
    "ne_name", "ne_artist",
    "qq_name", "qq_artist",
    "match_source", "synced",
]
VALID_SOURCES = {"manual", "isrc", "name_artist", "unmatched", "qq_only"}


def get_csv_path() -> Path:
    return Path(os.path.dirname(__file__)).parent / "csv" / "song_mappings.csv"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _empties() -> dict:
    """Return a row template with all empty strings."""
    return {col: "" for col in COLUMNS}


def load_mappings() -> dict:
    """Read CSV, return dict keyed by netease_id (empty string key for QQ-only rows).

    Also builds an in-memory qq_id → netease_id index on the returned dict
    as ``_qq_index`` for O(1) lookup.
    """
    path = get_csv_path()
    if not path.exists():
        return {"_qq_index": {}}

    rows = {}
    qq_index = {}
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ne_id = row.get("netease_id", "").strip()
            qq_id = row.get("qq_id", "").strip()
            # Use synthetic key for QQ-only rows (empty netease_id)
            key = ne_id if ne_id else (f"qq_{qq_id}" if qq_id else "")
            if not key:
                continue
            rows[key] = row
            if qq_id:
                qq_index[qq_id] = key

    rows["_qq_index"] = qq_index
    return rows


def lookup_by_ne(mappings: dict, ne_id: str) -> Optional[dict]:
    return mappings.get(ne_id)


def lookup_by_qq(mappings: dict, qq_id: str) -> Optional[str]:
    """Return netease_id for a given QQ ID, or None."""
    return mappings.get("_qq_index", {}).get(qq_id)


def get_synced_ne_ids(mappings: dict) -> set[str]:
    """Return netease_id set for rows already synced to QQ Music (synced=1)."""
    return {
        ne_id for ne_id, row in mappings.items()
        if ne_id and not ne_id.startswith("_")
        and row.get("synced", "") == "1"
    }


def get_pending_manual_ids(mappings: dict) -> set[str]:
    """Return netease_id set that are matched (manual/isrc/name_artist) but not yet synced."""
    return {
        ne_id for ne_id, row in mappings.items()
        if ne_id and not ne_id.startswith("_")
        and row.get("match_source", "") in ("manual", "isrc", "name_artist")
        and row.get("synced", "") != "1"
        and row.get("qq_id", "")
    }


def needs_match(mappings: dict, ne_id: str) -> bool:
    """True if this netease_id row needs (re-)matching — no qq_id and not synced."""
    row = mappings.get(ne_id)
    if not row:
        return True
    has_qq = bool(row.get("qq_id", ""))
    is_synced = row.get("synced", "") == "1"
    return not has_qq and not is_synced


def upsert_ne_track(mappings: dict, ne_track: dict):
    """Add or update a row from a NetEase track dict."""
    ne_id = str(ne_track["id"])
    if ne_id in mappings and not ne_id.startswith("_"):
        existing = mappings[ne_id]
        existing["ne_name"] = ne_track.get("name", "")
        existing["ne_artist"] = ne_track.get("artist", "")
        if not existing.get("name"):
            existing["name"] = ne_track.get("name", "")
        if not existing.get("artist"):
            existing["artist"] = ne_track.get("artist", "")
        if not existing.get("album"):
            existing["album"] = ne_track.get("album", "")
    else:
        row = _empties()
        row["netease_id"] = ne_id
        row["name"] = ne_track.get("name", "")
        row["artist"] = ne_track.get("artist", "")
        row["album"] = ne_track.get("album", "")
        row["ne_name"] = ne_track.get("name", "")
        row["ne_artist"] = ne_track.get("artist", "")
        mappings[ne_id] = row


def upsert_qq_track(mappings: dict, qq_track: dict):
    """Add a QQ-only row or update qq_* fields on an existing matched row."""
    qq_id = str(qq_track["id"])
    qq_index = mappings.get("_qq_index", {})

    existing_ne_id = qq_index.get(qq_id)
    if existing_ne_id and existing_ne_id in mappings:
        # Update existing matched row
        row = mappings[existing_ne_id]
        row["qq_id"] = qq_id
        row["qq_name"] = qq_track.get("name", "")
        row["qq_artist"] = qq_track.get("artist", "")
        if not row.get("name"):
            row["name"] = qq_track.get("name", "")
        if not row.get("artist"):
            row["artist"] = qq_track.get("artist", "")
    else:
        # QQ-only row — use empty netease_id as key
        key = f"qq_{qq_id}"
        if key not in mappings:
            row = _empties()
            row["qq_id"] = qq_id
            row["name"] = qq_track.get("name", "")
            row["artist"] = qq_track.get("artist", "")
            row["album"] = qq_track.get("album", "")
            row["qq_name"] = qq_track.get("name", "")
            row["qq_artist"] = qq_track.get("artist", "")
            row["match_source"] = "qq_only"
            mappings[key] = row
        qq_index[qq_id] = key
        mappings["_qq_index"] = qq_index


def record_match(mappings: dict, ne_id: str, qq_track: dict, source: str):
    """Fill in qq_* columns on a netease-keyed row."""
    if ne_id not in mappings or ne_id.startswith("_"):
        return
    row = mappings[ne_id]
    row["qq_id"] = str(qq_track.get("id", ""))
    row["qq_name"] = qq_track.get("name", "")
    row["qq_artist"] = qq_track.get("artist", "")
    row["match_source"] = source

    qq_index = mappings.setdefault("_qq_index", {})
    qq_index[row["qq_id"]] = ne_id


def mark_synced(mappings: dict, ne_id: str):
    """Mark a netease-keyed row as successfully synced to QQ Music."""
    if ne_id in mappings and not ne_id.startswith("_"):
        mappings[ne_id]["synced"] = "1"


def promote_qq_row(mappings: dict, qq_key: str, ne_track: dict, source: str):
    """Convert a QQ-only row (qq_{id} key) to a netease-keyed row.

    Fills in netease_id and ne_* fields, removes the old synthetic key,
    and updates qq_index to point to the new key.
    """
    if qq_key not in mappings or not qq_key.startswith("qq_"):
        return None
    row = mappings.pop(qq_key)
    ne_id = str(ne_track["id"])
    row["netease_id"] = ne_id
    row["ne_name"] = ne_track.get("name", "")
    row["ne_artist"] = ne_track.get("artist", "")
    if not row.get("name"):
        row["name"] = ne_track.get("name", "")
    if not row.get("artist"):
        row["artist"] = ne_track.get("artist", "")
    row["match_source"] = source
    mappings[ne_id] = row
    # Update qq_index
    qq_id = row.get("qq_id", "")
    if qq_id:
        mappings.setdefault("_qq_index", {})[qq_id] = ne_id
    return ne_id


def get_qq_only_tracks(mappings: dict) -> list[tuple]:
    """Return (key, row) for QQ-only rows (match_source=qq_only, not yet synced to Ne)."""
    return [
        (k, v) for k, v in mappings.items()
        if not k.startswith("_") and k.startswith("qq_")
        and v.get("match_source") == "qq_only"
    ]


def record_unmatched(mappings: dict, ne_id: str):
    """Mark a netease-keyed row as unmatched."""
    if ne_id not in mappings or ne_id.startswith("_"):
        return
    mappings[ne_id]["match_source"] = "unmatched"


def save_mappings(mappings: dict):
    """Atomically write mappings to CSV with UTF-8 BOM."""
    path = get_csv_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    # Sort: netease-keyed rows first (by netease_id), then qq_ prefixed rows
    data_rows = [
        (k, v) for k, v in mappings.items()
        if k and not k.startswith("_")
    ]
    ne_rows = [(k, v) for k, v in data_rows if not k.startswith("qq_")]
    qq_rows = [(k, v) for k, v in data_rows if k.startswith("qq_")]
    ne_rows.sort(key=lambda x: x[0])
    qq_rows.sort(key=lambda x: x[0])

    # Write to temp file, then atomically replace
    tmp_path = path.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        for _, row in ne_rows + qq_rows:
            writer.writerow({col: row.get(col, "") for col in COLUMNS})

    # Atomic replace (works on Windows too)
    os.replace(tmp_path, path)


def is_modified(mappings: dict) -> bool:
    """Check if in-memory mappings differ from on-disk CSV."""
    path = get_csv_path()
    if not path.exists():
        return bool([k for k in mappings if not k.startswith("_")])
    on_disk = load_mappings()
    return _dict_rows(mappings) != _dict_rows(on_disk)


def _dict_rows(mappings: dict) -> dict:
    return {k: v for k, v in mappings.items() if k and not k.startswith("_")}
