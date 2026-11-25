"""Build config/tax_data/: US taxes at each salary, per state and locality, from PolicyEngine-US,
for every household the Settings page offers.

    pip install "policyengine-us==2.88.9"   # Python 3.10-3.14; not a jobalerts dependency, only for rebuilding
    # (with a fresh environment, add uv's --exclude-newer 2026-10-31 so policyengine-core matches)
    python tools/build_tax_data.py [--config config] [--year 2026] [--only joint-2-1]

One gzipped table per household: filing (single or joint; one earner) x children under 17 (0-4) x
other dependents (0-1), e.g. tax_data/joint-2-1.json.gz. The localities come from config/taxes.yaml.
For each place this runs one PolicyEngine simulation holding one household per salary on the grid,
and stores federal income tax, Social Security + Medicare, state income tax, state payroll taxes and
local income taxes, in whole dollars, with no 401(k): jobalerts takes the 401(k) share off the
income that each tax is levied on at runtime (taxes.py). It interpolates between grid points.
Rebuild each January, once PolicyEngine has the new year's brackets (all 20 take ~20 minutes).
"""
from __future__ import annotations

import argparse
import gzip
import json
from datetime import date
from importlib.metadata import version
from pathlib import Path

import yaml

STATES = ["AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS",
          "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC",
          "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY"]
WAGES = [0] + list(range(20_000, 400_001, 5_000)) + list(range(425_000, 1_000_001, 25_000))
PARTS = ["federal", "fica", "state", "state_payroll", "local"]
CHILD_AGES = [6, 0, 10, 14]      # children under 17 (child tax credit), in the order they are added
OTHER_AGE = 18                   # an older dependent (the $500 credit for other dependents)


def households() -> list[dict]:
    return [{"filing": filing, "children": c, "other_dependents": o}
            for filing in ("single", "joint") for c in range(5) for o in range(2)]


def variant_name(h: dict) -> str:
    return f"{h['filing']}-{h['children']}-{h['other_dependents']}"


def situation(state: str, household: dict, inputs: dict, year: int) -> dict:
    """One household per grid salary: an earner (with a spouse who has no income when filing jointly)
    and the dependents. PolicyEngine treats everyone in the tax unit other than the head and spouse
    as a dependent, and picks the filing status (head of household for a single parent)."""
    people, units = {}, {"tax_units": {}, "families": {}, "spm_units": {}, "marital_units": {}, "households": {}}
    ages = CHILD_AGES[:household["children"]] + [OTHER_AGE] * household["other_dependents"]
    for i, wage in enumerate(WAGES):
        you = f"you{i}"
        person = {"age": {year: 40}, "employment_income": {year: wage}, "traditional_401k_contributions": {year: 0}}
        for var, value in (inputs.get("person") or {}).items():
            person[var] = {year: wage if value == "$wage" else value}
        people[you] = person
        adults = [you]
        if household["filing"] == "joint":
            people[f"spouse{i}"] = {"age": {year: 40}}
            adults.append(f"spouse{i}")
        kids = []
        for j, age in enumerate(ages):
            kid = f"kid{i}_{j}"
            people[kid] = {"age": {year: age}}
            units["marital_units"][f"k{i}_{j}"] = {"members": [kid]}
            kids.append(kid)
        members = [*adults, *kids]
        for unit in ("tax_units", "families", "spm_units"):
            units[unit][f"u{i}"] = {"members": members}
        units["marital_units"][f"u{i}"] = {"members": adults}
        hh = {"members": members, "state_name": {year: state}}
        for var, value in (inputs.get("household") or {}).items():
            hh[var] = {year: value}
        units["households"][f"u{i}"] = hh
    return {"people": people, **units}


def run(state: str, household: dict, inputs: dict, year: int) -> dict[str, list[int]]:
    """Taxes per grid salary. PolicyEngine's employee_payroll_tax includes the state payroll taxes. Its
    local_income_tax leaves out some local taxes (Maryland counties, Multnomah's preschool tax), so local
    tax is built from local_income_tax_before_refundable_credits (which has them) plus occupational taxes,
    less refundable local credits. Not from household_tax: that also nets out benefits such as the ACA
    premium tax credit (assumed when the household has no employer coverage) and adds a sales/use tax."""
    from policyengine_us import Simulation
    sim = Simulation(situation=situation(state, household, inputs, year))
    calc = {n: [float(v) for v in sim.calculate(n, year, map_to="household")]
            for n in ("income_tax", "employee_payroll_tax", "employee_state_payroll_tax", "state_income_tax",
                      "local_income_tax_before_refundable_credits", "local_occupational_tax",
                      "household_refundable_local_tax_credits")}
    out = {p: [] for p in PARTS}
    for i, wage in enumerate(WAGES):
        federal, payroll = calc["income_tax"][i], calc["employee_payroll_tax"][i]
        state_payroll, state_tax = calc["employee_state_payroll_tax"][i], calc["state_income_tax"][i]
        local = (calc["local_income_tax_before_refundable_credits"][i] + calc["local_occupational_tax"][i]
                 - calc["household_refundable_local_tax_credits"][i])
        if local < -1000:
            print(f"  warning: {state} {inputs or ''} ${wage:,}: local tax looks wrong ({local:,.0f})")
        for part, value in zip(PARTS, (federal, payroll - state_payroll, state_tax, state_payroll, local)):
            out[part].append(round(value))
    return {p: v for p, v in out.items() if p in ("federal", "fica") or any(v)}


def build(household: dict, rules: dict, year: int) -> dict:
    localities = rules.get("localities") or {}
    defaults = {loc["state"]: loc for loc in localities.values() if loc.get("default")}
    places = {}
    for st in STATES:
        places[st] = run(st, household, (defaults.get(st) or {}).get("policyengine") or {}, year)
    for key, loc in localities.items():
        places[key] = run(loc["state"], household, loc.get("policyengine") or {}, year)
    # "Somewhere in the US": the plain average of the states.
    places["US"] = {p: [round(sum(places[s].get(p, [0] * len(WAGES))[i] for s in STATES) / len(STATES))
                        for i in range(len(WAGES))] for p in PARTS}
    return {"year": year, "built": date.today().isoformat(), "source": f"PolicyEngine-US {version('policyengine-us')}",
            "household": household, "k401": 0, "wages": WAGES, "places": places}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default="config", help="directory holding taxes.yaml; tables go in its tax_data/")
    ap.add_argument("--year", type=int, help="tax year (default: taxes.yaml's year)")
    ap.add_argument("--only", nargs="*", help="build only these households, e.g. joint-2-1")
    args = ap.parse_args()
    cfg_dir = Path(args.config)
    rules = yaml.safe_load((cfg_dir / "taxes.yaml").read_text(encoding="utf-8"))
    year = args.year or rules["year"]
    out_dir = cfg_dir / "tax_data"
    out_dir.mkdir(exist_ok=True)
    for household in households():
        name = variant_name(household)
        if args.only and name not in args.only:
            continue
        print(f"{name}...", flush=True)
        data = build(household, rules, year)
        path = out_dir / f"{name}.json.gz"
        # mtime=0: the same tables give the same bytes, so git only sees real changes
        path.write_bytes(gzip.compress(json.dumps(data, separators=(",", ":")).encode(), mtime=0))
        print(f"wrote {path}: {len(data['places'])} places x {len(WAGES)} salaries, tax year {year}, {data['source']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
