import pandas as pd


def test_midnight_edge_case():
    d = pd.to_datetime("2023-08-01T00:00:00")
    assert d.hour == 0


def test_parse_various_formats():
    assert pd.to_datetime("01/08/2023", dayfirst=True).date().isoformat() == "2023-08-01"
    assert pd.to_datetime("2023-08-01T15:00:00Z").tz_convert(None).date().isoformat() == "2023-08-01"
