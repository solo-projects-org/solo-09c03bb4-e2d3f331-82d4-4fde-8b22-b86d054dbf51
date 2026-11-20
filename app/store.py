"""SQLite-backed store for tariff-code group data versions.

Domain rules implemented here:

* The base table (import amounts in integer cents for two adjacent years)
  is fixed first and cannot change once a mapping version exists.
* Each mapping version is a *complete* old<->new tariff-code mapping between
  the two base years.  Codes are year-scoped: an unchanged code still appears
  as an explicit (code, code) pair.
* A mapping that misses base-table codes or references unknown codes is
  rejected as a whole; previously committed data stays queryable.
* Codes connected directly or transitively through the mapping form a group
  (connected component of the bipartite mapping graph).
* On a new version, groups whose members are unchanged keep their group id
  and stored totals; amounts are only read for brand-new groups.
* Every version persists its supply diff (replaced version, removed group
  ids, summaries to write) so changes can be fetched by version after a
  restart.  The first version returns the full dataset.
* Re-submitting an existing version with identical content replays its
  original diff; different content conflicts.
* Writers are serialized (process lock + BEGIN IMMEDIATE) and the diff base
  is re-read at commit time, so concurrent submissions are applied against
  the version they actually replace.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS amounts (
    year         INTEGER NOT NULL,
    code         TEXT    NOT NULL,
    amount_cents INTEGER NOT NULL,
    PRIMARY KEY (year, code)
);
CREATE TABLE IF NOT EXISTS versions (
    version          INTEGER PRIMARY KEY,
    content_hash     TEXT    NOT NULL,
    content_json     TEXT    NOT NULL,
    replaced_version INTEGER,
    created_at       TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS changes (
    version                INTEGER PRIMARY KEY REFERENCES versions(version),
    removed_group_ids_json TEXT NOT NULL,
    summaries_json         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS current_groups (
    group_id        INTEGER PRIMARY KEY,
    member_hash     TEXT    NOT NULL UNIQUE,
    old_codes_json  TEXT    NOT NULL,
    new_codes_json  TEXT    NOT NULL,
    old_total_cents INTEGER NOT NULL,
    new_total_cents INTEGER NOT NULL
);
"""

MAX_REPORTED = 20  # cap on code lists inside validation error payloads


