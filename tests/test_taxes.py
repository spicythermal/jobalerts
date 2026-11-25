"""Take-home pay: the committed tax tables, place matching, local taxes, abroad, and the index on take-home."""
import json
import shutil
from datetime import date

import pytest

from jobalerts.col import Pay, PayIndex
from jobalerts.config import Config
from jobalerts.http import HttpClient
from jobalerts.notify import Notifier
from jobalerts.scheduler import Runner
from jobalerts.store import Store
from jobalerts.taxes import Taxes

from conftest import REPO, run
from test_col import DATA


# A household the hand checks below are worked out for: married, one earner, two children under 17,
# one older dependent, 6% into a pre-tax 401(k).
HOUSEHOLD = {"filing": "joint", "children": 2, "other_dependents": 1, "k401_rate": 0.06}
RULES, TABLES = REPO / "config" / "taxes.yaml", REPO / "config" / "tax_data"


@pytest.fixture(scope="module")
def taxes():
    return Taxes.load(RULES, TABLES, HOUSEHOLD)


def part(th, label):
    return th.parts.get(label, 0.0)


def test_federal_and_fica_by_hand(taxes):
    # $155K, married filing jointly, 6% 401(k): taxable 155,000 - 9,300 - 32,200 standard deduction = 113,500
    th = taxes.take_home(155_000, "USA", "TX", "austin")
    # less the credits for the dependents: two $2,200 child tax credits + the $500 other-dependent credit
    assert part(th, "federal income tax") == pytest.approx(0.10 * 24_800 + 0.12 * 76_000 + 0.22 * 12_700 - 4_900, abs=1)
    # 401(k) money still pays Social Security and Medicare
    assert part(th, "Social Security + Medicare") == pytest.approx(0.0765 * 155_000, abs=1)
    assert th.net == pytest.approx(155_000 - 9_494 - 11_858, abs=2)


def test_social_security_stops_at_the_wage_cap(taxes):
    th = taxes.take_home(300_000, "USA", "TX")
    # 6.2% up to $184,500, 1.45% Medicare on all, 0.9% more above $250K (joint)
    assert part(th, "Social Security + Medicare") == pytest.approx(0.062 * 184_500 + 0.0145 * 300_000 + 0.009 * 50_000, abs=1)


def test_flat_tax_states_by_hand(taxes):
    assert part(taxes.take_home(155_000, "USA", "CO"), "state income tax") == pytest.approx(0.044 * 113_500, abs=1)
    # Pennsylvania taxes 401(k) deferrals: 3.07% of the full salary
    assert part(taxes.take_home(155_000, "USA", "PA", "malvern"), "state income tax") == pytest.approx(0.0307 * 155_000, abs=1)


def test_portland_reference(taxes):
    th = taxes.take_home(155_000, "USA", "OR", "portland")
    assert th.where.startswith("Multnomah County")
    assert 9_500 < part(th, "state income tax") < 11_000          # 8.75% bracket, federal tax subtraction
    assert part(th, "state payroll (SDI, paid leave)") == pytest.approx(0.007 * 155_000, abs=20)  # paid leave + transit
    assert "Metro supportive housing services tax" not in th.parts  # only above $200K joint
    high = taxes.take_home(300_000, "USA", "OR", "portland")
    assert part(high, "Metro supportive housing services tax") == pytest.approx(0.01 * (300_000 - 18_000 - 200_000))
    assert part(high, "local income tax") > 0                       # Multnomah preschool tax above $200K


def test_washington_keeps_more_than_oregon(taxes):
    wa, oregon = taxes.take_home(155_000, "USA", "WA", "seattle"), taxes.take_home(155_000, "USA", "OR", "portland")
    assert part(wa, "state income tax") == 0                         # (its column holds a low-income credit)
    assert part(wa, "state payroll (SDI, paid leave)") > 1_500       # WA Cares + paid leave
    assert wa.net - oregon.net == pytest.approx(8_455, abs=300)  # Oregon's credit per dependent narrows the gap


def test_localities_by_city_and_state_default(taxes):
    assert taxes.place_key("NY", "brooklyn") == "nyc"
    assert taxes.place_key("NY", "rochester") == "NY"
    assert taxes.place_key("MD", "baltimore") == "baltimore_city"
    assert taxes.place_key("MD", "") == "montgomery_md"              # every Maryland county taxes income
    assert taxes.place_key("", "") == "US"
    nyc, upstate = taxes.take_home(155_000, "USA", "NY", "new york"), taxes.take_home(155_000, "USA", "NY", "albany")
    assert 4_000 < part(nyc, "local income tax") < 5_000 and "local income tax" not in upstate.parts


