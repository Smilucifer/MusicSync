"""One-time migration: sync_state.json + manual_mappings.json → csv/song_mappings.csv."""
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
from csv_database import get_csv_path, _empties, save_mappings, COLUMNS


def main():
    state_path = os.path.join(
        os.path.dirname(__file__), "..", "state", "sync_state.json"
    )
    manual_path = os.path.join(
        os.path.dirname(__file__), "..", "state", "manual_mappings.json"
    )
    template_path = os.path.join(
        os.path.dirname(__file__), "..", "state", "manual_mappings_template.json"
    )

    # Load sources
    with open(state_path, "r", encoding="utf-8") as f:
        state = json.load(f)

    manual = {}
    if os.path.exists(manual_path):
        with open(manual_path, "r", encoding="utf-8") as f:
            manual = json.load(f)

    template = {}
    if os.path.exists(template_path):
        with open(template_path, "r", encoding="utf-8") as f:
            template = json.load(f)

    now = datetime.now(timezone.utc).isoformat()
    mappings = {}
    stats = {"ne_total": 0, "ne_matched": 0, "qq_added": 0, "manual": 0, "template": 0}

    # Pass 1: Create rows from NetEase tracks
    ne_tracks = state.get("netease", {}).get("tracks", [])
    for t in ne_tracks:
        ne_id = str(t["id"])
        row = _empties()
        row["netease_id"] = ne_id
        row["name"] = t.get("name", "")
        row["artist"] = t.get("artist", "")
        row["album"] = t.get("album", "")
        row["ne_name"] = t.get("name", "")
        row["ne_artist"] = t.get("artist", "")

        qq_id = t.get("qq_match_id", "")
        confidence = t.get("qq_match_confidence", "")
        if qq_id and confidence:
            row["qq_id"] = str(qq_id)
            if confidence == "MANUAL":
                row["match_source"] = "manual"
            elif confidence == "L1":
                row["match_source"] = "isrc"
            elif confidence == "L2":
                row["match_source"] = "name_artist"
            stats["ne_matched"] += 1

        mappings[ne_id] = row
        stats["ne_total"] += 1

    # Pass 2: Enrich with QQ track data (fill in qq_name/qq_artist for matched)
    qq_tracks = state.get("qqmusic", {}).get("tracks", [])
    qq_by_id = {str(t["id"]): t for t in qq_tracks}

    for ne_id, row in list(mappings.items()):
        qq_id = row.get("qq_id", "")
        if qq_id and qq_id in qq_by_id:
            qq_t = qq_by_id[qq_id]
            row["qq_name"] = qq_t.get("name", "")
            row["qq_artist"] = qq_t.get("artist", "")

    # Pass 3: Add QQ-only tracks (not matched to any NetEase track)
    # Find matched QQ IDs
    matched_qq_ids = {row["qq_id"] for row in mappings.values() if row.get("qq_id")}
    for t in qq_tracks:
        qq_id = str(t["id"])
        if qq_id not in matched_qq_ids:
            key = f"qq_{qq_id}"
            row = _empties()
            row["qq_id"] = qq_id
            row["name"] = t.get("name", "")
            row["artist"] = t.get("artist", "")
            row["album"] = t.get("album", "")
            row["qq_name"] = t.get("name", "")
            row["qq_artist"] = t.get("artist", "")
            row["match_source"] = "qq_only"
            mappings[key] = row
            stats["qq_added"] += 1

    # Pass 4: Mark unmatched tracks from state
    unmatched = state.get("unmatched", {})
    for key, entry in unmatched.items():
        # key format: "netease:{id}"
        ne_id = key.split(":", 1)[1] if ":" in key else key
        if ne_id in mappings:
            row = mappings[ne_id]
            if not row.get("match_source") or row["match_source"] == "":
                row["match_source"] = "unmatched"

    # Pass 5: Apply manual_mappings.json overrides
    for ne_id, entry in manual.items():
        if isinstance(entry, dict):
            qq_id = entry.get("qq_id", "")
        else:
            qq_id = str(entry) if entry else ""

        if ne_id in mappings and qq_id:
            mappings[ne_id]["qq_id"] = qq_id
            mappings[ne_id]["match_source"] = "manual"
            stats["manual"] += 1

    # Pass 6: Add manual_mappings_template entries
    for ne_id, entry in template.items():
        if ne_id not in mappings:
            row = _empties()
            row["netease_id"] = ne_id
            row["name"] = entry.get("_name", "")
            row["artist"] = entry.get("_artist", "")
            row["ne_name"] = entry.get("_name", "")
            row["ne_artist"] = entry.get("_artist", "")
            row["match_source"] = "unmatched"
            mappings[ne_id] = row
            stats["template"] += 1

    # Save
    save_mappings(mappings)

    print(f"Migration complete → {get_csv_path()}")
    print(f"  NetEase tracks:     {stats['ne_total']}")
    print(f"    already matched:  {stats['ne_matched']}")
    print(f"  QQ-only tracks:     {stats['qq_added']}")
    print(f"  Manual overrides:   {stats['manual']}")
    print(f"  Template entries:   {stats['template']}")
    print(f"  Total CSV rows:     {sum(1 for k in mappings if k and not k.startswith('_'))}")


if __name__ == "__main__":
    main()
