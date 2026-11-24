"""Salvage a damaged database: keep every row that still reads, and fill only what is lost from an older healthy copy (#455).

    python -m app.db_salvage DAMAGED OUTPUT [--fill-from HEALTHY_COPY]

Restoring the newest healthy copy loses everything written since it was made: on the reporting site that would have been about 45 hours of
the recording index and of settings, for four lost pages out of 300,000. This writes a new file instead:

1. every table that reads cleanly is copied whole from the damaged file;
2. a table with lost pages is read in stretches, and the stretch around each lost page is narrowed down to the rows that cannot be read;
3. those rows, and only those, are taken from the healthy copy when one is given (rows it does not have are lost, and are counted);
4. the indexes, triggers and views are made again, and the result is checked.

Nothing is written to the damaged file or to the copy: both are opened read-only. The NVR must be stopped. The output must not exist yet.
Run it on copies of the files where you can (see docs/database-recovery.md).
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

BATCH = 5000  # Rows read at a time; a read that fails is tried again at half the length, down to one row


class SalvageError(RuntimeError):
    pass


@dataclass
class TableResult:
    name: str
    read: int = 0  # Rows copied from the damaged file
    filled: int = 0  # Rows taken from the healthy copy
    lost: list[tuple[int, int | None]] = field(default_factory=list)  # Stretches of row numbers (after, up to) that could not be read
    whole: bool = True  # Read in one go, with nothing lost
    note: str = ""


def _read_only(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    db.row_factory = sqlite3.Row
    return db


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _columns(db: sqlite3.Connection, table: str) -> list[str]:
    """The stored columns (not generated ones), in order."""
    return [row["name"] for row in db.execute(f"PRAGMA table_xinfo({_quote(table)})") if row["hidden"] == 0]


def _insert(out: sqlite3.Connection, table: str, columns: list[str], rows: list[tuple], *, ignore: bool = False) -> int:
    names = ",".join(["rowid", *map(_quote, columns)])
    marks = ",".join("?" * (len(columns) + 1))
    before = out.total_changes
    out.executemany(f"INSERT {'OR IGNORE ' if ignore else ''}INTO {_quote(table)}({names}) VALUES({marks})", rows)
    return out.total_changes - before


class _Reader:
    """Reads one table of the damaged file by row number. Every read is on a connection of its own: after a damage error a connection's
    later answers are not to be trusted."""

    def __init__(self, path: Path, table: str, columns: list[str]) -> None:
        self.path, self.table = path, table
        self.select = f"SELECT rowid,{','.join(map(_quote, columns))} FROM {_quote(table)}"

    def after(self, rowid: int, limit: int) -> list[tuple] | None:
        """The next rows after ``rowid``, or None when they cannot be read."""
        db = _read_only(self.path)
        try:
            return [tuple(row) for row in db.execute(f"{self.select} WHERE rowid>? ORDER BY rowid LIMIT ?", (rowid, limit)).fetchall()]
        except sqlite3.DatabaseError:
            return None
        finally:
            db.close()

    def resume_after(self, rowid: int) -> int | None:
        """The smallest row number from which reading works again, past the rows that cannot be read; None when nothing more can be read.
        A lost page makes every read that starts inside it fail and every read that starts past it work, so the edge is found by doubling
        the distance and then halving back."""
        step = 1
        while self.after(rowid + step, 1) is None:
            step *= 2
            if rowid + step > 2 ** 62:
                return None
        low, high = rowid + step // 2, rowid + step  # Reading fails from low (or low is where we stand) and works from high
        while high - low > 1:
            middle = (low + high) // 2
            if self.after(middle, 1) is None:
                low = middle
            else:
                high = middle
        return high


def _copy_table(damaged: Path, out: sqlite3.Connection, table: str, columns: list[str]) -> TableResult:
    result = TableResult(table)
    reader = _Reader(damaged, table, columns)
    position = -(2 ** 63)
    limit = BATCH
    while True:
        rows = reader.after(position, limit)
        if rows is None and limit > 1:
            limit //= 2  # Shorter reads up to the edge of what is lost
            continue
        if rows is None:
            result.whole = False
            resume = reader.resume_after(position)
            result.lost.append((position, resume))
            if resume is None:
                return result
            position, limit = resume, BATCH
            continue
        if not rows:
            return result
        result.read += _insert(out, table, columns, rows)
        position = rows[-1][0]
        limit = min(BATCH, limit * 2)


def _fill(copy: Path, out: sqlite3.Connection, result: TableResult, columns: list[str]) -> None:
    source = _read_only(copy)
    try:
        if source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (result.name,)).fetchone() is None:
            result.note = "not in the healthy copy"
            return
        shared = [name for name in columns if name in set(_columns(source, result.name))]
        select = f"SELECT rowid,{','.join(map(_quote, shared))} FROM {_quote(result.name)}"
        for after, up_to in result.lost:
            rows = [tuple(row) for row in source.execute(f"{select} WHERE rowid>? AND rowid<=? ORDER BY rowid", (after, 2 ** 63 - 1 if up_to is None else up_to))]
            result.filled += _insert(out, result.name, shared, rows, ignore=True)  # A row that also read from the damaged file keeps that, newer, form
    finally:
        source.close()


def _schema(damaged: Path) -> list[sqlite3.Row]:
    db = _read_only(damaged)
    try:
        return db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY rowid").fetchall()
    except sqlite3.DatabaseError as exc:
        raise SalvageError(f"The list of tables in {damaged.name} cannot be read ({exc}): this file cannot be salvaged this way. Restore the newest healthy copy.") from exc
    finally:
        db.close()


def salvage(damaged: Path, output: Path, fill_from: Path | None = None) -> list[TableResult]:
    """Write ``output`` from ``damaged`` (and ``fill_from``). Returns what happened to each table; raises SalvageError when the result is not sound."""
    if output.exists():
        raise SalvageError(f"{output} already exists: choose a name that is not in use. Nothing was changed.")
    for source in (damaged, fill_from):
        if source is not None and not source.is_file():
            raise SalvageError(f"{source} is not a file")
    schema = _schema(damaged)
    tables = [row for row in schema if row["type"] == "table" and not row["name"].startswith("sqlite_")]
    out = sqlite3.connect(output, isolation_level=None)
    out.row_factory = sqlite3.Row
    try:
        out.execute("PRAGMA foreign_keys = OFF")
        settings = _read_only(damaged)
        try:
            for pragma in ("user_version", "application_id"):
                out.execute(f"PRAGMA {pragma} = {int(settings.execute(f'PRAGMA {pragma}').fetchone()[0])}")
        finally:
            settings.close()
        out.execute("BEGIN")
        for row in tables:
            out.execute(row["sql"])
        results = []
        for row in tables:
            columns = _columns(out, row["name"])
            result = _copy_table(damaged, out, row["name"], columns)
            if result.lost and fill_from is not None:
                _fill(fill_from, out, result, columns)
            results.append(result)
        _copy_sequences(damaged, out)
        _work_out_derived(out)
        for row in schema:  # After the rows, so that no trigger runs on them and each index is built once
            if row["type"] in {"index", "trigger", "view"} and row["sql"]:
                try:
                    out.execute(row["sql"])
                except sqlite3.IntegrityError as exc:
                    raise SalvageError(f"The index {row['name']} could not be rebuilt ({exc}): two salvaged rows of {row['tbl_name']} claim the same value") from exc
        out.execute("COMMIT")
        problems = [str(found[0]) for found in out.execute("PRAGMA integrity_check(10)").fetchall()]
        if problems != ["ok"]:
            raise SalvageError("The salvaged file failed its integrity check: " + "; ".join(problems))
        orphans = out.execute("PRAGMA foreign_key_check").fetchall()
        if orphans:
            by_table: dict[str, int] = {}
            for orphan in orphans:
                by_table[orphan[0]] = by_table.get(orphan[0], 0) + 1
            for result in results:
                if result.name in by_table:
                    result.note = (result.note + "; " if result.note else "") + f"{by_table[result.name]} rows refer to a row that was lost"
        out.execute("PRAGMA journal_mode = WAL")
    except BaseException:
        out.close()
        for leftover in (output, output.with_name(output.name + "-journal"), output.with_name(output.name + "-wal"), output.with_name(output.name + "-shm")):
            leftover.unlink(missing_ok=True)
        raise
    out.close()
    output.chmod(0o600)
    return results


def _work_out_derived(out: sqlite3.Connection) -> None:
    """Tables worked out from others are made again from the salvaged rows: copied, they need not agree with what was kept (a row lost,
    or filled from the older copy). Before the triggers are made, so they do not run."""
    from app.migrations.m0096_alarm_counts import RECOUNT

    names = {row[0] for row in out.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if {"alarms", "alarm_counts"} <= names:  # The alarm inbox's counts (#517)
        for statement in RECOUNT:
            out.execute(statement)
    if {"ai_tracks", "ai_track_spans"} <= names:  # Each camera's longest track, which bounds a search (#515)
        from app.migrations.m0097_track_spans import RESPAN

        for statement in RESPAN:
            out.execute(statement)
    if "insight_fresh" in names:  # Insights and heatmap totals: forgotten, and worked out again by the background (#515)
        from app.migrations.m0109_insight_totals import FORGET

        for statement in FORGET:
            out.execute(statement)


def _copy_sequences(damaged: Path, out: sqlite3.Connection) -> None:
    """The next row number of each AUTOINCREMENT table, so a number once used is not given out again. Never lower than the rows salvaged."""
    if out.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone() is None:
        return
    source = _read_only(damaged)
    try:
        rows = source.execute("SELECT name,seq FROM sqlite_sequence").fetchall()
    except sqlite3.DatabaseError:
        rows = []
    finally:
        source.close()
    for row in rows:
        if out.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (row["name"],)).fetchone() is None:
            continue
        highest = out.execute(f"SELECT MAX(rowid) FROM {_quote(row['name'])}").fetchone()[0] or 0
        out.execute("DELETE FROM sqlite_sequence WHERE name=?", (row["name"],))
        out.execute("INSERT INTO sqlite_sequence(name,seq) VALUES(?,?)", (row["name"], max(int(row["seq"] or 0), highest)))


def report(results: list[TableResult], *, filled: bool) -> str:
    damaged = [result for result in results if not result.whole]
    lines = [f"{len(results) - len(damaged)} tables were copied whole ({sum(result.read for result in results if result.whole)} rows)."]
    for result in damaged:
        line = f"{result.name}: {result.read} rows read from the damaged file, {len(result.lost)} stretch(es) could not be read"
        line += f", {result.filled} rows taken from the healthy copy" if filled else "; no healthy copy was given, so those rows are lost"
        lines.append(line + (f" ({result.note})" if result.note else "") + ".")
    for result in results:
        if result.whole and result.note:
            lines.append(f"{result.name}: {result.note}.")
    if not damaged:
        lines.append("No table had rows that could not be read. If the NVR reported damage, it is in an index, and the indexes were rebuilt.")
    elif filled:
        lines.append("Rows taken from the healthy copy are as they were when that copy was made: changes to them since then are lost.")
    lines.append("The salvaged file passed its integrity check.")
    return "\n".join(lines)


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.db_salvage", description="Salvage a damaged NVR database into a new file. The NVR must be stopped.")
    parser.add_argument("damaged", type=Path, help="the damaged database file (opened read-only)")
    parser.add_argument("output", type=Path, help="the new file to write; it must not exist")
    parser.add_argument("--fill-from", type=Path, metavar="HEALTHY_COPY", help="an older healthy copy (a *.pre-migration-* file) to take the unreadable rows from")
    options = parser.parse_args(arguments)
    try:
        results = salvage(options.damaged, options.output, options.fill_from)
    except SalvageError as exc:
        print(f"Not salvaged: {exc}", file=sys.stderr)
        return 1
    print(report(results, filled=options.fill_from is not None))
    print(f"Written to {options.output}. Nothing else was changed: see docs/database-recovery.md for putting it in place.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