def test_city_taxes_policyengine_lacks(taxes):
    assert part(taxes.take_home(100_000, "USA", "OH", "columbus"), "Ohio municipal income tax") == pytest.approx(2_500)
    assert part(taxes.take_home(100_000, "USA", "OH", "athens"), "Ohio municipal income tax") == pytest.approx(2_000)
    assert "Ohio municipal income tax" not in taxes.take_home(100_000, "USA", "OH", "beavercreek").parts
    assert part(taxes.take_home(100_000, "USA", "PA", "pittsburgh"), "Pennsylvania local earned income tax") == pytest.approx(3_000)
    # Philadelphia's wage tax comes from PolicyEngine instead of the 1% default
    philly = taxes.take_home(155_000, "USA", "PA", "philadelphia")
    assert part(philly, "local income tax") == pytest.approx(0.0374 * 155_000, abs=1)
    assert "Pennsylvania local earned income tax" not in philly.parts


def test_interpolates_between_grid_points(taxes):
    a, b, mid = (taxes.take_home(w, "USA", "TX").taxes for w in (150_000, 155_000, 152_500))
    assert min(a, b) < mid < max(a, b)
    assert taxes.take_home(1_500_000, "USA", "TX").taxes > taxes.take_home(1_000_000, "USA", "TX").taxes


def test_abroad_rates(taxes):
    de = taxes.take_home(100_000, "DEU")
    assert de.taxes == pytest.approx(34_000) and "approximate" in de.where
    assert taxes.take_home(175_000, "DEU").taxes == pytest.approx(175_000 * 0.365)  # halfway between the points
    assert taxes.take_home(400_000, "DEU").taxes == pytest.approx(400_000 * 0.39)   # flat past the last point
    assert "not listed" in taxes.take_home(100_000, "XYZ").where


def test_problem_when_stale(taxes):
    assert taxes.problem(date(taxes.year, 6, 1)) == ""
    assert "rebuild" in taxes.problem(date(taxes.year + 1, 1, 2))


def test_every_household_has_a_table_for_every_state_and_locality():
    from jobalerts.taxes import variant_name
    for filing in ("single", "joint"):
        for children in range(5):
            for other in range(2):
                h = {"filing": filing, "children": children, "other_dependents": other}
                t = Taxes.load(RULES, TABLES, h)
                assert t.data["household"] == h and t.data["k401"] == 0 and t.data["source"] == "PolicyEngine-US 2.88.9"
                assert len([k for k in t.places if len(k) == 2 and k != "US"]) == 51 and "US" in t.places
    assert variant_name({"filing": "joint", "children": 6, "other_dependents": 3}) == "joint-4-1"   # most there is
    assert variant_name({}) == "single-0-0"


def test_dependents_and_filing_change_the_taxes():
    single = Taxes.load(RULES, TABLES, {"filing": "single", "k401_rate": 0}).take_home(155_000, "USA", "TX")
    joint = Taxes.load(RULES, TABLES, {"filing": "joint", "k401_rate": 0}).take_home(155_000, "USA", "TX")
    kids = Taxes.load(RULES, TABLES, {"filing": "joint", "children": 2, "k401_rate": 0}).take_home(155_000, "USA", "TX")
    fed = "federal income tax"
    assert part(single, fed) > part(joint, fed) > part(kids, fed)
    assert part(joint, fed) - part(kids, fed) == pytest.approx(2 * 2_200, abs=1)


