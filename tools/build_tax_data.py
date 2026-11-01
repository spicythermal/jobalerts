"""Build config/tax_data.json: US taxes at each salary, per state and locality, from PolicyEngine-US.

    pip install policyengine-us        # not a jobalerts dependency; only needed to rebuild the table
    python tools/build_tax_data.py [--config config] [--year 2026]

The household (filing status, 401(k) share) and the localities come from config/taxes.yaml. For each
place this runs one PolicyEngine simulation holding one household per salary on the grid, and stores
federal income tax, Social Security + Medicare, state income tax, state payroll taxes and local
income taxes, in whole dollars. jobalerts interpolates between grid points. Rebuild each January,
once PolicyEngine has the new year's brackets.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from importlib.metadata import version
from pathlib import Path

import yaml

STATES = ["AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS",
          "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC",
          "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY"]
WAGES = [0] + list(range(20_000, 400_001, 5_000)) + list(range(425_000, 1_000_001, 25_000))
PARTS = ["federal", "fica", "state", "state_payroll", "local"]


def situation(state: str, household: dict, inputs: dict, year: int) -> dict:
    """One household per grid salary: an earner and a spouse with no income, filing jointly."""
    people, units = {}, {"tax_units": {}, "families": {}, "spm_units": {}, "marital_units": {}, "households": {}}
    for i, wage in enumerate(WAGES):
        you, spouse = f"you{i}", f"spouse{i}"
        k401 = min(wage * household["k401_rate"], household["k401_limit"])
        person = {"age": {year: 40}, "employment_income": {year: wage}, "traditional_401k_contributions": {year: k401}}
        for var, value in (inputs.get("person") or {}).items():
            person[var] = {year: wage if value == "$wage" else value}
        people[you], people[spouse] = person, {"age": {year: 40}}
        members = [you, spouse]
        for unit in ("tax_units", "families", "spm_units", "marital_units"):
            units[unit][f"u{i}"] = {"members": members}
        hh = {"members": members, "state_name": {year: state}}
        for var, value in (inputs.get("household") or {}).items():
            hh[var] = {year: value}
        units["households"][f"u{i}"] = hh
    return {"people": people, **units}


def run(state: str, household: dict, inputs: dict, year: int) -> dict[str, list[int]]:
    """Taxes per grid salary. PolicyEngine's employee_payroll_tax includes the state payroll taxes, and
    its local_income_tax leaves out some local taxes that household_tax counts (Maryland counties,
    Multnomah's preschool tax), so local tax is taken as what's left of household_tax. household_tax
    also holds an estimated sales/use tax on spending; that's left out (local prices cover it)."""
    from policyengine_us import Simulation
    sim = Simulation(situation=situation(state, household, inputs, year))
    calc = {n: [float(v) for v in sim.calculate(n, year, map_to="household")]
            for n in ("income_tax", "employee_payroll_tax", "employee_state_payroll_tax", "state_income_tax",
                      "household_tax", "state_use_tax", "traditional_401k_contributions")}
    # Pennsylvania taxes 401(k) deferrals (PA-40 instructions); PolicyEngine 2.88 subtracts them.
    pa_rate = float(sim.tax_benefit_system.parameters(f"{year}-01-01").gov.states.pa.tax.income.rate) if state == "PA" else 0
    out = {p: [] for p in PARTS}
    for i, wage in enumerate(WAGES):
        federal, payroll = calc["income_tax"][i], calc["employee_payroll_tax"][i]
        state_payroll, state_tax = calc["employee_state_payroll_tax"][i], calc["state_income_tax"][i]
        local = calc["household_tax"][i] - federal - payroll - state_tax - calc["state_use_tax"][i]
        state_tax += pa_rate * calc["traditional_401k_contributions"][i]
        if local < -1000:
            print(f"  warning: {state} {inputs or ''} ${wage:,}: local tax looks wrong ({local:,.0f})")
        for part, value in zip(PARTS, (federal, payroll - state_payroll, state_tax, state_payroll, local)):
            out[part].append(round(value))
    return {p: v for p, v in out.items() if p in ("federal", "fica") or any(v)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default="config", help="directory holding taxes.yaml; tax_data.json is written there")
    ap.add_argument("--year", type=int, help="tax year (default: taxes.yaml's year)")
    args = ap.parse_args()
    cfg_dir = Path(args.config)
    rules = yaml.safe_load((cfg_dir / "taxes.yaml").read_text(encoding="utf-8"))
    year, household = args.year or rules["year"], rules["household"]
    if household.get("filing") != "joint":
        sys.exit("only filing: joint is set up (married filing jointly, one earner)")
    localities = rules.get("localities") or {}
    defaults = {loc["state"]: loc for loc in localities.values() if loc.get("default")}

    places = {}
    for st in STATES:
        print(f"{st}...", flush=True)
        places[st] = run(st, household, (defaults.get(st) or {}).get("policyengine") or {}, year)
    for key, loc in localities.items():
        print(f"{key}...", flush=True)
        places[key] = run(loc["state"], household, loc.get("policyengine") or {}, year)
    # "Somewhere in the US": the plain average of the states.
    places["US"] = {p: [round(sum(places[s].get(p, [0] * len(WAGES))[i] for s in STATES) / len(STATES))
                        for i in range(len(WAGES))] for p in PARTS}

    data = {
        "year": year,
        "built": date.today().isoformat(),
        "source": f"PolicyEngine-US {version('policyengine-us')}",
        "household": household,
        "wages": WAGES,
        "places": places,
    }
    path = cfg_dir / "tax_data.json"
    # One line per array keeps the file reviewable in a diff.
    lines = ["{"]
    for k in ("year", "built", "source", "household", "wages"):
        lines.append(f'  "{k}": {json.dumps(data[k])},')
    lines.append('  "places": {')
    for i, (name, parts) in enumerate(places.items()):
        body = ",\n".join(f'    "{p}": {json.dumps(v)}' for p, v in parts.items())
        lines.append(f'   "{name}": {{\n{body}\n   }}' + ("," if i < len(places) - 1 else ""))
    lines += ["  }", "}"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    json.loads(path.read_text(encoding="utf-8"))
    print(f"wrote {path}: {len(places)} places x {len(WAGES)} salaries, tax year {year}, {data['source']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
