"""Readers of the H1 rate files and of the meeting calendar (frozen text §1.2-§1.3).

The files are the ones downloaded on the PC on 2026-11-26/27 into
``data/h1-rates/raw/``, kept exactly as each source served them; their layouts are
described in ``data/h1-rates/layout.md``. Every reader returns ``{date: percent}``
and drops dates on or after the end of the split, so a development run keeps
nothing from the validation years. Each file read is hashed into the report.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any

from fxnext.research.cbdrift.events import Meeting
from fxnext.research.cbdrift.prereg import BANKS, EONIA_LAST_DAY, ESTR_TO_EONIA_BP

Series = dict[date, float]


class SourceError(ValueError):
    pass


def _num(text: str) -> float | None:
    t = text.strip().strip('"')
    if t in ("", "_", "...", "N/A", "NA", ".", "-"):
        return None
    try:
        return float(t)
    except ValueError as error:
        raise SourceError(f"not a number: {t!r}") from error


def _decode(payload: bytes, encoding: str = "utf-8-sig") -> str:
    try:
        return payload.decode(encoding)
    except UnicodeDecodeError as error:
        raise SourceError(f"not {encoding}") from error


def parse_column_csv(
    payload: bytes,
    date_col: str,
    value_col: str,
    *,
    delimiter: str = ",",
    date_format: str = "%Y-%m-%d",
    start_marker: str | None = None,
) -> Series:
    """Long or narrow CSV with a header row; ``start_marker`` skips lines up to it."""

    lines = _decode(payload).splitlines()
    if start_marker is not None:
        idx = [i for i, ln in enumerate(lines) if ln.strip().strip('"') == start_marker]
        if not idx:
            raise SourceError(f"marker {start_marker!r} not found")
        lines = lines[idx[0] + 1 :]
    reader = csv.reader(lines, delimiter=delimiter)
    header = next(reader, None)
    if header is None or date_col not in header or value_col not in header:
        raise SourceError(f"columns {date_col}/{value_col} not in header {header}")
    di, vi = header.index(date_col), header.index(value_col)
    out: Series = {}
    for row in reader:
        if not row or len(row) <= max(di, vi):
            continue
        v = _num(row[vi])
        if v is not None:
            out[datetime.strptime(row[di].strip(), date_format).date()] = v
    return out


def parse_snb(payload: bytes, code: str) -> Series:
    """SNB cube CSV, long format: "Date";"D0";"Value" after three metadata lines."""

    lines = _decode(payload).splitlines()
    idx = [i for i, ln in enumerate(lines) if ln.replace('"', "").startswith("Date;D0;Value")]
    if not idx:
        raise SourceError("SNB header not found")
    out: Series = {}
    for row in csv.reader(lines[idx[0] + 1 :], delimiter=";"):
        if len(row) < 3 or row[1] != code:
            continue
        v = _num(row[2])
        if v is not None:
            out[date.fromisoformat(row[0])] = v
    return out


_ES_MONTHS = {
    m: i + 1
    for i, m in enumerate(
        ["ENE", "FEB", "MAR", "ABR", "MAY", "JUN", "JUL", "AGO", "SEP", "OCT", "NOV", "DIC"]
    )
}


def parse_bde(payload: bytes, code: str) -> Series:
    """Banco de España wide CSV (latin-1): six metadata lines, Spanish month names."""

    rows = list(csv.reader(io.StringIO(_decode(payload, "cp1252"))))
    if not rows or code not in rows[0]:
        raise SourceError(f"{code} not in the Banco de España header")
    col = rows[0].index(code)
    out: Series = {}
    for row in rows[6:]:
        if not row or row[0].startswith(("FUENTE", "NOTAS")) or len(row) <= col:
            continue
        dd, mon, yyyy = row[0].split()
        if mon not in _ES_MONTHS:
            raise SourceError(f"bad Spanish date {row[0]!r}")
        v = _num(row[col])
        if v is not None:
            out[date(int(yyyy), _ES_MONTHS[mon], int(dd))] = v
    return out


def parse_rba_csv(payload: bytes, series_id: str) -> Series:
    rows = list(csv.reader(io.StringIO(_decode(payload))))
    ids = [r for r in rows[:12] if r and r[0] == "Series ID"]
    if not ids or series_id not in ids[0]:
        raise SourceError(f"{series_id} not in the RBA CSV")
    col = ids[0].index(series_id)
    start = rows.index(ids[0]) + 1
    out: Series = {}
    for row in rows[start:]:
        if not row or len(row) <= col or not row[0].strip():
            continue
        v = _num(row[col])
        if v is not None:
            out[datetime.strptime(row[0].strip(), "%d-%b-%Y").date()] = v
    return out


def parse_rba_xls(payload: bytes, series_id: str) -> Series:
    import xlrd  # type: ignore[import-untyped]  # optional dependency "h1"

    book = xlrd.open_workbook(file_contents=payload)
    sheet = book.sheet_by_name("Data")
    id_row = next(
        (r for r in range(min(15, sheet.nrows)) if sheet.cell_value(r, 0) == "Series ID"), None
    )
    if id_row is None:
        raise SourceError("Series ID row not found in the RBA XLS")
    ids = sheet.row_values(id_row)
    if series_id not in ids:
        raise SourceError(f"{series_id} not in the RBA XLS")
    col = ids.index(series_id)
    out: Series = {}
    for r in range(id_row + 1, sheet.nrows):
        dcell, vcell = sheet.cell(r, 0), sheet.cell(r, col)
        if dcell.ctype != xlrd.XL_CELL_DATE or vcell.ctype != xlrd.XL_CELL_NUMBER:
            continue  # empty cells and early target ranges written as text
        day = xlrd.xldate.xldate_as_datetime(dcell.value, book.datemode).date()
        out[day] = float(vcell.value)
    return out


def _xlsx_rows(payload: bytes, sheet: str) -> list[tuple[Any, ...]]:
    import openpyxl  # type: ignore[import-untyped]  # optional dependency "h1"

    wb = openpyxl.load_workbook(io.BytesIO(payload), read_only=True, data_only=True)
    ws = wb[sheet]
    ws.reset_dimensions()
    return [tuple(r) for r in ws.iter_rows(values_only=True)]


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def parse_rbnz_xlsx(payload: bytes, series_id: str) -> Series:
    rows = _xlsx_rows(payload, "Data")
    id_row = next((i for i, r in enumerate(rows[:10]) if r and r[0] == "Series Id"), None)
    if id_row is None or series_id not in rows[id_row]:
        raise SourceError(f"{series_id} not in the RBNZ file")
    col = rows[id_row].index(series_id)
    out: Series = {}
    for r in rows[id_row + 1 :]:
        if not r or len(r) <= col:
            continue
        d, v = _as_date(r[0]), r[col]
        if d is not None and isinstance(v, int | float):
            out[d] = float(v)
    return out


# Frozen text §1.3: BoE OIS spot curve, 1-month column (B); one sheet per inner file.
BOE_OIS_SHEETS = {
    "OIS daily data_2009 to 2015.xlsx": "2. spot curve",
    "OIS daily data_2016 to 2024.xlsx": "3. spot, short end",
    "OIS daily data_2025 to present.xlsx": "3. spot, short end",
}


def parse_boe_ois(payload: bytes) -> Series:
    out: Series = {}
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        names = set(z.namelist())
        if set(BOE_OIS_SHEETS) - names:
            raise SourceError(f"BoE OIS zip: missing {sorted(set(BOE_OIS_SHEETS) - names)}")
        for inner, sheet in BOE_OIS_SHEETS.items():
            rows = _xlsx_rows(z.read(inner), sheet)
            if len(rows) < 3 or rows[2][0] != "months:" or rows[2][1] != 1:
                raise SourceError(f"{inner}: column B is not the 1-month maturity")
            for r in rows[5:]:
                if not r or len(r) < 2:
                    continue
                d, v = _as_date(r[0]), r[1]
                if d is not None and isinstance(v, int | float):
                    out[d] = float(v)
    return out


def parse_riksbank(payload: bytes, value_key: str = "value") -> Series:
    data = json.loads(_decode(payload))
    if not isinstance(data, list):
        raise SourceError("Riksbank JSON is not a list")
    out: Series = {}
    for item in data:
        v = item.get(value_key)
        if v is not None:
            out[date.fromisoformat(item["date"])] = float(v)
    return out


def _fred(col: str) -> Callable[[bytes], Series]:
    return lambda p: parse_column_csv(p, "observation_date", col)


def _boe(col: str) -> Callable[[bytes], Series]:
    return lambda p: parse_column_csv(p, "DATE", col, date_format="%d %b %Y")


def _boc(col: str) -> Callable[[bytes], Series]:
    return lambda p: parse_column_csv(p, "date", col, start_marker="OBSERVATIONS")


def _ecb(p: bytes) -> Series:
    return parse_column_csv(p, "TIME_PERIOD", "OBS_VALUE")


def _norges(p: bytes) -> Series:
    return parse_column_csv(p, "TIME_PERIOD", "OBS_VALUE", delimiter=";")


# series key -> list of (file name, reader). Several files of one series are merged;
# a date present in two files must carry the same value.
SERIES: dict[str, list[tuple[str, Callable[[bytes], Series]]]] = {
    "DTB4WK": [("FRED_DTB4WK.csv", _fred("DTB4WK"))],
    "DFF": [("FRED_DFF.csv", _fred("DFF"))],
    "EURIBOR1M": [("BDE_ti_1_7.csv", lambda p: parse_bde(p, "D_DNBAC172"))],
    "EONIA": [("ECB_EON.D.EONIA_TO.RATE.csv", _ecb)],
    "ESTR": [("ECB_EST.B.EU000A2X2A25.WT.csv", _ecb)],
    "IUDVNEA": [("BOE_IUDVNEA.csv", _boe("IUDVNEA"))],
    "IUDSOIA": [("BOE_IUDSOIA.csv", _boe("IUDSOIA"))],
    "BOE_OIS1M": [("BOE_oisddata.zip", parse_boe_ois)],
    "FIRMMOIS1D": [
        ("RBA_F1_f01dhist.xls", lambda p: parse_rba_xls(p, "FIRMMOIS1D")),
        ("RBA_F1_f1-data.csv", lambda p: parse_rba_csv(p, "FIRMMOIS1D")),
    ],
    "FIRMMBAB30D": [
        ("RBA_F1_f01dhist.xls", lambda p: parse_rba_xls(p, "FIRMMBAB30D")),
        ("RBA_F1_f1-data.csv", lambda p: parse_rba_csv(p, "FIRMMBAB30D")),
    ],
    "FIRMMCRTD": [
        ("RBA_F1_f01dhist.xls", lambda p: parse_rba_xls(p, "FIRMMCRTD")),
        ("RBA_F1_f1-data.csv", lambda p: parse_rba_csv(p, "FIRMMCRTD")),
    ],
    "TB.CDN.30D.MID": [("BOC_TB.CDN.30D.MID.csv", _boc("TB.CDN.30D.MID"))],
    "AVG.INTWO": [("BOC_AVG.INTWO.csv", _boc("AVG.INTWO"))],
    "SETB1MBENCHC": [("RIKSBANK_SETB1MBENCHC.json", parse_riksbank)],
    "SECBREPOEFF": [("RIKSBANK_SECBREPOEFF.json", parse_riksbank)],
    "INM.DB01.NZZV": [
        ("RBNZ_hb2-daily-close-1985-2017.xlsx", lambda p: parse_rbnz_xlsx(p, "INM.DB01.NZZV")),
        ("RBNZ_hb2-daily-close.xlsx", lambda p: parse_rbnz_xlsx(p, "INM.DB01.NZZV")),
    ],
    "INM.DP1.N": [
        ("RBNZ_hb2-daily-close-1985-2017.xlsx", lambda p: parse_rbnz_xlsx(p, "INM.DP1.N")),
        ("RBNZ_hb2-daily-close.xlsx", lambda p: parse_rbnz_xlsx(p, "INM.DP1.N")),
    ],
    "SAR1M": [("SNB_zirepo.csv", lambda p: parse_snb(p, "H4"))],
    "SARON": [("SNB_zirepo.csv", lambda p: parse_snb(p, "H0"))],
    "IR/B.KPRA.SD.R": [("NORGESBANK_IR.B.KPRA.SD.R.csv", _norges)],
}


def eur_overnight(eonia: Series, estr: Series) -> Series:
    """EONIA to 2021-12-31, then ESTR + 8.5 bp (EONIA's own definition since 2019-10)."""

    out = {d: v for d, v in eonia.items() if d <= EONIA_LAST_DAY}
    out.update({d: v + ESTR_TO_EONIA_BP / 100.0 for d, v in estr.items() if d > EONIA_LAST_DAY})
    return out


def load_rates(root: Path, end: date) -> tuple[dict[str, Series], dict[str, str], dict[str, Any]]:
    """All series of ``SERIES`` (+ EUR_ON) with dates before ``end``; file hashes; spans."""

    root = Path(root)
    cache: dict[str, bytes] = {}
    hashes: dict[str, str] = {}
    out: dict[str, Series] = {}
    for key, parts in SERIES.items():
        merged: Series = {}
        for name, reader in parts:
            if name not in cache:
                path = root / name
                if not path.is_file():
                    raise SourceError(f"missing rate file {path}")
                cache[name] = path.read_bytes()
                hashes[name] = hashlib.sha256(cache[name]).hexdigest()
            for d, v in reader(cache[name]).items():
                if d >= end:
                    continue
                if d in merged and abs(merged[d] - v) > 1e-9:
                    raise SourceError(f"{key}: two files disagree on {d}")
                merged[d] = v
        out[key] = merged
    out["EUR_ON"] = eur_overnight(out.pop("EONIA"), out.pop("ESTR"))
    info = {
        k: {
            "first": min(v).isoformat() if v else None,
            "last": max(v).isoformat() if v else None,
            "values": len(v),
        }
        for k, v in sorted(out.items())
    }
    return out, hashes, info


def load_calendar(path: Path) -> tuple[list[Meeting], str]:
    payload = Path(path).read_bytes()
    rows = list(csv.DictReader(io.StringIO(_decode(payload))))
    need = {"bank", "announcement_date_local", "scheduled", "decision"}
    if not rows or need - set(rows[0]):
        raise SourceError(f"meetings.csv must have the columns {sorted(need)}")
    out = []
    for r in rows:
        if r["bank"] not in BANKS:
            raise SourceError(f"unknown bank {r['bank']!r}")
        if r["scheduled"] not in ("yes", "no"):
            raise SourceError(f"scheduled must be yes/no: {r['scheduled']!r}")
        if r["decision"] not in ("hike", "cut", "hold"):
            raise SourceError(f"decision must be hike/cut/hold: {r['decision']!r}")
        out.append(
            Meeting(
                r["bank"],
                date.fromisoformat(r["announcement_date_local"]),
                r["scheduled"] == "yes",
                r["decision"],
            )
        )
    return out, hashlib.sha256(payload).hexdigest()


__all__ = ["SERIES", "SourceError", "load_calendar", "load_rates", "eur_overnight"]
