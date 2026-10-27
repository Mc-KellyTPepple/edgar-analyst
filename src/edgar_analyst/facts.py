"""Turn SEC XBRL 'company facts' into a clean table of annual figures."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

# canonical metric -> (label, kind, us-gaap tags in priority order)
# Companies change tags over time, so for each fiscal period we take the
# highest-priority tag that has a value.
METRICS: dict[str, tuple[str, str, list[str]]] = {
    "revenue": ("Revenue", "duration", [
        "RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
        "SalesRevenueNet", "RevenueFromContractWithCustomerIncludingAssessedTax"]),
    "gross_profit": ("Gross profit", "duration", ["GrossProfit"]),
    "operating_income": ("Operating income", "duration", ["OperatingIncomeLoss"]),
    "net_income": ("Net income", "duration", ["NetIncomeLoss", "ProfitLoss"]),
    "rd_expense": ("Research and development expense", "duration", [
        "ResearchAndDevelopmentExpense", "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost"]),
    "operating_cash_flow": ("Net cash provided by operating activities", "duration", [
        "NetCashProvidedByUsedInOperatingActivities"]),
    "capex": ("Capital expenditures", "duration", ["PaymentsToAcquirePropertyPlantAndEquipment"]),
    "total_assets": ("Total assets", "instant", ["Assets"]),
    "total_liabilities": ("Total liabilities", "instant", ["Liabilities"]),
    "cash": ("Cash and cash equivalents", "instant", ["CashAndCashEquivalentsAtCarryingValue"]),
}


@dataclass(frozen=True)
class Fact:
    metric: str
    period_start: str | None
    period_end: str
    value: float
    unit: str
    accession: str
    form: str
    filed: str
    tag: str


def _days(start: str, end: str) -> int:
    return (date.fromisoformat(end) - date.fromisoformat(start)).days


def extract_annual_facts(companyfacts: dict) -> list[Fact]:
    """Annual (10-K, full fiscal year) USD figures, one per metric and period end.

    - Quarterly figures and 10-Q data are ignored.
    - 52/53-week fiscal years are accepted (350 to 380 days).
    - If a figure was restated in a later 10-K, the most recently filed value wins.
    """
    gaap = companyfacts.get("facts", {}).get("us-gaap", {})
    best: dict[tuple[str, str], tuple[int, Fact]] = {}
    for metric, (_label, kind, tags) in METRICS.items():
        for prio, tag in enumerate(tags):
            for p in gaap.get(tag, {}).get("units", {}).get("USD", []):
                if p.get("form") != "10-K" or p.get("fp") != "FY":
                    continue
                start, end, val = p.get("start"), p.get("end"), p.get("val")
                if end is None or val is None:
                    continue
                if kind == "duration":
                    if not start or not (350 <= _days(start, end) <= 380):
                        continue
                elif start:
                    continue
                fact = Fact(metric, start, end, float(val), "USD", p.get("accn", ""), "10-K", p.get("filed", ""), tag)
                key = (metric, end)
                cur = best.get(key)
                if cur is None or prio < cur[0] or (prio == cur[0] and fact.filed > cur[1].filed):
                    best[key] = (prio, fact)
    return sorted((f for _p, f in best.values()), key=lambda f: (f.metric, f.period_end))


def extract_10k_filings(submissions: dict) -> list[dict]:
    """10-K filings listed in the 'recent' block of an EDGAR submissions file."""
    recent = submissions.get("filings", {}).get("recent", {})
    out = []
    for i, form in enumerate(recent.get("form", [])):
        if form != "10-K":
            continue
        out.append({
            "accession": recent["accessionNumber"][i],
            "form": form,
            "filed": recent["filingDate"][i],
            "report_date": recent.get("reportDate", [None] * (i + 1))[i],
            "primary_doc": recent["primaryDocument"][i],
        })
    return out
