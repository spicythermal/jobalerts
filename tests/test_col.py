"""Cost-of-living pay index: salary parsing, place resolution, the index math, and the data refresh."""
import io
import json
import zipfile
from datetime import date, timedelta

import httpx
import pytest

from jobalerts import col
from jobalerts.col import Pay, PayIndex, parse_salary
from jobalerts.config import Board, Config
from jobalerts.http import HttpClient
from jobalerts.notify import Notifier
from jobalerts.scheduler import Runner
from jobalerts.scorer import FitScore
from jobalerts.store import Store, stories_from_row

from conftest import REPO, run
from test_pipeline import FakeSource, make_job

# Round numbers so the expected index is easy to check by hand.
DATA = {
    "built": date.today().isoformat(),
    "sources": {"us": "test RPP", "countries": "test PPP"},
    "us_metros": {"Portland-Vancouver-Hillsboro, OR-WA": 105.0, "San Jose-Sunnyvale-Santa Clara, CA": 126.0,
                  "Albuquerque, NM": 84.0, "Portland-South Portland, ME": 100.0},
    "us_nonmetro": 88.0,
    "us_states": {"OR": 100.0, "CA": 110.0, "NM": 90.0},
    "countries": {"DEU": {"ppp": 0.7, "fx": 0.875, "year": 2025}, "JPN": {"ppp": 100, "fx": 150, "year": 2025}, "CHE": {"ppp": 1.12, "fx": 0.8, "year": 2025}},
}


@pytest.fixture
def pi():
    return PayIndex(DATA)


@pytest.mark.parametrize("text, expected", [
    ("$155K-200K/yr", Pay(155_000, 200_000, "$", "year")),
    ("$175,000 – $225,000 / year", Pay(175_000, 225_000, "$", "year")),
    ("$140K – $170K • 0.05% – 0.1%", Pay(140_000, 170_000, "$", "year")),
    ("EUR 90K-110K/yr", Pay(90_000, 110_000, "EUR", "year")),
    ("€80.000 – €95.000", Pay(80_000, 95_000, "EUR", "year")),
    ("CHF 140,000 per year", Pay(140_000, 140_000, "CHF", "year")),
    ("$45.50 - $60/hour", Pay(45.5, 60, "$", "hour")),
    ("$62", Pay(62, 62, "$", "hour")),
    ("R$ 25.000/mês", Pay(25_000, 25_000, "BRL", "month")),
    ("S$150K", Pay(150_000, 150_000, "SGD", "year")),
    ("Competitive", None),
    ("", None),
])
def test_parse_salary(text, expected):
    assert parse_salary(text) == expected


def test_reference_scores_one_and_double_pay_scores_two(pi):
    assert pi.index(Pay(155_000, 155_000), "Portland, OR").index == pytest.approx(1.0)
    assert pi.index(Pay(310_000, 310_000), "Hillsboro, OR").index == pytest.approx(2.0)


def test_cheaper_and_dearer_places(pi):
    # Albuquerque prices are 0.8x Portland's, so the same pay goes 1.25x as far; San Jose's are 1.2x.
    assert pi.index(Pay(155_000, 155_000), "Albuquerque, NM").index == pytest.approx(1.25)
    r = pi.index(Pay(155_000, 186_000), "San Jose, CA")
    assert (r.low, r.high) == (pytest.approx(155 / 186), pytest.approx(1.0))
    assert r.index == pytest.approx((155 / 186 + 1) / 2)


def test_abroad_uses_ppp_in_local_currency(pi):
    # EUR 70K / PPP 0.7 = $100K at US-average prices; reference is $155K / 1.05.
    r = pi.index(Pay(70_000, 70_000, "EUR"), "Munich, Germany")
    assert r.index == pytest.approx(100_000 / (155_000 / 1.05))
    assert r.place.country == "DEU"
    # No currency given -> the country's own; "$" in Germany means US dollars.
    assert pi.index(Pay(70_000, 70_000, ""), "Munich, DE").index == pytest.approx(r.index)
    usd = pi.index(Pay(87_500, 87_500, "$"), "Munich, Germany")
    assert usd.index == pytest.approx(87_500 / 0.8 / (155_000 / 1.05))  # price level 0.7/0.875 = 0.8


@pytest.mark.parametrize("location, remote, label", [
    ("Hillsboro, OR", False, "Portland-Vancouver-Hillsboro"),
    ("Beaverton, Oregon", False, "Portland-Vancouver-Hillsboro"),
    ("US-OR-Hillsboro", False, "Portland-Vancouver-Hillsboro"),
    ("Portland, ME", False, "Portland-South Portland"),
    ("Hybrid - San Jose, CA", False, "San Jose"),
    ("Remote - US", True, "Portland-Vancouver-Hillsboro"),
    ("Remote; San Jose, CA", False, "Portland-Vancouver-Hillsboro"),
    ("Livermore, CA", False, "California"),        # its metro isn't in the test data -> state
    ("Remote, Germany", True, "DEU"),               # remote abroad means living abroad
    ("Villigen", False, "CHE"),
    ("Santa Fe, NM", False, "New Mexico"),
    ("US > New Mexico > Albuquerque", False, "Albuquerque"),
    ("Japan > Tokyo (Tama-shi)", False, "JPN"),
    ("Albuquerque-NM (Main)", False, "Albuquerque"),
    ("Greater Albuquerque Area", False, "Albuquerque"),
])
def test_resolve(pi, location, remote, label):
    assert label in pi.resolve(location, remote).label


def test_unknown_places_and_currencies_give_no_index(pi):
    assert pi.resolve("Atlantis") is None
    assert pi.index(Pay(1, 1), "Atlantis") is None
    assert pi.index(Pay(5_000_000, 5_000_000, "KRW"), "Remote") is None  # no KRW rate in the test data
    assert pi.index(None, "Portland, OR") is None


def test_location_overrides():
    pi = PayIndex(DATA, overrides={"Los Alamos": "NM", "Garching": "DEU", "Anywhere": "home"})
    assert "New Mexico" in pi.resolve("Los Alamos").label
    assert pi.resolve("Garching bei München").country == "DEU"
    assert pi.resolve("Anywhere") is pi.home


