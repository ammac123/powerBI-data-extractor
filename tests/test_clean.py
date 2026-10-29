import pandas as pd

from pbi_interaction.clean import clean_csv


def test_strips_bom_whitespace_and_totals():
    df = clean_csv("\ufeffName ,  Sales\n a ,\"1,000\"\nb,2\nTotal,1002\n")
    assert list(df.columns) == ["Name", "Sales"]
    assert df["Name"].tolist() == ["a", "b"]
    assert df["Sales"].tolist() == [1000, 2]


def test_leading_zero_ids_stay_text():
    df = clean_csv("id,n\n007,1\n010,2\n")
    assert df["id"].tolist() == ["007", "010"]
    assert pd.api.types.is_numeric_dtype(df["n"])


def test_mixed_and_symbol_columns_stay_text():
    df = clean_csv("a,b,c\n1,$5,x\n2,$6,3\n")
    assert pd.api.types.is_numeric_dtype(df["a"])
    assert df["b"].tolist() == ["$5", "$6"]
    assert df["c"].tolist() == ["x", "3"]


def test_decimals_and_blank_rows():
    df = clean_csv("a,b\n1.5,\n,\n2,3\n")
    assert df["a"].tolist() == [1.5, 2.0]
    assert len(df) == 2
