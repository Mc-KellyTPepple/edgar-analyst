"""Synthetic SEC-shaped data for tests. Company names and numbers are made up."""
from __future__ import annotations

from datetime import date, timedelta

ALPHA_ENDS = ["2020-12-31", "2021-12-31", "2022-12-31", "2023-12-31", "2024-12-31"]
BETA_ENDS = ["2021-01-31", "2022-01-30", "2023-01-29", "2024-01-28", "2025-01-26"]

# $ millions per fiscal year
ALPHA = {
    "revenue": [1000, 1200, 1500, 1800, 2400],
    "rd_expense": [200, 230, 270, 300, 340],
    "operating_income": [150, 210, 300, 420, 720],
    "net_income": [120, 170, 250, 350, 600],
    "operating_cash_flow": [160, 220, 310, 430, 700],
    "total_assets": [2000, 2300, 2700, 3200, 4000],
}
BETA = {
    "revenue": [5000, 5200, 4900, 5300, 5600],
    "rd_expense": [400, 450, 500, 520, 560],
    "operating_income": [900, 800, 500, 600, 800],
    "net_income": [700, 650, 400, 480, 650],
    "operating_cash_flow": [950, 850, 600, 700, 900],
    "total_assets": [9000, 9300, 9500, 9900, 10200],
}
TAGS = {
    "revenue": "RevenueFromContractWithCustomerExcludingAssessedTax",
    "rd_expense": "ResearchAndDevelopmentExpense",
    "operating_income": "OperatingIncomeLoss",
    "net_income": "NetIncomeLoss",
    "operating_cash_flow": "NetCashProvidedByUsedInOperatingActivities",
    "total_assets": "Assets",
}
INSTANT = {"total_assets"}


def _point(end, val, accn, filed, instant, form="10-K", fp="FY", days=364):
    p = {"end": end, "val": val, "accn": accn, "fy": int(end[:4]), "fp": fp, "form": form, "filed": filed}
    if not instant:
        p["start"] = (date.fromisoformat(end) - timedelta(days=days)).isoformat()
    return p


def make_companyfacts(cik: int, name: str, ends: list[str], values: dict[str, list[float]]) -> dict:
    gaap: dict = {}
    for metric, series in values.items():
        pts = []
        for i, (end, v) in enumerate(zip(ends, series)):
            filed = (date.fromisoformat(end) + timedelta(days=45)).isoformat()
            pts.append(_point(end, v * 1e6, f"{cik:010d}-{end[2:4]}-{i:06d}", filed, metric in INSTANT))
        gaap[TAGS[metric]] = {"units": {"USD": pts}}
    # noise that must be ignored
    rev = gaap[TAGS["revenue"]]["units"]["USD"]
    rev.append(_point(ends[-1], 1, "q-1", "2025-05-01", False, form="10-Q", fp="Q1", days=90))      # 10-Q
    rev.append(_point(ends[-1], 2, "q-2", "2025-02-01", False, days=90))                              # 90-day period inside a 10-K
    # a restatement of the oldest year, filed later with a different value: the later filing must win
    rev.append(_point(ends[0], values["revenue"][0] * 1e6 + 5e6, "restated", "2030-01-01", False))
    return {"cik": cik, "entityName": name, "facts": {"us-gaap": gaap}}