def test_committed_data_resolves_the_reference():
    data = json.loads((REPO / "config" / "col_data.json").read_text(encoding="utf-8"))
    col.check_data(data, "Portland, OR")  # with family housing (HUD rents) at the default 40%
    col.check_data(data, "Portland, OR", buy=True)
    for share in (0.0, 0.4):
        pi = PayIndex(data, housing_share=share)
        assert "Portland-Vancouver-Hillsboro" in pi.home.label
        assert pi.index(Pay(310_000, 310_000, "USD"), "Hillsboro, OR").index == pytest.approx(2.0)
    # family housing makes San Jose dearer against Portland than the all-items RPP does
    plain, family = (PayIndex(data, housing_share=s).index(Pay(155_000, 155_000, "USD"), "San Jose, CA").index
                     for s in (0.0, 0.4))
    assert family < plain - 0.1
    # buying a 4-bedroom home in San Jose costs far more against Portland than renting one does
    result = PayIndex(data, housing_share=0.4, ownership=col.Ownership()).index(Pay(250_000, 250_000, "USD"),
                                                                                   "Palo Alto, CA")
    assert result.buy_index < result.index - 0.2


# ---------------------------------------------------------------------------- family housing
# RPP parts, weights and HUD rents chosen so the levels work out by hand (see test comments).
FAMILY = dict(
    DATA,
    us_metro_parts={
        "Portland-Vancouver-Hillsboro, OR-WA": {"goods": 100, "housing": 120, "utilities": 100, "other": 100, "fmr4": 3000},
        "San Jose-Sunnyvale-Santa Clara, CA": {"goods": 100, "housing": 200, "utilities": 150, "other": 100, "fmr4": 5000},
    },
    us_state_parts={"NM": {"goods": 90, "housing": 80, "utilities": 100, "other": 95}},
    rpp_weights={"goods": 0.3, "housing": 0.2, "utilities": 0.1, "other": 0.4},
    fmr_scale=0.04,
)


def test_family_housing_levels_by_hand():
    pi = PayIndex(FAMILY, housing_share=0.4)
    # Portland: 60% x goods/other 100 + 40% x FMR $3,000 x 0.04 = 120 -> 108
    assert pi.home.level == pytest.approx(1.08)
    assert "4-bedroom FMR $3,000" in pi.home.label
    # San Jose: 60% x 100 + 40% x $5,000 x 0.04 = 200 -> 140; the same pay buys 108/140 as much
    assert pi.resolve("San Jose, CA").level == pytest.approx(1.40)
    assert pi.index(Pay(155_000, 155_000), "San Jose, CA").index == pytest.approx(1.08 / 1.40)
    # New Mexico has no FMR: RPP rents (0.2 x 80 + 0.1 x 100) / 0.3 = 86.67, goods/other (0.3 x 90 + 0.4 x 95) / 0.7
    nm = pi.resolve("Santa Fe, NM")
    assert nm.level == pytest.approx((0.6 * (27 + 38) / 0.7 + 0.4 * 26 / 0.3) / 100)
    assert "RPP rents" in nm.label
    # places without parts and countries keep their plain price level
    assert pi.resolve("Albuquerque, NM").level == pytest.approx(0.84)
    assert pi.resolve("Munich, Germany").level == pytest.approx(0.8)


OWN = col.Ownership(down_payment=0.2, property_tax=0.01, property_tax_by_state={"CA": 0.012}, upkeep=0.005,
                    utilities=300)
BUY = dict(FAMILY, mortgage_rate=6.0, us_metro_parts={
    "Portland-Vancouver-Hillsboro, OR-WA": dict(FAMILY["us_metro_parts"]["Portland-Vancouver-Hillsboro, OR-WA"],
                                                home4=600_000),
    "San Jose-Sunnyvale-Santa Clara, CA": dict(FAMILY["us_metro_parts"]["San Jose-Sunnyvale-Santa Clara, CA"],
                                               home4=1_800_000),
})


def test_buy_index_by_hand():
    pi = PayIndex(BUY, housing_share=0.4, ownership=OWN)
    # Portland: (480K x 6% + 600K x 1.5%) / 12 = 3,150 + utilities 300 x 100/100 = $3,450/mo
    assert pi.own_monthly(BUY["us_metro_parts"]["Portland-Vancouver-Hillsboro, OR-WA"], "OR") == pytest.approx(3450)
    # level: 60% x 100 + 40% x 3,450 x 0.04 = 138 -> 1.152
    assert pi.home.buy_level == pytest.approx(1.152)
    assert "own $600K 4-bed at 6.00%: $3.5K/mo, $120K down" == pi.home.buy_note
    # San Jose: (1.44M x 6% + 1.8M x (1.2% + 0.5%)) / 12 = 9,750 + 300 x 150/100 = $10,200 -> 60 + 40 x 408 / 100
    sj = pi.resolve("San Jose, CA")
    assert sj.buy_level == pytest.approx((60 + 0.4 * 10_200 * 0.04) / 100)
    result = pi.index(Pay(155_000, 155_000), "San Jose, CA")
    assert result.index == pytest.approx(1.08 / 1.40)                        # renting: unchanged
    assert result.buy_index == pytest.approx(1.152 / sj.buy_level)           # buying against buying in Portland
    assert result.text().startswith(f"pay index 0.77 rent · {result.buy_index:.2f} buy: $155K/yr in San Jose")
    assert result.best_high == result.high                                   # renting is the better of the two here
    # the reference buys too: same pay in Portland is 1.0 either way
    home = pi.index(Pay(155_000, 155_000), "Portland, OR")
    assert home.index == pytest.approx(1.0) and home.buy_index == pytest.approx(1.0)


def test_no_buy_index_without_home_prices_or_abroad():
    pi = PayIndex(BUY, housing_share=0.4, ownership=OWN)
    assert pi.index(Pay(80_000, 80_000, "EUR"), "Munich, Germany").buy_low is None
    nm = pi.index(Pay(155_000, 155_000), "Santa Fe, NM")                     # state parts without home4
    assert nm.buy_low is None and "buy" not in nm.text()
    assert PayIndex(FAMILY, housing_share=0.4, ownership=OWN).ownership is None   # table without home prices
    assert PayIndex(BUY, housing_share=0, ownership=OWN).ownership is None        # buying needs family housing


def test_family_housing_off_or_missing_falls_back_to_rpp():
    assert PayIndex(FAMILY, housing_share=0).home.level == pytest.approx(1.05)
    old_table = PayIndex(DATA, housing_share=0.4)  # built before the RPP parts were kept
    assert old_table.housing_share == 0 and old_table.home.level == pytest.approx(1.05)


