"""CSV text from visual.exportData() -> tidy DataFrame."""

import io
import re

import pandas as pd

_TOTAL_LABELS = {"total", "grand total"}
# Plain numbers only, optionally with thousands separators. Leading zeros ("007"), signs, currency
# and percentages fail the match so ID-like columns stay text.
_NUMBER = r"-?(0|[1-9]\d{0,2}(,\d{3})+|[1-9]\d*)(\.\d+)?"


def clean_csv(csv_text: str) -> pd.DataFrame:
    """Tidy exported CSV; columns whose every value is a plain number become numeric, the rest stay text."""
    df = pd.read_csv(io.StringIO(csv_text.lstrip("\ufeff")), dtype=str, keep_default_na=False)
    df.columns = [re.sub(r"\s+", " ", c).strip() for c in df.columns]
    df = df.apply(lambda s: s.str.strip())
    df = df.replace("", pd.NA).dropna(how="all")
    if len(df) and str(df.iloc[-1, 0]).strip().lower() in _TOTAL_LABELS:
        df = df.iloc[:-1]
    for col in df.columns:
        values = df[col].dropna()
        if len(values) and values.str.fullmatch(_NUMBER).all():
            df[col] = pd.to_numeric(df[col].str.replace(",", "", regex=False))
    return df.reset_index(drop=True)
