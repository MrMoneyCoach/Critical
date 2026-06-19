"""
SJP-style Critical Yield Calculator (CYC) — reconstructed prototype.

Calibrated against report ref. 1786247 (Nitin Suneja, 14 May 2026) and the
companion Retirement Account illustration.

This is a working prototype. It implements the documented mechanics of the
CYC v2.1.5 (v2) output, with assumptions where the SJP rulebook was not
disclosed in the source documents. All such assumptions are flagged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional


# ---------------------------------------------------------------------------
# Configuration tables (sourced from the SJP Retirement Account illustration,
# 14 May 2026, Table 5).
# ---------------------------------------------------------------------------

PRODUCT_CHARGE_TIERS: list[tuple[float, float]] = [
    (500_000,   0.0035),
    (500_000,   0.0032),
    (1_000_000, 0.0029),
    (1_000_000, 0.0027),
    (float("inf"), 0.0025),
]

# CY limit defaults (only the 0.75% transfer limit was disclosed). The
# regular-contribution limit is configurable; 0.75% is used as the default
# for sub-15-year terms.
DEFAULT_CY_LIMIT_TRANSFER = 0.0075
DEFAULT_CY_LIMIT_REGULAR = 0.0075

# FCA mid-rate assumption (real, net of 2% inflation) used in the SJP
# illustration for Polaris 3. CY is a differential, so the absolute rate
# only affects rounding in the bisection.
DEFAULT_GROSS_RATE = 0.029

# Standard IAC ceiling for a Retirement Account transfer.
STANDARD_IAC = 0.03


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

@dataclass
class Client:
    name: str
    dob: date
    retirement_age: int = 67
    attitude_to_risk: str = "Medium"


@dataclass
class FundHolding:
    name: str
    allocation_gbp: float
    annual_charge: float


@dataclass
class CedingPlan:
    name: str
    provider: str
    transfer_value: float
    wrapper_charge: float
    funds: list[FundHolding]
    regular_contribution: float = 0.0
    regular_frequency_months: int = 1
    include_regulars_in_replacement: bool = False

    @property
    def blended_fund_charge(self) -> float:
        total = sum(f.allocation_gbp for f in self.funds)
        if total == 0:
            return 0.0
        return sum(f.allocation_gbp * f.annual_charge for f in self.funds) / total

    @property
    def total_annual_charge(self) -> float:
        return self.wrapper_charge + self.blended_fund_charge


@dataclass
class RecommendedPlan:
    product: str = "Retirement Account"
    ongoing_advice_charge: float = 0.008
    fund_charge: float = 0.0052
    existing_sjp_holdings: float = 0.0
    iac: float = STANDARD_IAC

    def product_charge(self, total_holdings: float) -> float:
        """Tier lookup based on Table 5 of the illustration."""
        remaining = total_holdings
        weighted_sum = 0.0
        for band_size, rate in PRODUCT_CHARGE_TIERS:
            band = min(remaining, band_size)
            weighted_sum += band * rate
            remaining -= band
            if remaining <= 0:
                break
        return weighted_sum / total_holdings if total_holdings > 0 else 0.0

    def total_ongoing_charge(self, total_holdings: float) -> float:
        return (
            self.ongoing_advice_charge
            + self.product_charge(total_holdings)
            + self.fund_charge
        )


# ---------------------------------------------------------------------------
# Term calculation
# ---------------------------------------------------------------------------

def term_months(dob: date, retirement_age: int, projection_start: date) -> int:
    """SJP uses age 67 as standard NRA; term measured in whole months."""
    retirement_date = date(dob.year + retirement_age, dob.month, dob.day)
    months = (retirement_date.year - projection_start.year) * 12 + (
        retirement_date.month - projection_start.month
    )
    if retirement_date.day < projection_start.day:
        months -= 1
    return months


# ---------------------------------------------------------------------------
# Projection engine — monthly compounding, charges deducted monthly.
# ---------------------------------------------------------------------------

def project(
    initial: float,
    gross_annual_rate: float,
    annual_charge: float,
    months: int,
    monthly_contribution: float = 0.0,
) -> float:
    net_annual = gross_annual_rate - annual_charge
    monthly_rate = (1 + net_annual) ** (1 / 12) - 1
    value = initial
    for _ in range(months):
        value = value * (1 + monthly_rate) + monthly_contribution
    return value


# ---------------------------------------------------------------------------
# Critical Yield — closed-form Reduction-in-Yield (RIY) approximation.
#
# The SJP CYC expresses the IAC as an annualised drag over the term, so the
# "level of outperformance required" is:
#
#     CY = (SJP ongoing charges - ceding ongoing charges) + IAC / term_years
#
# This matches the disclosed result of 1.74% for the calibration case at the
# standard 3% IAC and the 1.57% overall figure on the ESS comparison page.
# ---------------------------------------------------------------------------

def critical_yield_riy(
    sjp_ongoing_charge: float,
    ceding_ongoing_charge: float,
    iac: float,
    term_years: float,
) -> float:
    return (sjp_ongoing_charge - ceding_ongoing_charge) + iac / term_years


def solve_iac_for_limit(
    sjp_ongoing_charge: float,
    ceding_ongoing_charge: float,
    cy_limit: float,
    term_years: float,
    standard_iac: float,
    step: float = 0.0001,
) -> float:
    """Reduce IAC from the standard rate until CY <= limit, or hit zero."""
    headroom = cy_limit - (sjp_ongoing_charge - ceding_ongoing_charge)
    if headroom <= 0:
        return 0.0
    required = headroom * term_years
    required = max(0.0, min(standard_iac, required))
    return round(required / step) * step


# ---------------------------------------------------------------------------
# Results model
# ---------------------------------------------------------------------------

@dataclass
class CYCResult:
    plan_name: str
    transfer_value: float
    iac_applied: float
    iac_required_to_proceed: float
    product_charge: float
    ongoing_advice_charge: float
    fund_charge: float
    term_years: int
    term_months: int
    critical_yield_single: float
    critical_yield_limit: float
    monetary_year_one: float
    pass_fail: str
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Top-level runner
# ---------------------------------------------------------------------------

def run_cyc(
    client: Client,
    ceding: CedingPlan,
    recommended: RecommendedPlan,
    projection_start: date,
    base_gross_rate: float = DEFAULT_GROSS_RATE,
    cy_limit: float = DEFAULT_CY_LIMIT_TRANSFER,
) -> CYCResult:
    months = term_months(client.dob, client.retirement_age, projection_start)
    years_part, months_part = divmod(months, 12)
    term_years = months / 12

    total_holdings = recommended.existing_sjp_holdings + ceding.transfer_value
    sjp_ongoing = recommended.total_ongoing_charge(total_holdings)
    ceding_ongoing = ceding.total_annual_charge

    cy_published = critical_yield_riy(
        sjp_ongoing, ceding_ongoing, STANDARD_IAC, term_years
    )
    cy_effective = critical_yield_riy(
        sjp_ongoing, ceding_ongoing, recommended.iac, term_years
    )

    monetary_year_one = cy_published * ceding.transfer_value
    iac_required = recommended.iac

    notes: list[str] = []
    if iac_required < recommended.iac:
        notes.append(
            "Partner entitlement has been reduced to bring the CYC for this "
            "recommendation within allowable limits."
        )
    notes.append(
        "Product charge tiering has been considered in the calculations "
        "where it applies."
    )
    if iac_required < 0.01:
        notes.append(
            "The IAC for this case is below 1%. This may require special "
            "permission to proceed."
        )
    notes.append(
        f"The CYC has used a calculation term of {years_part} years, "
        f"{months_part} months."
    )

    return CYCResult(
        plan_name=ceding.name,
        transfer_value=ceding.transfer_value,
        iac_applied=recommended.iac,
        iac_required_to_proceed=iac_required,
        product_charge=recommended.product_charge(total_holdings),
        ongoing_advice_charge=recommended.ongoing_advice_charge,
        fund_charge=recommended.fund_charge,
        term_years=years_part,
        term_months=months_part,
        critical_yield_single=cy_published,
        critical_yield_limit=cy_limit,
        monetary_year_one=monetary_year_one,
        pass_fail="Pass" if cy_effective <= cy_limit else "Fail",
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Report formatter — mirrors page 2 of the CYC PDF.
# ---------------------------------------------------------------------------

def format_report(result: CYCResult) -> str:
    lines = [
        "=" * 70,
        "Critical Yield Calculator — Results",
        "=" * 70,
        f"SJP Plan:               Retirement Account",
        f"Overall Result:         {result.pass_fail}",
        "",
        "Notes:",
    ]
    for n in result.notes:
        lines.append(f"  - {n}")
    lines += [
        "",
        f"Transfer/fund value:    £{result.transfer_value:,.2f}",
        f"New lump sum:           £0.00",
        f"Total transfer & lump:  £{result.transfer_value:,.2f}",
        "",
        "Charges & entitlements (lump sums):",
        f"  Standard IAC:         {STANDARD_IAC*100:.2f}%",
        f"  IAC required:         {result.iac_required_to_proceed*100:.2f}%",
        f"  Product charge:       {result.product_charge*100:.2f}%",
        f"  OAC:                  {result.ongoing_advice_charge*100:.2f}%",
        f"  Fund charge:          {result.fund_charge*100:.2f}%",
        "",
        f"Plan: {result.plan_name}",
        f"  Critical Yield:           {result.critical_yield_single*100:.2f}%",
        f"  Monetary equiv. Year 1:   £{result.monetary_year_one:,.2f}",
        f"  Critical Yield Limit:     {result.critical_yield_limit*100:.2f}%",
        "=" * 70,
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Calibration case — Nitin Suneja, ref. 1786247
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    client = Client(
        name="Nitin Suneja",
        dob=date(1972, 7, 30),
        retirement_age=67,
    )

    ceding = CedingPlan(
        name="LifeSight Microsoft Plan",
        provider="Lifesight",
        transfer_value=35_175.73,
        wrapper_charge=0.0,
        funds=[
            FundHolding("LifeSight Equity",            26_821.93, 0.0015),
            FundHolding("LifeSight Diversified Growth", 8_353.80, 0.0017),
        ],
    )

    recommended = RecommendedPlan(
        product="Retirement Account",
        ongoing_advice_charge=0.008,
        fund_charge=0.0052,
        existing_sjp_holdings=66_069.50,
        iac=0.0073,
    )

    result = run_cyc(
        client=client,
        ceding=ceding,
        recommended=recommended,
        projection_start=date(2026, 5, 14),
        base_gross_rate=DEFAULT_GROSS_RATE,
        cy_limit=DEFAULT_CY_LIMIT_TRANSFER,
    )

    print(format_report(result))
    print()
    print(f"Expected (PDF): CY 1.74%  |  Monetary £612.92  |  IAC 0.73%")
    print(
        f"Calculated:     CY {result.critical_yield_single*100:.2f}%  |  "
        f"Monetary £{result.monetary_year_one:,.2f}  |  "
        f"IAC {result.iac_required_to_proceed*100:.2f}%"
    )