def test_metro_fmr_picks_the_whole_metro_area():
    areas = [{"FMR_CODE": "METRO14460MM4760", "FMR_AREANAME": "Brockton, MA HUD Metro FMR Area", "FMR_4BDR": 3000},
             {"FMR_CODE": "METRO14460MM1120", "FMR_AREANAME": "Boston-Cambridge-Quincy, MA-NH HUD Metro FMR Area", "FMR_4BDR": 3894}]
    assert col._metro_fmr("14460", "Boston-Cambridge-Newton, MA-NH", areas) == 3894        # by first city
    areas.append({"FMR_CODE": "METRO14460M14460", "FMR_AREANAME": "Whole metro", "FMR_4BDR": 3500})
    assert col._metro_fmr("14460", "Boston-Cambridge-Newton, MA-NH", areas) == 3500        # coded for the metro
    assert col._metro_fmr("99999", "Nowhere, OR", []) is None


def test_check_data_rejects_broken_tables():
    with pytest.raises(ValueError):
        col.check_data(DATA, "Portland, OR")  # far too few metros to be a real download


# ---------------------------------------------------------------------------- refresh
def _zip(name: str, rows: list[str]) -> bytes:
    header = "GeoFIPS,GeoName,Region,TableName,LineCode,IndustryClassification,Description,Unit,2023,2024"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, "\n".join([header, *rows]))
    return buf.getvalue()


PDX = "Portland-Vancouver-Hillsboro, OR-WA"
WEIGHTS = {"goods": 0.3, "housing": 0.17, "utilities": 0.01, "other": 0.52}
LINES = {"goods": 2, "housing": 3, "utilities": 4, "other": 5}


def _metro_rows(fips: str, name: str, parts: dict[str, float], total: float | None = None) -> list[str]:
    """All-items line (by default the WEIGHTS mix of the parts) and the four part lines."""
    total = total if total is not None else sum(WEIGHTS[k] * v for k, v in parts.items())
    rows = [f' "{fips}","{name} (Metropolitan Statistical Area)", ,MARPP,1,"...","RPPs: All items ","Index",99,{total}']
    rows += [f' "{fips}","{name} (Metropolitan Statistical Area)", ,MARPP,{LINES[k]},"...","{k}","Index",99,{v}'
             for k, v in parts.items()]
    return rows


def _zillow_csv(rows: list[tuple[str, str, float | str]]) -> str:
    header = "RegionID,SizeRank,RegionName,RegionType,StateName,2026-08-31,2026-09-30"
    return "\n".join([header, *(f'1,1,"{name}",msa,{st},1,{v}' for name, st, v in rows)])


def _zillow_city_csv(rows: list[tuple[str, str, str, float]]) -> str:
    header = "RegionID,SizeRank,RegionName,RegionType,StateName,State,Metro,CountyName,2026-08-31,2026-09-30"
    return "\n".join([header, *(f'1,1,{city},city,{st},{st},"{metro}",X County,1,{v}' for city, st, metro, v in rows)])


def fake_sources(metros: int = 320, fmr: bool = True, homes: bool = True) -> httpx.MockTransport:
    msa = []
    fmr_features = []
    for i in range(metros):
        parts = {"goods": 95 + i % 7, "housing": 80 + i % 40, "utilities": 90 + i % 11, "other": 98 + i % 5}
        msa += _metro_rows(str(10000 + i), f"Town{i}, OR", parts)
        fmr_features.append({"FMR_CODE": f"METRO{10000 + i}M{10000 + i}", "FMR_AREANAME": f"Town{i}, OR MSA",
                             "FMR_4BDR": 2000 + 25 * (i % 40)})
    msa += _metro_rows("38900", "Portland-Vancouver-Hillsboro, OR-WA",
                       {"goods": 105, "housing": 125, "utilities": 107, "other": 100}, total=105.4)
    fmr_features.append({"FMR_CODE": "METRO38900M38900", "FMR_AREANAME": "Portland-Vancouver-Hillsboro, OR-WA MSA",
                         "FMR_4BDR": 3109})
    states = [f' "{i:02d}000","{name}",8,SARPP,1,"...","RPPs: All items ","Index",100,101'
              for i, name in enumerate(col.STATES.values())]
    oregon = list(col.STATES.values()).index("Oregon")
    states += [f' "{oregon:02d}000","Oregon",8,SARPP,{n},"...","{k}","Index",100,{100 + n}' for k, n in LINES.items()]
    zillow_metros = [(f"Town{i}, OR", "OR", 300_000 + 1000 * i) for i in range(metros)] if homes else []
    zillow_metros += [("Portland, OR", "OR", 650_000), ("Nowhere, ZZ", "ZZ", "")]  # empty latest month: skipped
    zillow_states = [(name, "", 400_000) for name in col.STATES.values()]
    iso3 = list(col.CURRENCY)
    wb = lambda v: [{"page": 1}, [{"countryiso3code": c, "date": y, "value": v} for c in iso3 for y in ("2024", "2025")]
                    + [{"countryiso3code": "ARG", "date": "2021", "value": 99.0}]]  # PPP-only year: ignored

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "Fair_Market_Rents" in url:
            if not request.url.path.endswith("/query"):
                return httpx.Response(200, json={"editingInfo": {"lastEditDate": 1759276162016}})
            if not fmr:
                return httpx.Response(200, json={"error": {"code": 400, "message": "Invalid query"}})
            start = int(request.url.params["resultOffset"])  # pages of 200, like a small maxRecordCount
            page = fmr_features[start:start + 200]
            return httpx.Response(200, json={"features": [{"attributes": a} for a in page],
                                             "exceededTransferLimit": start + 200 < len(fmr_features)})
        if "City_zhvi" in url:
            return httpx.Response(200, text=_zillow_city_csv([("Lake Oswego", "OR", PDX, 1_000_000),
                                                               ("Gresham", "OR", PDX, 640_000)]))
        if "City_zori" in url:
            return httpx.Response(200, text=_zillow_city_csv([("Lake Oswego", "OR", PDX, 2600)]))
        if "Metro_zori" in url:
            return httpx.Response(200, text=_zillow_csv([("Portland, OR", "OR", 2000)]))
        if "zillowstatic" in url:
            return httpx.Response(200, text=_zillow_csv(zillow_metros if "Metro_" in url else zillow_states))
        if "freddiemac" in url:
            return httpx.Response(200, text="date,pmms30,pmms30p,pmms15\n4/2/1971,7.33, ,\n10/29/2026,7.24,,6.6\n"
                                            "11/5/2026,7.18,,6.57\n")
        if "MARPP" in url:
            return httpx.Response(200, content=_zip("MARPP_MSA_2008_2024.csv", msa))
        if "SARPP" in url:
            return httpx.Response(200, content=_zip("SARPP_STATE_2008_2024.csv", states))
        return httpx.Response(200, json=wb(0.8 if "PRVT" in url else 1.0))
    return httpx.MockTransport(handler)