class ApiError(Exception):
    """Domain error carrying an HTTP status code and a detail payload."""

    def __init__(self, status_code: int, detail):
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _member_hash(old_codes, new_codes) -> str:
    payload = json.dumps(
        {"old": old_codes, "new": new_codes},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _content_hash(pairs) -> str:
    payload = json.dumps(pairs, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _components(pairs):
    """Connected components of the bipartite old<->new mapping graph.

    Returns a deterministically ordered list of (old_codes, new_codes),
    each side sorted.
    """
    old_to_new: dict[str, set[str]] = {}
    new_to_old: dict[str, set[str]] = {}
    for old, new in pairs:
        old_to_new.setdefault(old, set()).add(new)
        new_to_old.setdefault(new, set()).add(old)

    groups = []
    seen_old: set[str] = set()
    for start in old_to_new:
        if start in seen_old:
            continue
        olds: set[str] = set()
        news: set[str] = set()
        stack = [("old", start)]
        while stack:
            side, code = stack.pop()
            if side == "old":
                if code in olds:
                    continue
                olds.add(code)
                stack.extend(("new", n) for n in old_to_new.get(code, ()))
            else:
                if code in news:
                    continue
                news.add(code)
                stack.extend(("old", o) for o in new_to_old.get(code, ()))
        seen_old |= olds
        groups.append((sorted(olds), sorted(news)))
    groups.sort(key=lambda g: (g[0][0], g[1][0]))
    return groups


class Store:
    """Thread-safe SQLite store.  All public methods take the write lock."""

    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout = 5000")
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(SCHEMA)
        self._lock = threading.RLock()

    def close(self):
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ meta
    def _meta_get(self, key):
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else json.loads(row["value"])

    def _meta_set(self, key, value):
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )

    def get_years(self):
        return self._meta_get("years")

    def _current_version(self):
        return self._meta_get("current_version")

    # ------------------------------------------------------------------ base
    def put_base(self, amounts):
        """Replace the base table.  ``amounts`` is a list of (year, code, cents)."""
        with self._lock:
            if self._current_version() is not None:
                raise ApiError(409, "base amounts are fixed once a mapping version exists")
            years = sorted({y for y, _, _ in amounts})
            if len(years) != 2 or years[1] != years[0] + 1:
                raise ApiError(422, "base amounts must cover exactly two adjacent years")
            seen = set()
            for year, code, _ in amounts:
                if (year, code) in seen:
                    raise ApiError(422, f"duplicate amount for year {year} code {code!r}")
                seen.add((year, code))
            per_year = {y: 0 for y in years}
            for year, _, _ in amounts:
                per_year[year] += 1

            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute("DELETE FROM amounts")
                self._conn.executemany(
                    "INSERT INTO amounts(year, code, amount_cents) VALUES(?, ?, ?)", amounts
                )
                self._meta_set("years", years)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            return {
                "years": years,
                "codes_per_year": {str(y): per_year[y] for y in years},
            }

    def get_base(self):
        with self._lock:
            rows = self._conn.execute(
                "SELECT year, code, amount_cents FROM amounts ORDER BY year, code"
            ).fetchall()
            return {"years": self.get_years(), "amounts": [dict(r) for r in rows]}

    # -------------------------------------------------------------- mappings
    def submit_mapping(self, version, pairs):
        """Submit a complete mapping.  Returns (change_record, created)."""
        with self._lock:
            years = self.get_years()
            if years is None:
                raise ApiError(409, "base amounts must be loaded before submitting mappings")
            old_year, new_year = years
            pairs = sorted(set(pairs))
            self._validate_pairs(pairs, old_year, new_year)
            content_hash = _content_hash(pairs)

            existing = self._conn.execute(
                "SELECT content_hash FROM versions WHERE version = ?", (version,)
            ).fetchone()
            if existing is not None:
                if existing["content_hash"] == content_hash:
                    # Idempotent replay: return the diff recorded at the time.
                    return self._change_record(version), False
                raise ApiError(409, f"version {version} already exists with different content")
            current = self._current_version()
            if current is not None and version <= current:
                raise ApiError(
                    409, f"version {version} is not newer than current version {current}"
                )

            self._conn.execute("BEGIN IMMEDIATE")
            try:
                # Re-read under the write lock: the diff is computed against the
                # version actually being replaced at commit time.
                replaced = self._current_version()
                record = self._commit_version(
                    version, pairs, content_hash, replaced, old_year, new_year
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            return record, True

    def _validate_pairs(self, pairs, old_year, new_year):
        base_old = {
            r["code"]
            for r in self._conn.execute("SELECT code FROM amounts WHERE year = ?", (old_year,))
        }
        base_new = {
            r["code"]
            for r in self._conn.execute("SELECT code FROM amounts WHERE year = ?", (new_year,))
        }
        map_old = {o for o, _ in pairs}
        map_new = {n for _, n in pairs}
        problems = {}
        for name, diff in (
            ("missing_old_codes", sorted(base_old - map_old)),
            ("unknown_old_codes", sorted(map_old - base_old)),
            ("missing_new_codes", sorted(base_new - map_new)),
            ("unknown_new_codes", sorted(map_new - base_new)),
        ):
            if diff:
                shown = diff[:MAX_REPORTED]
                if len(diff) > MAX_REPORTED:
                    shown.append(f"... and {len(diff) - MAX_REPORTED} more")
                problems[name] = shown
        if problems:
            raise ApiError(
                422,
                {
                    "message": "mapping rejected: it must cover exactly the base-table "
                    "codes of both years",
                    **problems,
                },
            )

    def _commit_version(self, version, pairs, content_hash, replaced, old_year, new_year):
        existing = {
            r["member_hash"]: r
            for r in self._conn.execute("SELECT * FROM current_groups")
        }
        next_id = self._meta_get("next_group_id") or 1
        summaries = []
        new_rows = []
        used_hashes = set()
        for olds, news in _components(pairs):
            mh = _member_hash(olds, news)
            used_hashes.add(mh)
            carried = existing.get(mh)
            if carried is not None:
                # Unchanged members: keep group id and stored totals, no reads.
                new_rows.append(
                    (
                        carried["group_id"],
                        mh,
                        olds,
                        news,
                        carried["old_total_cents"],
                        carried["new_total_cents"],
                    )
                )
                continue
            old_total = self._total(old_year, olds)
            new_total = self._total(new_year, news)
            new_rows.append((next_id, mh, olds, news, old_total, new_total))
            summaries.append(
                {
                    "group_id": next_id,
                    "old_codes": olds,
                    "new_codes": news,
                    "old_total_cents": old_total,
                    "new_total_cents": new_total,
                }
            )
            next_id += 1

        removed = sorted(
            row["group_id"] for mh, row in existing.items() if mh not in used_hashes
        )

        # Summary and the version's diff take effect in the same transaction.
        self._conn.execute("DELETE FROM current_groups")
        self._conn.executemany(
            "INSERT INTO current_groups(group_id, member_hash, old_codes_json, "
            "new_codes_json, old_total_cents, new_total_cents) VALUES(?, ?, ?, ?, ?, ?)",
            [
                (gid, mh, json.dumps(o), json.dumps(n), ot, nt)
                for gid, mh, o, n, ot, nt in new_rows
            ],
        )
        self._conn.execute(
            "INSERT INTO versions(version, content_hash, content_json, replaced_version, "
            "created_at) VALUES(?, ?, ?, ?, ?)",
            (version, content_hash, json.dumps(pairs, ensure_ascii=False), replaced, _now()),
        )
        self._conn.execute(
            "INSERT INTO changes(version, removed_group_ids_json, summaries_json) "
            "VALUES(?, ?, ?)",
            (version, json.dumps(removed), json.dumps(summaries, ensure_ascii=False)),
        )
        self._meta_set("next_group_id", next_id)
        self._meta_set("current_version", version)
        return {
            "version": version,
            "replaced_version": replaced,
            "removed_group_ids": removed,
            "summaries": summaries,
        }

    def _total(self, year, codes):
        placeholders = ",".join("?" for _ in codes)
        row = self._conn.execute(
            f"SELECT COALESCE(SUM(amount_cents), 0) AS t FROM amounts "
            f"WHERE year = ? AND code IN ({placeholders})",
            (year, *codes),
        ).fetchone()
        return row["t"]

    # ---------------------------------------------------------------- reads
    def _change_record(self, version):
        v = self._conn.execute(
            "SELECT * FROM versions WHERE version = ?", (version,)
        ).fetchone()
        if v is None:
            return None
        c = self._conn.execute(
            "SELECT * FROM changes WHERE version = ?", (version,)
        ).fetchone()
        return {
            "version": v["version"],
            "replaced_version": v["replaced_version"],
            "removed_group_ids": json.loads(c["removed_group_ids_json"]),
            "summaries": json.loads(c["summaries_json"]),
        }

    def get_change(self, version):
        with self._lock:
            return self._change_record(version)

    def get_mapping(self, version):
        with self._lock:
            v = self._conn.execute(
                "SELECT * FROM versions WHERE version = ?", (version,)
            ).fetchone()
            if v is None:
                return None
            return {
                "version": v["version"],
                "replaced_version": v["replaced_version"],
                "created_at": v["created_at"],
                "entries": [
                    {"old_code": o, "new_code": n}
                    for o, n in json.loads(v["content_json"])
                ],
            }

    def list_versions(self):
        with self._lock:
            rows = self._conn.execute(
                "SELECT version, replaced_version, created_at FROM versions "
                "ORDER BY version"
            ).fetchall()
            return {
                "current_version": self._current_version(),
                "versions": [dict(r) for r in rows],
            }

    def get_summary(self):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM current_groups ORDER BY group_id"
            ).fetchall()
            return {
                "version": self._current_version(),
                "years": self.get_years(),
                "groups": [
                    {
                        "group_id": r["group_id"],
                        "old_codes": json.loads(r["old_codes_json"]),
                        "new_codes": json.loads(r["new_codes_json"]),
                        "old_total_cents": r["old_total_cents"],
                        "new_total_cents": r["new_total_cents"],
                    }
                    for r in rows
                ],
            }
