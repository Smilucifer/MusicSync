"""One-shot: migrate csv/song_mappings.csv → data/musicsync.db.

Algorithm: union-find on (netease_id, qq_id) — any two rows that share
either id collapse into one song cluster. canonical_key is NOT used as
the merge key because the same logical song may have different cleaned
names across platforms (e.g. "Love Story (Taylor's Version)" vs "Love Story").
"""
import csv
import json
import os
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from db import (
    init_db, connect, upsert_song, upsert_platform_link, put_lyrics, set_meta,
)
from matcher import clean_name, clean_artist  # uses post-rewrite matcher's helpers; available now from old matcher too


PRIORITY = {
    "manual": 0,
    "isrc": 1,
    "name_artist": 2,
    "unmatched": 3,
    "qq_only": 3,
}


class UnionFind:
    def __init__(self):
        self.parent: dict[int, int] = {}

    def find(self, x: int) -> int:
        while self.parent.get(x, x) != x:
            self.parent[x] = self.parent.get(self.parent[x], self.parent[x])
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb

    def add(self, x: int) -> None:
        self.parent.setdefault(x, x)


def _canonical_key(name: str, artist: str) -> str:
    return f"{clean_name(name)}|{clean_artist(artist)}"


def _priority(row: dict) -> int:
    return PRIORITY.get(row.get("match_source", ""), 99)


def migrate_csv(csv_path: str, db_path: str) -> dict:
    """Read CSV → write SQLite. Returns summary dict."""
    init_db(db_path)

    rows: list[dict] = []
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            rows.append(r)

    uf = UnionFind()
    ne_to_idx: dict[str, int] = {}
    qq_to_idx: dict[str, int] = {}

    for i, r in enumerate(rows):
        uf.add(i)
        ne = (r.get("netease_id") or "").strip()
        qq = (r.get("qq_id") or "").strip()
        if ne:
            if ne in ne_to_idx:
                uf.union(i, ne_to_idx[ne])
            else:
                ne_to_idx[ne] = i
        if qq:
            if qq in qq_to_idx:
                uf.union(i, qq_to_idx[qq])
            else:
                qq_to_idx[qq] = i

    clusters: dict[int, list[int]] = {}
    for i in range(len(rows)):
        clusters.setdefault(uf.find(i), []).append(i)

    summary = {"rows": len(rows), "songs": 0, "links": 0}

    with connect(db_path) as conn:
        for cluster_rows in clusters.values():
            members = [rows[i] for i in cluster_rows]
            members.sort(key=_priority)
            head = members[0]

            is_manual = (head.get("match_source") == "manual")
            match_source = "manual" if is_manual else "migrated"
            original_match_source = head.get("match_source") or None

            song_id = upsert_song(
                conn,
                canonical_key=_canonical_key(head.get("name", ""), head.get("artist", "")),
                name=head.get("name", "") or head.get("ne_name", "") or head.get("qq_name", "") or "",
                artist=head.get("artist", "") or head.get("ne_artist", "") or head.get("qq_artist", "") or "",
                album=head.get("album") or None,
                match_source=match_source,
                original_match_source=original_match_source,
            )
            summary["songs"] += 1

            seen_links: set[tuple[str, str]] = set()
            for r in members:
                ne_id = (r.get("netease_id") or "").strip()
                qq_id = (r.get("qq_id") or "").strip()
                if ne_id and ("netease", ne_id) not in seen_links:
                    upsert_platform_link(
                        conn,
                        song_id=song_id,
                        platform="netease",
                        platform_track_id=ne_id,
                        platform_name=r.get("ne_name") or r.get("name") or "",
                        platform_artist=r.get("ne_artist") or r.get("artist") or "",
                        platform_album=r.get("album") or None,
                        liked=1,
                        synced_at=None,
                    )
                    seen_links.add(("netease", ne_id))
                    summary["links"] += 1
                if qq_id and ("qq", qq_id) not in seen_links:
                    upsert_platform_link(
                        conn,
                        song_id=song_id,
                        platform="qq",
                        platform_track_id=qq_id,
                        platform_name=r.get("qq_name") or r.get("name") or "",
                        platform_artist=r.get("qq_artist") or r.get("artist") or "",
                        platform_album=r.get("album") or None,
                        liked=1,
                        synced_at=None,
                    )
                    seen_links.add(("qq", qq_id))
                    summary["links"] += 1

        # 迁移 state.json 中的 lyrics_cache
        state_path = ROOT / "state" / "sync_state.json"
        if state_path.exists():
            try:
                with open(state_path, "r", encoding="utf-8") as f:
                    state = json.load(f)
                cache = state.get("lyrics_cache", {})
                imported = 0
                for k, v in cache.items():
                    if ":" in k:
                        platform, tid = k.split(":", 1)
                        if platform not in ("netease", "qq") or not tid:
                            continue
                    elif k.isdigit():
                        platform, tid = "netease", k
                    else:
                        continue
                    if isinstance(v, dict):
                        original = v.get("original", "") or v.get("lyric", "") or ""
                        translated = v.get("translated", "") or v.get("trans", "") or ""
                    elif isinstance(v, (list, tuple)) and len(v) >= 2:
                        original, translated = v[0] or "", v[1] or ""
                    else:
                        continue
                    put_lyrics(conn, platform, tid, original, translated)
                    imported += 1
                summary["lyrics_cache_imported"] = imported
                last_sync = state.get("last_sync")
                if last_sync:
                    set_meta(conn, "last_sync_at", last_sync)
            except Exception as e:
                print(f"WARN: lyrics_cache migration skipped: {e}")

    return summary


def main(argv: list[str]) -> int:
    csv_path = argv[1] if len(argv) > 1 else str(ROOT / "csv" / "song_mappings.csv")
    db_path = argv[2] if len(argv) > 2 else str(ROOT / "data" / "musicsync.db")
    if os.path.exists(db_path):
        print(f"REFUSING: {db_path} already exists. Delete it first if you mean to re-run.")
        return 1
    summary = migrate_csv(csv_path, db_path)
    print(f"Migrated: {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