def test_build_data_parses_bea_and_world_bank():
    data = run(col.build_data(httpx.AsyncClient(transport=fake_sources())))
    assert data["us_metros"]["Portland-Vancouver-Hillsboro, OR-WA"] == 105.4  # all items, latest year
    assert data["us_states"]["OR"] == 101 and len(data["us_states"]) == 51
    assert data["countries"]["DEU"] == {"ppp": 0.8, "fx": 1.0, "year": 2025}
    # RPP parts and HUD rents (all pages of the FMR query), and the part weights fitted back exactly
    assert data["us_metro_parts"]["Portland-Vancouver-Hillsboro, OR-WA"] == {
        "goods": 105, "housing": 125, "utilities": 107, "other": 100, "fmr4": 3109, "home4": 650_000}
    assert sum(1 for p in data["us_metro_parts"].values() if p.get("fmr4")) == 321
    assert data["us_state_parts"]["OR"] == {"goods": 102, "housing": 103, "utilities": 104, "other": 105,
                                            "home4": 400_000}
    # Zillow 4-bedroom home values (latest month) and the latest weekly mortgage rate
    assert data["us_metro_parts"]["Town7, OR"]["home4"] == 307_000
    assert data["mortgage_rate"] == 7.18 and "2026-11-05" in data["sources"]["mortgage"]
    assert "2026-09" in data["sources"]["homes"] and "Zillow" in data["sources"]["homes"]
    # cities far from their metro's prices (Gresham is within 10% of Portland's $650K: left out)
    assert data["us_cities"] == {"lake oswego, OR": {"metro": PDX, "home4": 1_000_000, "rent_ratio": 1.3}}
    assert data["rpp_weights"] == pytest.approx(WEIGHTS, abs=0.005)  # Portland's all-items is off the mix
    assert data["fmr_scale"] > 0 and "2025-09-30" in data["sources"]["housing"]
    col.check_data(data, "Portland, OR")


def test_check_data_needs_hud_rents_for_family_housing():
    data = run(col.build_data(httpx.AsyncClient(transport=fake_sources())))
    for p in data["us_metro_parts"].values():
        p.pop("fmr4")
    with pytest.raises(ValueError, match="Fair Market Rent"):
        col.check_data(data, "Portland, OR", housing_share=0.4)
    col.check_data(data, "Portland, OR", housing_share=0)  # plain RPP doesn't need them


def test_check_data_needs_home_values_for_the_buy_index():
    data = run(col.build_data(httpx.AsyncClient(transport=fake_sources(homes=False))))
    col.check_data(data, "Portland, OR")
    with pytest.raises(ValueError, match="Zillow"):
        col.check_data(data, "Portland, OR", buy=True)


def test_metro_home_tries_each_principal_city():
    homes = {("prescott", "AZ"): 600_000.0, ("san jose", "CA"): 1_900_000.0}
    assert col._metro_home("Prescott Valley-Prescott, AZ", homes) == 600_000
    assert col._metro_home("San Jose-Sunnyvale-Santa Clara, CA", homes) == 1_900_000
    assert col._metro_home("Enid, OK *", homes) is None
    assert col._metro_home("Prescott, AZ Metropolitan Statistical Area", homes) == 600_000


def test_metro_without_a_home_value_uses_the_state_value():
    data = dict(BUY, us_state_parts={"CA": {"goods": 100, "housing": 150, "utilities": 120, "other": 100, "home4": 900_000}})
    data["us_metro_parts"] = dict(BUY["us_metro_parts"])
    data["us_metro_parts"]["San Jose-Sunnyvale-Santa Clara, CA"] = FAMILY["us_metro_parts"]["San Jose-Sunnyvale-Santa Clara, CA"]
    assert "own $900K 4-bed" in PayIndex(data, housing_share=0.4, ownership=OWN).resolve("San Jose, CA").buy_note


def test_failed_hud_query_fails_the_build():
    with pytest.raises(ValueError, match="HUD FMR"):
        run(col.build_data(httpx.AsyncClient(transport=fake_sources(fmr=False))))


def _runner(tmp_path, built: str):
    (tmp_path / "config").mkdir()
    seed = dict(DATA, built=built)
    (tmp_path / "config" / "col_data.json").write_text(json.dumps(seed))
    cfg = Config(database=str(tmp_path / "data" / "jobs.db"), config_dir=tmp_path / "config",
                 pay={"taxes": False})  # gross-pay math here; taxes are tested in test_taxes.py
    return Runner(cfg, Store(":memory:"), HttpClient(), Notifier(cfg.notify, dry_run=True))


def test_fresh_data_is_not_redownloaded(tmp_path, monkeypatch):
    runner = _runner(tmp_path, date.today().isoformat())
    monkeypatch.setattr(col, "build_data", lambda: pytest.fail("should not download"))
    assert run(runner.refresh_pay_data()) == ""


def test_stale_data_is_rebuilt_next_to_the_database(tmp_path, monkeypatch):
    runner = _runner(tmp_path, (date.today() - timedelta(days=40)).isoformat())
    real_build = col.build_data
    monkeypatch.setattr(col, "build_data", lambda: real_build(httpx.AsyncClient(transport=fake_sources())))
    message = run(runner.refresh_pay_data())
    assert "rebuilt" in message and runner.pay_problem == ""
    assert (tmp_path / "data" / "col_data.json").is_file()
    assert runner.pay.data["built"] == date.today().isoformat()
    assert "Town1, OR" in runner.pay.metros  # the new table is in use without a restart
    assert runner.pay.housing_share == 0.4 and "family" in runner.pay.home.label  # pay.housing_share default


