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
from typing import Optional

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
CREATE TABLE IF NOT EXISTS forms (
    form_no         TEXT PRIMARY KEY,
    year            INTEGER NOT NULL,
    tax_codes_json  TEXT    NOT NULL,
    total_cents     INTEGER NOT NULL,
    status          TEXT    NOT NULL CHECK (status IN ('active', 'revoked')),
    registered_at   TEXT    NOT NULL,
    revoked_at      TEXT
);
CREATE TABLE IF NOT EXISTS vouchers (
    voucher_no       TEXT PRIMARY KEY,
    version          INTEGER NOT NULL,
    group_id         INTEGER NOT NULL,
    year             INTEGER NOT NULL,
    form_nos_json    TEXT    NOT NULL,
    total_cents      INTEGER NOT NULL,
    issued_at        TEXT    NOT NULL
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


def _exact_cover(candidates, target):
    """Smallest exact cover of ``target`` by candidate forms.

    ``candidates`` is an iterable of ``(form_no, code_set)``; every chosen
    form is used whole, chosen forms must be pairwise code-disjoint and their
    union must equal ``target`` exactly.  Returns the tuple of form numbers of
    the cover using the fewest forms, breaking ties by lexicographically
    smallest form-number tuple; returns ``None`` when no cover exists.
    """
    target = set(target)
    # Forms are used whole, so a form carrying any code outside the target
    # group can never participate in an exact cover and is discarded upfront.
    # Deterministic order also makes the first minimum found the lex-smallest.
    forms = sorted(
        ((no, set(codes)) for no, codes in candidates if set(codes) <= target),
        key=lambda item: item[0],
    )
    n = len(forms)
    best: Optional[tuple] = None
    current: list[str] = []

    def search(remaining: set[str]):
        nonlocal best
        if best is not None and len(current) >= len(best):
            return  # cannot beat the incumbent on cardinality
        if not remaining:
            best = tuple(current)
            return
        # Branch on one representative uncovered code: every valid cover must
        # pick exactly one form containing it.  Forms overlapping earlier picks
        # are excluded by the ``codes <= remaining`` check.
        needed = min(remaining)
        for i in range(n):
            no, codes = forms[i]
            if needed not in codes or not codes <= remaining:
                continue
            current.append(no)
            search(remaining - codes)
            current.pop()

    search(set(target))
    return best


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

    # ------------------------------------------------------------- 核验单
    @staticmethod
    def _form_out(row):
        return {
            "form_no": row["form_no"],
            "year": row["year"],
            "tax_codes": json.loads(row["tax_codes_json"]),
            "total_cents": row["total_cents"],
            "status": row["status"],
            "registered_at": row["registered_at"],
            "revoked_at": row["revoked_at"],
        }

    def _base_amounts(self, year, codes):
        placeholders = ",".join("?" for _ in codes)
        return {
            r["code"]: r["amount_cents"]
            for r in self._conn.execute(
                f"SELECT code, amount_cents FROM amounts "
                f"WHERE year = ? AND code IN ({placeholders})",
                (year, *codes),
            ).fetchall()
        }

    def register_form(self, form_no, year, tax_codes, total_cents):
        """Register a verification form; its total must match the base table."""
        with self._lock:
            years = self.get_years()
            if years is None:
                raise ApiError(409, "base amounts must be loaded before registering forms")
            if year not in years:
                raise ApiError(
                    422,
                    {
                        "message": "year must be one of the base-table years",
                        "year": year,
                        "base_years": years,
                    },
                )
            duplicates = sorted({c for c in tax_codes if tax_codes.count(c) > 1})
            if duplicates:
                raise ApiError(
                    422, {"message": "tax_codes must be distinct", "duplicates": duplicates}
                )
            base = self._base_amounts(year, tax_codes)
            unknown = sorted(set(tax_codes) - base.keys())
            if unknown:
                raise ApiError(
                    422,
                    {
                        "message": "every tax code must exist in the base table for that year",
                        "unknown_tax_codes": unknown,
                    },
                )
            expected = sum(base.values())
            if total_cents != expected:
                raise ApiError(
                    422,
                    {
                        "message": "total_cents does not match the base table",
                        "expected_total_cents": expected,
                        "submitted_total_cents": total_cents,
                    },
                )
            if self._conn.execute(
                "SELECT 1 FROM forms WHERE form_no = ?", (form_no,)
            ).fetchone():
                raise ApiError(409, f"form_no {form_no!r} is already registered")

            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "INSERT INTO forms(form_no, year, tax_codes_json, total_cents, "
                    "status, registered_at, revoked_at) VALUES(?, ?, ?, ?, 'active', ?, NULL)",
                    (form_no, year, json.dumps(tax_codes), total_cents, _now()),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            row = self._conn.execute(
                "SELECT * FROM forms WHERE form_no = ?", (form_no,)
            ).fetchone()
            return self._form_out(row)

    def revoke_form(self, form_no):
        """Revoke a form.  The row is kept (soft delete); repeat calls are fine."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM forms WHERE form_no = ?", (form_no,)
            ).fetchone()
            if row is None:
                raise ApiError(404, f"form_no {form_no!r} not found")
            if row["status"] == "active":
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    self._conn.execute(
                        "UPDATE forms SET status = 'revoked', revoked_at = ? WHERE form_no = ?",
                        (_now(), form_no),
                    )
                    self._conn.execute("COMMIT")
                except Exception:
                    self._conn.execute("ROLLBACK")
                    raise
            return self._form_out(
                self._conn.execute(
                    "SELECT * FROM forms WHERE form_no = ?", (form_no,)
                ).fetchone()
            )

    def list_forms(self, status: Optional[str] = None):
        with self._lock:
            if status is not None:
                rows = self._conn.execute(
                    "SELECT * FROM forms WHERE status = ? ORDER BY form_no", (status,)
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM forms ORDER BY form_no"
                ).fetchall()
            return {"forms": [self._form_out(r) for r in rows]}

    def get_form(self, form_no):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM forms WHERE form_no = ?", (form_no,)
            ).fetchone()
            if row is None:
                raise ApiError(404, f"form_no {form_no!r} not found")
            return self._form_out(row)

    # --------------------------------------------------------------- 凭证
    def _resolve_group(self, group_id, year):
        """Return the group row and its member codes for the given year."""
        years = self.get_years()
        if years is None:
            raise ApiError(409, "base amounts must be loaded before issuing vouchers")
        if year not in years:
            raise ApiError(
                422,
                {
                    "message": "year must be one of the base-table years",
                    "year": year,
                    "base_years": years,
                },
            )
        row = self._conn.execute(
            "SELECT * FROM current_groups WHERE group_id = ?", (group_id,)
        ).fetchone()
        if row is None:
            raise ApiError(
                422,
                {
                    "message": "group_id not found in the current mapping version",
                    "group_id": group_id,
                },
            )
        side = "old_codes_json" if year == years[0] else "new_codes_json"
        return row, set(json.loads(row[side]))

    def _validate_issue_input(self, observed_version, group_id, year, form_nos):
        """Shared validation/selection used by both preview and issuance."""
        current = self._current_version()
        if current is None:
            raise ApiError(409, "a mapping version must exist before issuing vouchers")
        if len(form_nos) != len(set(form_nos)):
            raise ApiError(422, {"message": "form_nos must be distinct"})
        _, target = self._resolve_group(group_id, year)

        rows = {
            r["form_no"]: r
            for r in self._conn.execute(
                "SELECT * FROM forms WHERE form_no IN (%s)"
                % ",".join("?" for _ in form_nos),
                form_nos,
            ).fetchall()
        }
        unknown = sorted(no for no in form_nos if no not in rows)
        if unknown:
            raise ApiError(
                422, {"message": "unknown form numbers", "unknown_form_nos": unknown}
            )
        wrong_year = sorted(no for no in form_nos if rows[no]["year"] != year)
        if wrong_year:
            raise ApiError(
                422,
                {
                    "message": "every form must belong to the requested year",
                    "wrong_year_form_nos": wrong_year,
                },
            )

        active = [
            (no, set(json.loads(rows[no]["tax_codes_json"])))
            for no in form_nos
            if rows[no]["status"] == "active"
        ]
        ignored_revoked = sorted(no for no in form_nos if rows[no]["status"] == "revoked")

        # The observed version is checked before the cover so a stale caller
        # always gets the conflict rather than a misleading cover result.
        if observed_version != current:
            raise ApiError(
                409,
                {
                    "message": "observed mapping version has been replaced",
                    "observed_version": observed_version,
                    "current_version": current,
                },
            )

        selected = _exact_cover(active, target)
        if selected is None:
            if ignored_revoked:
                # Revoked submitted forms are excluded from the candidate pool;
                # the caller can still retry after replacing them.
                raise ApiError(
                    409,
                    {
                        "message": "a submitted form was revoked and the remaining forms "
                        "cannot exactly cover the group",
                        "revoked_form_nos": ignored_revoked,
                    },
                )
            raise ApiError(
                422,
                {
                    "message": "no subset of the submitted active forms can exactly cover "
                    "the group's codes without overlap",
                    "group_id": group_id,
                    "year": year,
                },
            )
        total = sum(rows[no]["total_cents"] for no in selected)
        return {
            "observed_version": observed_version,
            "group_id": group_id,
            "year": year,
            "selected_form_nos": list(selected),
            "total_cents": total,
            "ignored_revoked_form_nos": ignored_revoked,
        }

    def preview_voucher(self, observed_version, group_id, year, form_nos):
        with self._lock:
            return self._validate_issue_input(
                observed_version, group_id, year, form_nos
            )

    def issue_voucher(self, observed_version, group_id, year, form_nos):
        with self._lock:
            result = self._validate_issue_input(
                observed_version, group_id, year, form_nos
            )
            selected = result["selected_form_nos"]

            self._conn.execute("BEGIN IMMEDIATE")
            try:
                # Re-check inside the write transaction: anything that changed
                # between selection and commit aborts the issuance, no voucher.
                current = self._current_version()
                if current != observed_version:
                    raise ApiError(
                        409,
                        {
                            "message": "observed mapping version has been replaced",
                            "observed_version": observed_version,
                            "current_version": current,
                        },
                    )
                placeholders = ",".join("?" for _ in selected)
                picked_rows = self._conn.execute(
                    f"SELECT form_no, status FROM forms WHERE form_no IN ({placeholders})",
                    selected,
                ).fetchall()
                now_revoked = sorted(
                    r["form_no"] for r in picked_rows if r["status"] != "active"
                )
                if now_revoked:
                    raise ApiError(
                        409,
                        {
                            "message": "a selected form was revoked before the voucher "
                            "was written; no voucher generated",
                            "revoked_form_nos": now_revoked,
                        },
                    )

                group_row, target = self._resolve_group(group_id, year)
                expected_total = (
                    group_row["old_total_cents"]
                    if year == self.get_years()[0]
                    else group_row["new_total_cents"]
                )
                if result["total_cents"] != expected_total:
                    raise ApiError(
                        422,
                        {
                            "message": "selected forms' total does not match the group total",
                            "selected_total_cents": result["total_cents"],
                            "group_total_cents": expected_total,
                        },
                    )

                next_no = (self._meta_get("next_voucher_no") or 1)
                voucher_no = f"V{next_no:06d}"
                issued_at = _now()
                self._conn.execute(
                    "INSERT INTO vouchers(voucher_no, version, group_id, year, "
                    "form_nos_json, total_cents, issued_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
                    (
                        voucher_no,
                        current,
                        group_id,
                        year,
                        json.dumps(selected),
                        result["total_cents"],
                        issued_at,
                    ),
                )
                self._meta_set("next_voucher_no", next_no + 1)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            return {
                "voucher_no": voucher_no,
                "version": current,
                "issued_at": issued_at,
                **result,
            }

    def get_voucher(self, voucher_no):
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM vouchers WHERE voucher_no = ?", (voucher_no,)
            ).fetchone()
            if r is None:
                raise ApiError(404, f"voucher_no {voucher_no!r} not found")
            return {
                "voucher_no": r["voucher_no"],
                "version": r["version"],
                "observed_version": r["version"],
                "group_id": r["group_id"],
                "year": r["year"],
                "selected_form_nos": json.loads(r["form_nos_json"]),
                "total_cents": r["total_cents"],
                "ignored_revoked_form_nos": [],
                "issued_at": r["issued_at"],
            }

    def list_vouchers(self):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM vouchers ORDER BY voucher_no"
            ).fetchall()
            return {"vouchers": [self.get_voucher(r["voucher_no"]) for r in rows]}
