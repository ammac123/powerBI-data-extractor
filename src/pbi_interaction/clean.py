"""CSV text from visual.exportData() -> tidy DataFrame."""

from __future__ import annotations

import io
import re

import pandas as pd

_TOTAL_LABELS = {"total", "grand total"}


def clean_csv(csv_text: str) -> pd.DataFrame:
    df = pd.read_csv(io.StringIO(csv_text.lstrip("﻿")), dtype=str, keep_default_na=False)
    df.columns = [re.sub(r"\s+", " ", c).strip() for c in df.columns]
    df = df.apply(lambda s: s.str.strip())
    df = df.replace("", pd.NA).dropna(how="all")
    # Drop a trailing totals row if the first column says so.
    if len(df) and str(df.iloc[-1, 0]).strip().lower() in _TOTAL_LABELS:
        df = df.iloc[:-1]
    # Best-effort numeric coercion; columns that do not fully parse stay as strings.
    for col in df.columns:
        as_num = pd.to_numeric(df[col].str.replace(",", "", regex=False), errors="coerce")
        if as_num.notna().sum() == df[col].notna().sum():
            df[col] = as_num
    return df.reset_index(drop=True)