def test_failed_or_broken_refresh_keeps_the_old_data(tmp_path, monkeypatch):
    old = (date.today() - timedelta(days=40)).isoformat()
    runner = _runner(tmp_path, old)
    real_build = col.build_data
    monkeypatch.setattr(col, "build_data", lambda: real_build(httpx.AsyncClient(transport=fake_sources(metros=5))))
    run(runner.refresh_pay_data())
    assert runner.pay.data["built"] == old
    assert "refresh failed" in runner.pay_problem and old in runner.pay_problem
    assert not (tmp_path / "data" / "col_data.json").exists()
    assert "Pay index: cost-of-living refresh failed" in run(runner.send_digest(mark=False))


# ---------------------------------------------------------------------------- pipeline
class PayScorer:
    def __init__(self, fit: FitScore):
        self.fit = fit
        self.calls = 0

    async def score(self, job):
        self.calls += 1
        return self.fit


@pytest.fixture
def pay_env(tmp_path, monkeypatch):
    from jobalerts import sources
    fake = FakeSource()
    monkeypatch.setitem(sources.SOURCES, "fake", fake)
    monkeypatch.setattr("jobalerts.scheduler.SOURCES", sources.SOURCES)
    (tmp_path / "col_data.json").write_text(json.dumps(DATA))
    cfg = Config.model_validate({"criteria": {"title_include": ["network engineer"]}, "config_dir": tmp_path,
                                 "database": str(tmp_path / "db" / "x.db"), "pay": {"taxes": False}})
    store = Store(":memory:")
    notifier = Notifier(cfg.notify, dry_run=True)
    runner = Runner(cfg, store, HttpClient(), notifier)
    board = Board("fake", "acme", "Acme", 600)
    fake.jobs = [make_job(0)]
    run(runner.poll_board(board))  # baseline
    return fake, runner, board, store, notifier


def test_pay_index_from_ai_reaches_alert_and_db(pay_env):
    fake, runner, board, store, notifier = pay_env
    runner.scorer = PayScorer(FitScore(score=90, summary="fit", pay_min=165_000, pay_max=210_000,
                                       pay_currency="USD", pay_period="year"))
    fake.jobs.append(make_job(1, location="San Jose, CA"))  # FakeSource.enrich: no pay in the description
    run(runner.poll_board(board))
    row = store.db.execute("SELECT status, pay_index, pay_note FROM jobs WHERE job_id='1'").fetchone()
    assert row["status"] == "alerted" and row["pay_index"] == pytest.approx(1.008, abs=1e-3)
    assert "pay index 1.01 (0.89-1.13)" in row["pay_note"]
    assert "Pay index 1.01" in notifier.sent[-1]["message"]


def test_low_stated_pay_skips_the_ai_and_shows_in_the_digest(pay_env):
    fake, runner, board, store, notifier = pay_env
    runner.scorer = scorer = PayScorer(FitScore(score=95, summary="fit"))
    job = make_job(2, location="Portland, OR")
    job.salary = "$120K-160K/yr"  # top of range 160/155 = 1.03 < 1.05
    fake.jobs.append(job)
    run(runner.poll_board(board))
    row = store.db.execute("SELECT status, reason, pay_index FROM jobs WHERE job_id='2'").fetchone()
    assert row["status"] == "low_pay" and "1.03 < 1.05" in row["reason"]
    assert scorer.calls == 0 and notifier.sent == []
    digest = run(runner.send_digest(mark=False))
    assert "Skipped for pay" in digest and "Acme: Network Engineer: pay index 0.90 (0.77-1.03)" in digest


def test_pay_in_the_description_is_checked_before_the_ai(pay_env):
    fake, runner, board, store, notifier = pay_env
    runner.scorer = scorer = PayScorer(FitScore(score=95, summary="fit"))
    job = make_job(3, location="Portland, OR")
    job.description = "The base salary range for this role is $110,000 - $140,000."
    fake.jobs.append(job)
    run(runner.poll_board(board))
    assert store.db.execute("SELECT status FROM jobs WHERE job_id='3'").fetchone()["status"] == "low_pay"
    assert scorer.calls == 0


def test_top_of_range_at_the_threshold_goes_to_the_ai(pay_env):
    fake, runner, board, store, notifier = pay_env
    runner.scorer = scorer = PayScorer(FitScore(score=95, summary="fit"))
    job = make_job(4, location="Portland, OR")
    job.salary = "$120K-165K/yr"  # 165/155 = 1.065
    fake.jobs.append(job)
    run(runner.poll_board(board))
    assert scorer.calls == 1
    assert store.db.execute("SELECT status FROM jobs WHERE job_id='4'").fetchone()["status"] == "alerted"


def test_no_stated_pay_still_goes_to_the_ai_which_may_find_it_low(pay_env):
    fake, runner, board, store, notifier = pay_env
    runner.scorer = scorer = PayScorer(FitScore(score=95, summary="fit"))
    fake.jobs.append(make_job(5, location="Portland, OR"))
    run(runner.poll_board(board))
    assert scorer.calls == 1
    assert store.db.execute("SELECT status, pay_index FROM jobs WHERE job_id='5'").fetchone()["status"] == "alerted"

    scorer.fit = FitScore(score=95, summary="fit", pay_min=100_000, pay_max=120_000, pay_currency="USD")
    fake.jobs.append(make_job(6, location="Portland, OR"))
    run(runner.poll_board(board))
    row = store.db.execute("SELECT status, score FROM jobs WHERE job_id='6'").fetchone()
    assert row["status"] == "low_pay" and row["score"] == 95 and len(notifier.sent) == 1


def test_top_matches_computes_pay_for_baseline_rows(pay_env):
    fake, runner, board, store, notifier = pay_env
    store.db.execute("UPDATE jobs SET salary='$310K', location='Hillsboro, OR' WHERE job_id='0'")
    total, jobs = runner.top_matches(5, 7)
    assert jobs[0]["pay_index"] == 2.0


