"""Per-library, per-conversation local calendar ranges. Never deletes cached media."""
import json
import os
from datetime import date, datetime, time, timedelta
from pathlib import Path


def validate_range(start: str, end: str | None, *, today=None) -> dict:
    today = today or date.today()
    try:
        first = date.fromisoformat(start)
        last = date.fromisoformat(end) if end is not None else today
        if first.isoformat() != start or (end is not None and last.isoformat() != end):
            raise ValueError()
        if first > last or last > today:
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('请选择有效日期，起始日不能晚于结束日，结束日不能晚于今天') from None
    return {'start': start, 'end': end}


class MediaRanges:
    def __init__(self, root: Path, added_at: str, *, today=None):
        self.today = today or date.today()
        added = datetime.fromisoformat(added_at.replace('Z', '+00:00')).astimezone().date()
        self.default = {'start': (added - timedelta(days=14)).isoformat(), 'end': None}
        self.path = root / 'data' / 'media_ranges.json'
        self.overrides = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {}
        if not isinstance(self.overrides, dict):
            raise ValueError('invalid_media_ranges')
        for value in self.overrides.values():
            validate_range(value['start'], value['end'], today=self.today)

    def get(self, username: str) -> dict:
        return dict(self.overrides.get(username, self.default))

    def save(self, username: str, start: str, end: str | None) -> dict:
        value = validate_range(start, end, today=self.today)
        self.overrides[username] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.json.tmp')
        temporary.write_text(json.dumps(self.overrides, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temporary, self.path)
        return dict(value)

    def bounds(self, username: str) -> tuple[int, int]:
        value = self.get(username)
        first = date.fromisoformat(value['start'])
        last = date.fromisoformat(value['end']) if value['end'] else self.today
        return (int(datetime.combine(first, time.min).timestamp()),
                int(datetime.combine(last + timedelta(days=1), time.min).timestamp()))

    def contains(self, username: str, timestamp: int) -> bool:
        first, end = self.bounds(username)
        return first <= timestamp < end

    def envelope(self) -> tuple[int, int]:
        bounds = [self.bounds('')] + [self.bounds(name) for name in self.overrides]
        return min(x[0] for x in bounds), max(x[1] for x in bounds)
