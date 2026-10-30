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
from jobalerts.store import Store

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
    col.check_data(data, "Portland, OR")
    pi = PayIndex(data)
    assert "Portland-Vancouver-Hillsboro" in pi.home.label
    assert pi.index(Pay(310_000, 310_000, "USD"), "Hillsboro, OR").index == pytest.approx(2.0)


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


def fake_sources(metros: int = 320) -> httpx.MockTransport:
    msa = [f' "{10000 + i}","Town{i}, OR (Metropolitan Statistical Area)", ,MARPP,1,"...","RPPs: All items ","Index",99,{90 + i % 30}'
           for i in range(metros)]
    msa.append(' "38900","Portland-Vancouver-Hillsboro, OR-WA (Metropolitan Statistical Area)", ,MARPP,1,"...","RPPs: All items ","Index",106,105.4')
    msa.append(' "38900","Portland-Vancouver-Hillsboro, OR-WA (Metropolitan Statistical Area)", ,MARPP,3,"...","Housing","Index",125,125')
    states = [f' "{i:02d}000","{name}",8,SARPP,1,"...","RPPs: All items ","Index",100,101'
              for i, name in enumerate(col.STATES.values())]
    iso3 = list(col.CURRENCY)
    wb = lambda v: [{"page": 1}, [{"countryiso3code": c, "date": y, "value": v} for c in iso3 for y in ("2024", "2025")]
                    + [{"countryiso3code": "ARG", "date": "2021", "value": 99.0}]]  # PPP-only year: ignored

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
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
    col.check_data(data, "Portland, OR")


def _runner(tmp_path, built: str):
    (tmp_path / "config").mkdir()
    seed = dict(DATA, built=built)
    (tmp_path / "config" / "col_data.json").write_text(json.dumps(seed))
    cfg = Config(database=str(tmp_path / "data" / "jobs.db"), config_dir=tmp_path / "config")
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
                                 "database": str(tmp_path / "db" / "x.db")})
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
    ("Gehalt: 80.000 € - 95.000 € brutto", Pay(80_000, 95_000, "EUR", "year")),
    ("Compensation includes a $15,000 sign-on bonus and $10,000 relocation.", None),
    ("Backed by $100,000,000 from investors.", None),
    ("We offer great pay and a 401(k).", None),
])
def test_pay_in_description(text, expected):
    assert col.pay_in_description(text) == expected
