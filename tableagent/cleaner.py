"""Layer 1: spreadsheet → CleanTable with issues + _dedup_strings."""
from __future__ import annotations
import re, unicodedata
from dataclasses import dataclass, field
from pathlib import Path
import pandas as pd
import duckdb


@dataclass
class CleanTable:
    name: str
    df: pd.DataFrame
    source: str
    issues: list[dict] = field(default_factory=list)


def _sanitize_name(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9_]", "_", s)
    return re.sub(r"_+", "_", s).strip("_").lower()


def _detect_header(df_raw: pd.DataFrame) -> int:
    for i in range(min(20, len(df_raw))):
        row = df_raw.iloc[i]
        non_null = row.dropna()
        if len(non_null) >= max(2, len(row) * 0.5):
            if all(isinstance(v, str) for v in non_null):
                return i
    return 0


def _normalize_headers(columns: list) -> list[str]:
    out = []
    for j, c in enumerate(columns):
        s = str(c) if c is not None and str(c) != "nan" else f"col_{j}"
        s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
        s = re.sub(r"[^a-zA-Z0-9_]", "_", s)
        s = re.sub(r"_+", "_", s).strip("_").upper()
        # dedup consecutive words
        parts = s.split("_")
        deduped = [parts[0]] if parts else []
        for p in parts[1:]:
            if p != deduped[-1]:
                deduped.append(p)
        s = "_".join(deduped) if deduped else f"COL_{j}"
        # unique
        base = s
        k = 2
        while s in out:
            s = f"{base}_{k}"; k += 1
        out.append(s)
    return out


def _infer_types(df: pd.DataFrame) -> None:
    for col in df.columns:
        if not pd.api.types.is_object_dtype(df[col]):
            continue
        # try datetime
        try:
            parsed = pd.to_datetime(df[col], dayfirst=True, errors="coerce", format="mixed")
            if parsed.notna().sum() > len(df) * 0.5:
                df[col] = parsed; continue
        except Exception:
            pass
        # try numeric
        try:
            cleaned = df[col].astype(str).str.replace(r"[R$\s]", "", regex=True)
            # Only strip dots that are thousand separators (dot followed by exactly 3 digits)
            cleaned = cleaned.str.replace(r"\.(\d{3})(?!\d)", r"\1", regex=True)
            cleaned = cleaned.str.replace(",", ".")
            parsed = pd.to_numeric(cleaned, errors="coerce")
            if parsed.notna().sum() > len(df) * 0.5:
                df[col] = parsed
        except Exception:
            pass


def _dedup_strings(df: pd.DataFrame, issues: list[dict]) -> None:
    """Strip whitespace and flag near-duplicate string values for user review."""
    _date_pat = re.compile(r"^\d{4}[-/]\d{2}[-/]\d{2}")
    for col in df.columns:
        if not pd.api.types.is_object_dtype(df[col]):
            continue
        mask = df[col].notna()
        df.loc[mask, col] = df.loc[mask, col].astype(str).str.strip()
        # Skip columns where most values look like dates
        sample = df[col].dropna().head(20)
        if len(sample) > 0 and sample.astype(str).str.match(_date_pat).sum() > len(sample) * 0.5:
            continue
        counts = df[col].value_counts()
        if len(counts) < 2:
            continue
        seen = list(counts.index)
        flagged = set()
        for i, a in enumerate(seen):
            if not isinstance(a, str):
                continue
            ta = a.lower().split()
            for b in seen[i + 1:]:
                if not isinstance(b, str) or (a, b) in flagged:
                    continue
                tb = b.lower().split()
                matched = sum(1 for wa in ta if any(
                    wa == wb or (len(wa) >= 3 and len(wb) >= 3 and
                                 (wa.startswith(wb[:3]) or wb.startswith(wa[:3])))
                    for wb in tb))
                union = max(len(ta), len(tb))
                if union > 0 and matched / union >= 0.75:
                    flagged.add((a, b))
                    keep = a if counts.get(a, 0) >= counts.get(b, 0) else b
                    variant = b if keep == a else a
                    issues.append({
                        "column": col, "type": "dedup",
                        "value": variant, "suggestion": keep,
                        "rows": [], "count": int(counts.get(variant, 0)),
                        "suggestion_count": int(counts.get(keep, 0)),
                    })


def _register_duckdb(t: CleanTable, conn: duckdb.DuckDBPyConnection) -> None:
    try: conn.execute(f'DROP VIEW IF EXISTS "{t.name}"')
    except: pass
    try: conn.execute(f'DROP TABLE IF EXISTS "{t.name}"')
    except: pass
    conn.register("__tmp", t.df)
    conn.execute(f'CREATE TABLE "{t.name}" AS SELECT * FROM __tmp')
    conn.unregister("__tmp")


def _clean_one_sheet(df_raw: pd.DataFrame, sheet_name: str, source: str,
                     conn: duckdb.DuckDBPyConnection) -> CleanTable | None:
    if df_raw.empty or df_raw.shape[1] < 2:
        return None
    # ST-1: header
    hdr = _detect_header(df_raw)
    if hdr > 0:
        df_raw.columns = df_raw.iloc[hdr]
        df_raw = df_raw.iloc[hdr + 1:].reset_index(drop=True)
    # ST-2: normalize headers
    df_raw.columns = _normalize_headers(list(df_raw.columns))
    # ST-3: drop empty
    df_raw = df_raw.dropna(how="all").dropna(axis=1, how="all").reset_index(drop=True)
    if df_raw.empty:
        return None
    # ST-4: type inference
    _infer_types(df_raw)
    # ST-5: dedup strings
    issues: list[dict] = []
    _dedup_strings(df_raw, issues)
    name = _sanitize_name(sheet_name)
    t = CleanTable(name=name, df=df_raw, source=source, issues=issues)
    # ST-6: register in DuckDB
    _register_duckdb(t, conn)
    return t


