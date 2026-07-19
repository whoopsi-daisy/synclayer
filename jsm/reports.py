"""Missing-subtitle reports with CSV / JSON / text export."""

from __future__ import annotations

import csv
import io
import json

from jsm.database.db import Database
from jsm.database.models import Media, MediaStatus


def missing_report(db: Database, statuses: list[str] | None = None) -> list[dict]:
    statuses = statuses or [MediaStatus.MISSING, MediaStatus.WRONG_LANG,
                            MediaStatus.UNSYNCED]
    rows: list[dict] = []
    media_by_status = [(s, db.all_media(status=s)) for s in statuses]
    all_ids = [m.id for _, ms in media_by_status for m in ms if m.id is not None]
    subs_map = db.subtitles_by_media(all_ids)
    for status, media_list in media_by_status:
        for media in media_list:
            assert media.id is not None
            langs = sorted({s.language for s in subs_map.get(media.id, [])})
            rows.append(
                {
                    "path": media.path,
                    "status": media.status,
                    "existing_languages": ",".join(langs),
                    "size": media.size,
                }
            )
    rows.sort(key=lambda r: r["path"])
    return rows


def format_report(rows: list[dict], fmt: str = "text") -> str:
    if fmt == "json":
        return json.dumps(rows, indent=2)
    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.DictWriter(
            buf, fieldnames=["path", "status", "existing_languages", "size"]
        )
        writer.writeheader()
        writer.writerows(rows)
        return buf.getvalue()
    if fmt == "text":
        if not rows:
            return "All good - no missing, wrong-language or unsynced subtitles.\n"
        width = max(len(r["status"]) for r in rows)
        lines = [f"{r['status']:<{width}}  {r['path']}"
                 f"{('  [' + r['existing_languages'] + ']') if r['existing_languages'] else ''}"
                 for r in rows]
        lines.append(f"\n{len(rows)} file(s) need attention")
        return "\n".join(lines) + "\n"
    raise ValueError(f"Unknown report format: {fmt}")