@pytest.mark.parametrize("text, expected", [
    ("We raised $50M. The base salary range for this role is $150,000 - $200,000 per year.",
     Pay(150_000, 200_000, "$", "year")),
    ("The base salary range is 152,000 USD - 241,500 USD for Level 3, and 184,000 USD - 287,500 USD for Level 4.",
     Pay(152_000, 287_500, "USD", "year")),
    ("Estimated salary range for Senior $130,000-160,000 and Principal: $150,000-210,000.",
     Pay(130_000, 210_000, "$", "year")),
    ("Salary ranges for U.S. locations (USD): $105,600.00 - $158,400.00. Pay range / Gehaltsband: "
     "Ireland: 64,640.00 EUR - 96,960.00 EUR", Pay(105_600, 158_400, "$", "year")),
    ("Pay range: $45.50 to $60 per hour", Pay(45.5, 60, "$", "hour")),
    ("Estimated salary range for Senior $130,000-160,000 and Principal: 150,000-210,000. Candidates may",
     Pay(130_000, 210_000, "$", "year")),
    ("Salary: $150,000 - $180,000. Founded in 2015, 2,000-5,000 employees.", Pay(150_000, 180_000, "$", "year")),
    ("Gehalt: 80.000 € - 95.000 € brutto", Pay(80_000, 95_000, "EUR", "year")),
    ("Compensation includes a $15,000 sign-on bonus and $10,000 relocation.", None),
    ("Backed by $100,000,000 from investors.", None),
    ("We offer great pay and a 401(k).", None),
])
def test_pay_in_description(text, expected):
    assert col.pay_in_description(text) == expected


def test_reprice_applies_the_index_to_jobs_processed_before_it(pay_env):
    fake, runner, board, store, notifier = pay_env
    # Three matches scored before the pay index existed.
    for n, salary, status, score in [(7, "$120K-140K", "alerted", 90), (8, "$200K-260K", "digested", 60),
                                     (9, "", "digest", 70)]:
        job = make_job(n, location="Portland, OR")
        job.salary = salary
        store.insert_job(job, "processing")
        store.update_job(job.key, status=status, score=score)
    out = {job.job_id: (old, new, pay) for job, old, new, pay, _ in run(runner.reprice([board]))}
    assert out["7"][:2] == ("alerted", "low_pay") and out["7"][2].high < 1.05
    assert out["8"][:2] == ("digested", "digested") and out["8"][2].index > 1.4
    assert out["9"] == ("digest", "digest", None)
    rows = {r["job_id"]: r for r in store.db.execute("SELECT * FROM jobs")}
    assert rows["7"]["status"] == "low_pay" and rows["8"]["pay_index"] > 1.4
    assert [j["title"] for j in runner.top_matches(10, 7)[1]].count("Network Engineer") == 3  # 8, 9 and baseline 0


def test_reprice_restores_low_pay_jobs_that_now_pass(pay_env):
    fake, runner, board, store, notifier = pay_env
    for n, score in [(10, 80), (11, None)]:
        job = make_job(n, location="Portland, OR")
        job.salary = "$120K-160K"  # top 1.03
        store.insert_job(job, "processing")
        store.update_job(job.key, status="low_pay", score=score)
    runner.cfg.pay.min_index = 1.0
    out = {job.job_id: new for job, _, new, _, _ in run(runner.reprice([board]))}
    assert out == {"10": "digest", "11": "baseline"}  # unscored -> baseline, so backfill scores it


def test_reprice_dry_run_writes_nothing(pay_env):
    fake, runner, board, store, notifier = pay_env
    job = make_job(12, location="Portland, OR")
    job.salary = "$100K"
    store.insert_job(job, "processing")
    store.update_job(job.key, status="alerted", score=90)
    assert run(runner.reprice([board], write=False))[0][2] == "low_pay"
    assert store.db.execute("SELECT status, pay_index FROM jobs WHERE job_id='12'").fetchone()[:] == ("alerted", None)


def test_reprice_asks_the_ai_once_when_no_pay_is_stated(pay_env):
    fake, runner, board, store, notifier = pay_env
    runner.scorer = scorer = PayScorer(FitScore(score=88, summary="new", pay_min=190_000, pay_max=250_000,
                                                pay_currency="USD"))
    for n in (13, 14):
        job = make_job(n, location="Portland, OR")
        store.insert_job(job, "processing")
        store.update_job(job.key, status="alerted", score=70)
    out = {job.job_id: (new, pay, ai) for job, _, new, pay, ai in run(runner.reprice([board]))}
    assert scorer.calls == 2 and out["13"][2] and out["13"][1].low == pytest.approx(190 / 155)
    row = store.db.execute("SELECT score, score_summary, pay_low, pay_high FROM jobs WHERE job_id='13'").fetchone()
    assert row[:] == (88, "new", pytest.approx(1.226, abs=1e-3), pytest.approx(1.613, abs=1e-3))
    top = {j["url"]: j for j in runner.top_matches(10, 7)[1]}["https://x/13"]
    assert (top["score"], top["pay_low"], top["pay_high"]) == (88, 1.23, 1.61)

    # The AI found no pay for job 14 the second time round: it isn't asked again on the next run.
    scorer.fit = FitScore(score=60, summary="none")
    store.db.execute("UPDATE jobs SET pay_index=NULL, pay_note=NULL WHERE job_id='14'")
    calls = scorer.calls
    run(runner.reprice([board]))
    assert scorer.calls == calls + 1  # only job 14; job 13 keeps the pay the AI read
    run(runner.reprice([board]))
    assert scorer.calls == calls + 1
    row = store.db.execute("SELECT pay_note, pay_high FROM jobs WHERE job_id='13'").fetchone()
    assert row["pay_note"] != col.NO_PAY and row["pay_high"] == pytest.approx(1.613, abs=1e-3)
    assert runner.top_matches(10, 7)[1][0]["pay"] != col.NO_PAY


def test_reprice_dry_run_makes_no_ai_calls(pay_env):
    fake, runner, board, store, notifier = pay_env
    runner.scorer = scorer = PayScorer(FitScore(score=88, summary="new"))
    job = make_job(15, location="Portland, OR")
    store.insert_job(job, "processing")
    store.update_job(job.key, status="alerted", score=70)
    run(runner.reprice([board], write=False))
    assert scorer.calls == 0


def test_overrides_match_whole_words_and_take_any_place():
    pi = PayIndex(DATA, overrides={"PAO H": "San Jose, CA", "AUS S": "Albuquerque, NM"})
    assert "San Jose" in pi.resolve("PAO H 3120").label
    assert "San Jose" in pi.resolve("PAO H 3120, United States of America").label
    assert pi.resolve("Melbourne, AUS") is None  # "AUS S" isn't a whole-word match
    assert pi.resolve("PAOLO H") is None


# ---------------------------------------------------------------------------- renting or buying
BUY_DATA = dict(BUY, us_metro_parts=dict(BUY["us_metro_parts"], **{
    "Albuquerque, NM": {"goods": 100, "housing": 100, "utilities": 100, "other": 100, "fmr4": 3000, "home4": 200_000}}))