def test_401k_lowers_income_taxes_but_not_payroll_or_wage_taxes():
    none = Taxes.load(RULES, TABLES, dict(HOUSEHOLD, k401_rate=0))
    six = Taxes.load(RULES, TABLES, HOUSEHOLD)
    for state, city in (("TX", ""), ("NY", "brooklyn"), ("PA", "philadelphia"), ("OR", "portland")):
        a, b = none.take_home(155_000, "USA", state, city), six.take_home(155_000, "USA", state, city)
        assert part(b, "federal income tax") == pytest.approx(part(a, "federal income tax") - 0.22 * 9_300, abs=2)
        assert part(b, "Social Security + Medicare") == part(a, "Social Security + Medicare")
        assert part(b, "state payroll (SDI, paid leave)") == part(a, "state payroll (SDI, paid leave)")
    # Pennsylvania and Philadelphia's wage tax count 401(k) money; New York City's tax doesn't
    pa0, pa6 = none.take_home(155_000, "USA", "PA", "philadelphia"), six.take_home(155_000, "USA", "PA", "philadelphia")
    assert part(pa6, "state income tax") == part(pa0, "state income tax")
    assert part(pa6, "local income tax") == part(pa0, "local income tax")
    ny0, ny6 = none.take_home(155_000, "USA", "NY", "brooklyn"), six.take_home(155_000, "USA", "NY", "brooklyn")
    assert part(ny6, "local income tax") < part(ny0, "local income tax")
    # the deferral stops at the IRS limit
    rich = six.take_home(600_000, "USA", "TX")
    assert part(rich, fed := "federal income tax") == pytest.approx(
        part(none.take_home(600_000 - 24_500, "USA", "TX"), fed), abs=1)


def test_old_single_household_tables_are_refused():
    rules = {"localities": {}}
    with pytest.raises(ValueError, match="rebuild"):
        Taxes(rules, {"year": 2026, "wages": [0, 1], "places": {"US": {}}})
    assert Taxes(rules, {"year": 2026, "k401": 0, "wages": [0, 1], "places": {"US": {}}}).describe().startswith("single")


# ---------------------------------------------------------------------------- the index on take-home
@pytest.fixture
def pi(taxes):
    return PayIndex(DATA, taxes=taxes)


def test_reference_is_still_one_and_places_carry_state_and_city(pi):
    assert pi.home.state == "OR" and pi.home.city == "portland"
    assert pi.index(Pay(155_000, 155_000, "USD"), "Portland, OR").index == pytest.approx(1.0)
    place = pi.resolve("Santa Fe, NM")  # no Santa Fe metro in the test data: the state, with the city kept
    assert (place.state, place.city) == ("NM", "santa fe")
    place = pi.resolve("Beaverton, OR")  # a suburb: the metro's prices, the suburb's city taxes
    assert place.label.startswith("Portland") and place.city == "beaverton"


def test_index_uses_take_home(pi, taxes):
    gross = PayIndex(DATA)
    # San Jose: CA taxes less than Oregon at $155K, so the index rises against the gross one
    with_tax = pi.index(Pay(155_000, 155_000, "USD"), "San Jose, CA").index
    assert with_tax > gross.index(Pay(155_000, 155_000, "USD"), "San Jose, CA").index
    ca, oregon = taxes.take_home(155_000, "USA", "CA", "san jose"), taxes.take_home(155_000, "USA", "OR", "portland")
    assert with_tax == pytest.approx((ca.net / 1.26) / (oregon.net / 1.05))
    # progressive taxes: double the pay is less than double the index
    assert 1.8 < pi.index(Pay(310_000, 310_000, "USD"), "Portland, OR").index < 1.9
    # remote jobs pay the reference location's taxes
    assert pi.index(Pay(155_000, 155_000, "USD"), "Remote - US", remote=True).index == pytest.approx(1.0)


def test_runner_loads_the_tax_tables_and_flags_a_stale_year(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "col_data.json").write_text(json.dumps(DATA))
    shutil.copy(RULES, cfg_dir / "taxes.yaml")
    shutil.copytree(TABLES, cfg_dir / "tax_data")
    cfg = Config(database=str(tmp_path / "data" / "jobs.db"), config_dir=cfg_dir)
    runner = Runner(cfg, Store(":memory:"), HttpClient(), Notifier(cfg.notify, dry_run=True))
    assert runner.pay.taxes is not None and runner.pay_problem == ""
    year = runner.pay.taxes.year
    monkeypatch.setattr("jobalerts.taxes.date", type("D", (), {"today": staticmethod(lambda: date(year + 1, 1, 5))}))
    assert "rebuild config/tax_data/" in run(runner.send_digest(mark=False))


def test_missing_tax_tables_fall_back_to_gross_with_a_note(tmp_path):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "col_data.json").write_text(json.dumps(DATA))
    cfg = Config(database=str(tmp_path / "data" / "jobs.db"), config_dir=cfg_dir)
    runner = Runner(cfg, Store(":memory:"), HttpClient(), Notifier(cfg.notify, dry_run=True))
    assert runner.pay is not None and runner.pay.taxes is None
    assert "tax tables unusable" in runner.pay_problem