def _is_empty_row(row):
    return row.isna().all() or (row.astype(str).str.strip().replace("", pd.NA).isna().all())


def _split_sub_tables(df_raw: pd.DataFrame, sheet_name: str) -> list[tuple[str, pd.DataFrame]]:
    """Split a sheet with multiple logical tables (separated by empty rows) into parts."""
    if df_raw.empty:
        return []

    # Find empty row indices
    empty_mask = df_raw.apply(_is_empty_row, axis=1)
    # Split into blocks of consecutive non-empty rows
    blocks, current = [], []
    for i in range(len(df_raw)):
        if empty_mask.iloc[i]:
            if current:
                blocks.append(current); current = []
        else:
            current.append(i)
    if current:
        blocks.append(current)

    if len(blocks) <= 1:
        return [(sheet_name, df_raw)]  # single table, no splitting needed

    results = []
    global_title = ""
    for block_rows in blocks:
        sub = df_raw.iloc[block_rows].reset_index(drop=True)
        ncols = sub.shape[1]

        # Separate title rows (≤1 non-null) from potential header/data rows
        title_rows, rest_start = [], 0
        for j in range(len(sub)):
            non_null = sub.iloc[j].dropna()
            non_null = non_null[non_null.astype(str).str.strip() != ""]
            if len(non_null) <= 1:
                title_rows.append(sub.iloc[j])
                rest_start = j + 1
            else:
                break

        # Extract section title from title rows
        section_title = ""
        for tr in title_rows:
            val = tr.dropna()
            val = val[val.astype(str).str.strip() != ""]
            if len(val) == 1:
                t = str(val.iloc[0]).strip()
                if len(t) > 2:
                    section_title = t

        if not section_title and title_rows:
            # First block might be a global title
            for tr in title_rows:
                val = tr.dropna()
                val = val[val.astype(str).str.strip() != ""]
                if len(val) == 1:
                    global_title = str(val.iloc[0]).strip()

        rest = sub.iloc[rest_start:].reset_index(drop=True)
        if rest.empty or len(rest) < 1:
            continue

        # Detect header rows: consecutive rows at top where all non-null values are short text (not dates/numbers)
        hdr_end = 0
        _date_re = re.compile(r"^\d{4}[-/]\d{2}[-/]\d{2}")
        for j in range(min(3, len(rest))):
            non_null = rest.iloc[j].dropna()
            non_null = non_null[non_null.astype(str).str.strip() != ""]
            if len(non_null) >= 1 and all(
                isinstance(v, str)
                and not v.replace(".", "").replace(",", "").replace("-", "").replace(" ", "").isdigit()
                and not _date_re.match(v)
                for v in non_null
            ):
                hdr_end = j + 1
            else:
                break

        if hdr_end == 0:
            # First block: headers may already be in df_raw.columns (consumed by pandas)
            if block_rows == blocks[0]:
                data = rest.copy()
                orig_cols = list(df_raw.columns)
                # Check if original columns look like real headers (not generic 0,1,2...)
                if any(isinstance(c, str) and not c.startswith('Unnamed') for c in orig_cols):
                    data.columns = orig_cols[:data.shape[1]]
                    name = section_title or f"{sheet_name}_{len(results)+1}"
                    results.append((name, data))
            continue  # no header found, skip block

        # Merge multi-row headers: last non-null value per column
        merged_header = [None] * ncols
        for j in range(hdr_end):
            for c in range(ncols):
                val = rest.iloc[j, c]
                if pd.notna(val) and str(val).strip():
                    merged_header[c] = str(val).strip()

        data = rest.iloc[hdr_end:].reset_index(drop=True)
        if data.empty:
            continue

        data.columns = merged_header
        name = section_title or f"{sheet_name}_{len(results)+1}"
        results.append((name, data))

    return results if results else [(sheet_name, df_raw)]


def clean_spreadsheet(path: str, conn: duckdb.DuckDBPyConnection) -> list[CleanTable]:
    p = Path(path)
    tables: list[CleanTable] = []
    if p.suffix.lower() == ".csv":
        df = pd.read_csv(path, dtype=str)
        t = _clean_one_sheet(df, p.stem, path, conn)
        if t: tables.append(t)
    else:
        sheets = pd.read_excel(path, sheet_name=None, dtype=str, engine="openpyxl")
        for sheet_name, df in sheets.items():
            sub_tables = _split_sub_tables(df, sheet_name)
            if len(sub_tables) == 1 and sub_tables[0][0] == sheet_name:
                # Single table, use normal flow
                t = _clean_one_sheet(df, sheet_name, path, conn)
                if t: tables.append(t)
            else:
                for name, sub_df in sub_tables:
                    t = _clean_one_sheet(sub_df, name, path, conn)
                    if t: tables.append(t)
    return tables