OWN_CFG = {"down_payment": 0.2, "property_tax": 0.01, "property_tax_by_state": {"CA": 0.012}, "upkeep": 0.005,
           "utilities": 300}


@pytest.fixture
def buy_env(tmp_path, monkeypatch):
    from jobalerts import sources
    fake = FakeSource()
    monkeypatch.setitem(sources.SOURCES, "fake", fake)
    monkeypatch.setattr("jobalerts.scheduler.SOURCES", sources.SOURCES)
    (tmp_path / "col_data.json").write_text(json.dumps(BUY_DATA))
    cfg = Config.model_validate({"criteria": {"title_include": ["network engineer"]}, "config_dir": tmp_path,
                                 "database": str(tmp_path / "db" / "x.db"),
                                 "pay": {"taxes": False, "buy": OWN_CFG}})
    store = Store(":memory:")
    notifier = Notifier(cfg.notify, dry_run=True)
    runner = Runner(cfg, store, HttpClient(), notifier, PayScorer(FitScore(score=90, summary="fit")))
    board = Board("fake", "acme", "Acme", 600)
    fake.jobs = [make_job(0)]
    run(runner.poll_board(board))  # baseline
    return fake, runner, board, store, notifier


def test_a_job_passes_the_pay_gate_when_buying_clears_it(buy_env):
    fake, runner, board, store, notifier = buy_env
    # $160K in Albuquerque: renting 160/155 x 1.08/1.08 = 1.03 (under 1.05), but a 4-bedroom home costs
    # (160K x 6% + 200K x 1.5%) / 12 + 300 = $1,350/mo -> level 0.816 against Portland's 1.152 -> buying 1.46
    job = make_job(1, location="Albuquerque, NM")
    job.salary = "$160K"
    fake.jobs.append(job)
    run(runner.poll_board(board))
    row = store.db.execute("SELECT * FROM jobs WHERE job_id='1'").fetchone()
    assert row["status"] == "alerted"
    assert row["pay_high"] == pytest.approx(160 / 155, abs=1e-3)
    assert row["buy_high"] == pytest.approx(160 / 155 * 1.152 / 0.816, abs=1e-3)
    assert "Pay index 1.03 rent · 1.46 buy" in notifier.sent[-1]["message"]
    assert runner.top_matches(10, 7)[1][0]["buy_index"] == pytest.approx(1.46, abs=0.01)


def test_a_job_under_the_bar_renting_and_buying_is_low_pay(buy_env):
    fake, runner, board, store, notifier = buy_env
    job = make_job(2, location="San Jose, CA")
    job.salary = "$160K"  # renting 0.80, buying lower still
    fake.jobs.append(job)
    run(runner.poll_board(board))
    row = store.db.execute("SELECT status, reason, buy_high FROM jobs WHERE job_id='2'").fetchone()
    assert row["status"] == "low_pay" and row["buy_high"] < 0.8
    assert "< 1.05" in row["reason"] and runner.scorer.calls == 0


# ---------------------------------------------------------------------------- staying put, the job's city
SJ = "San Jose-Sunnyvale-Santa Clara, CA"
STAY = col.CurrentHome(payment=3000, principal=500, upkeep=300, rate=4.5)
CITY_DATA = dict(BUY, us_cities={"palo alto, CA": {"metro": SJ, "home4": 3_000_000, "rent_ratio": 1.5}})


def test_staying_in_your_own_home_is_the_reference():
    pi = PayIndex(CITY_DATA, housing_share=0.4, ownership=OWN, current_home=STAY)
    # staying: 3,000 - 500 + 300 + utilities 300 x 100/100 = $3,100/mo -> 60 + 40% x 3,100 x 0.04 = 109.6
    assert pi.home.level == pytest.approx(1.096) and pi.home.buy_level == pytest.approx(1.096)
    assert "staying in your home at 4.5%" in pi.home.buy_note and "$3.1K/mo" in pi.home.buy_note
    for where in ("Portland, OR", "Hillsboro, OR", "Remote"):  # you'd keep the house: 1.0 either way
        r = pi.index(Pay(155_000, 155_000), where)
        assert (r.index, r.buy_index) == pytest.approx((1.0, 1.0))
    sj = pi.index(Pay(155_000, 155_000), "San Jose, CA")
    assert sj.index == pytest.approx(1.096 / 1.40)
    assert sj.buy_index == pytest.approx(1.096 / ((60 + 0.4 * 10_200 * 0.04) / 100))  # buying at today's 6%
    assert PayIndex(CITY_DATA, housing_share=0.4, ownership=OWN).home.buy_level == pytest.approx(1.152)  # off


def test_the_jobs_city_moves_metro_prices_by_city_weight():
    pi = PayIndex(CITY_DATA, housing_share=0.4, ownership=OWN, city_weight=0.5)
    pa = pi.resolve("Palo Alto, CA")
    # rents: FMR $5,000 x (1 + 0.5 x 0.5) = $6,250 -> 60 + 40% x 250 = 160
    assert pa.level == pytest.approx(1.60) and "50% toward Palo Alto" in pa.label
    # home: halfway between the metro's $1.8M and the city's $3M = $2.4M
    assert "own $2.40M 4-bed" in pa.buy_note
    assert pi.resolve("Sunnyvale, CA").level == pytest.approx(1.40)       # not in the city table: the metro
    assert PayIndex(CITY_DATA, housing_share=0.4, ownership=OWN).resolve("Palo Alto, CA").level == pytest.approx(1.40)


def test_city_table_keeps_cities_that_differ_from_their_metro():
    parts = {SJ: {"home4": 2_000_000}, "Boise City, ID": {"home4": 500_000}}
    homes = [{"city": "palo alto", "state": "CA", "metro": SJ, "value": 4_000_000},
             {"city": "campbell", "state": "CA", "metro": SJ, "value": 2_100_000},       # within 10%: dropped
             {"city": "nampa", "state": "ID", "metro": "Boise City, ID", "value": 520_000},
             {"city": "elsewhere", "state": "ZZ", "metro": "Not a BEA metro", "value": 1}]
    rents = [{"city": "palo alto", "state": "CA", "metro": SJ, "value": 4800},
             {"city": "nampa", "state": "ID", "metro": "Boise City, ID", "value": 1500}]
    table = col._city_table(parts, homes, rents, {("san jose", "CA"): 4000.0, ("boise city", "ID"): 2000.0})
    assert table == {"palo alto, CA": {"metro": SJ, "home4": 4_000_000, "rent_ratio": 1.2},
                     "nampa, ID": {"metro": "Boise City, ID", "home4": 520_000, "rent_ratio": 0.75}}


def test_runner_takes_current_home_and_city_weight_from_the_config(tmp_path):
    (tmp_path / "col_data.json").write_text(json.dumps(CITY_DATA))
    cfg = Config.model_validate({"config_dir": tmp_path, "database": str(tmp_path / "x.db"), "pay": {
        "taxes": False, "buy": OWN_CFG, "city_weight": 0.5,
        "current_home": {"payment": 3000, "principal": 500, "upkeep": 300, "rate": 4.5}}})
    runner = Runner(cfg, Store(":memory:"), HttpClient(), Notifier(cfg.notify, dry_run=True))
    assert runner.pay.home.level == pytest.approx(1.096) and "staying" in runner.pay.home.buy_note
    assert runner.pay.resolve("Palo Alto, CA").level == pytest.approx(1.60)


def _http_error(status: int, url: str, body: str = "") -> httpx.HTTPStatusError:
    request = httpx.Request("GET", url)
    return httpx.HTTPStatusError("x", request=request, response=httpx.Response(status, text=body, request=request))


def test_posting_gone_knows_a_taken_down_posting_from_a_blocked_request():
    from jobalerts.http import posting_gone
    wd = "https://boeing.wd1.myworkdayjobs.com/wday/cxs/boeing/EXTERNAL_CAREERS/job/x"
    assert posting_gone(_http_error(404, "https://boards-api.greenhouse.io/v1/boards/a/jobs/1"))
    assert posting_gone(_http_error(410, "https://x/1"))
    assert posting_gone(_http_error(403, wd, '{"errorCode":"S22","httpStatus":403,"message":"permission denied"}'))
    assert not posting_gone(_http_error(403, wd, "<html>Access Denied</html>"))  # a block, not a closed job
    assert not posting_gone(_http_error(403, "https://x/1", '"S22"'))
    assert not posting_gone(_http_error(429, "https://x/1"))
    assert not posting_gone(RuntimeError("boom"))


def _gone(fake, job_ids, status=404):
    original = fake.enrich

    async def enrich(job, board, http):
        if job.job_id in job_ids:
            raise _http_error(status, f"https://x/{job.job_id}")
        await original(job, board, http)
    fake.enrich = enrich


def test_reprice_closes_postings_that_were_taken_down(pay_env):
    fake, runner, board, store, notifier = pay_env
    for n in (16, 17):
        job = make_job(n, location="Portland, OR")
        job.salary = "$200K-260K"
        store.insert_job(job, "processing")
        store.update_job(job.key, status="digest", score=80)
    _gone(fake, {"16"})
    assert run(runner.reprice([board], write=False))[0][2] == "closed"
    assert store.db.execute("SELECT status FROM jobs WHERE job_id='16'").fetchone()[0] == "digest"  # dry run
    out = {job.job_id: (old, new) for job, old, new, _, _ in run(runner.reprice([board]))}
    assert out == {"16": ("digest", "closed"), "17": ("digest", "digest")}
    assert store.db.execute("SELECT reason FROM jobs WHERE job_id='16'").fetchone()[0] == "posting taken down (HTTP 404)"
    assert "https://x/16" not in {j["url"] for j in runner.top_matches(10, 7)[1]}


def test_backfill_closes_a_taken_down_baseline_job_without_scoring_it(pay_env):
    fake, runner, board, store, notifier = pay_env
    runner.scorer = scorer = PayScorer(FitScore(score=90, summary="fit"))
    _gone(fake, {"0"})
    assert [(job.job_id, status) for job, status, _ in run(runner.backfill([board]))] == [("0", "closed")]
    assert scorer.calls == 0
    assert store.db.execute("SELECT status FROM jobs WHERE job_id='0'").fetchone()[0] == "closed"


def test_top_stories_reach_db_alert_and_dashboard(pay_env):
    fake, runner, board, store, notifier = pay_env
    runner.scorer = PayScorer(FitScore(score=90, summary="fit", top_stories=["gun", "beam"],
                                       top_story_titles=["Designed an electron gun", "Halved beam loss"]))
    fake.jobs.append(make_job(20, location="Portland, OR"))
    run(runner.poll_board(board))
    assert json.loads(store.db.execute("SELECT top_stories FROM jobs WHERE job_id='20'").fetchone()[0]) == [
        ["gun", "Designed an electron gun"], ["beam", "Halved beam loss"]]
    assert "Your best stories: Designed an electron gun; Halved beam loss" in notifier.sent[-1]["message"]
    top = {j["url"]: j for j in runner.top_matches(10, 7)[1]}["https://x/20"]
    assert top["stories"] == ["Designed an electron gun", "Halved beam loss"] and top["story_ids"] == ["gun", "beam"]


def test_rescore_updates_scores_and_never_pushes(pay_env):
    fake, runner, board, store, notifier = pay_env
    for n, status, score in [(21, "digest", 60), (22, "low", 40), (23, "alerted", 90), (24, "digest", 70)]:
        job = make_job(n, location="Portland, OR")
        store.insert_job(job, "processing")
        store.update_job(job.key, status=status, score=score)
    _gone(fake, {"24"})
    runner.scorer = PayScorer(FitScore(score=95, summary="new", top_stories=["gun"],
                                       top_story_titles=["Designed an electron gun"]))
    sent = len(notifier.sent)
    out = {job.job_id: (old, new, fit) for job, old, new, fit in run(runner.rescore([board]))}
    assert {k: v[:2] for k, v in out.items()} == {"21": ("digest", "digest"), "22": ("low", "digest"),
                                                  "23": ("alerted", "alerted"), "24": ("digest", "closed")}
    assert len(notifier.sent) == sent                                   # nothing pushed
    rows = {r["job_id"]: r for r in store.db.execute("SELECT * FROM jobs WHERE job_id IN ('21','22','23')")}
    assert all(r["score"] == 95 and r["score_summary"] == "new" for r in rows.values())
    assert stories_from_row(rows["22"]) == [("gun", "Designed an electron gun")]

    runner.scorer = PayScorer(FitScore(score=30, summary="worse"))
    out = {job.job_id: new for job, _, new, _ in run(runner.rescore([board]))}
    assert out == {"21": "low", "22": "low", "23": "alerted"}
