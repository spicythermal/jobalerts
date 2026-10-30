"""Cost-of-living pay index: what a base salary buys where the job is, relative to a reference.

    index = (salary in US dollars / local price level) / (reference salary / home price level)

With the defaults, $155K base in the Portland metro is 1.0; $310K in a place with Portland's prices
is 2.0; $155K somewhere 10% cheaper than Portland is 1.11.

Price levels are all relative to the US average (= 1.0) and come from `col_data.json`, which
`jobalerts pay --update` builds from two free, official sources:

- US metro areas and states: BEA Regional Price Parities (RPP, "all items"). One number per metro
  area or state covering goods, rents and other services, from the CPI and the Census's American
  Community Survey rents. Released each December for the year before last.
- Other countries: World Bank purchasing-power parity for household consumption (PA.NUS.PRVT.PP)
  divided by the official exchange rate (PA.NUS.FCRF). That ratio is the country's price level
  against the US. Country-wide only: there is no free official city-level series outside the US.

Taxes, benefits, bonus and equity are not included; this is base pay against prices.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
import zipfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

BEA_URLS = {"msa": "https://apps.bea.gov/regional/zip/MARPP.zip", "state": "https://apps.bea.gov/regional/zip/SARPP.zip"}
WB_URL = "https://api.worldbank.org/v2/country/all/indicator/{}?format=json&date={}:{}&per_page=20000"
WB_PPP, WB_FX = "PA.NUS.PRVT.PP", "PA.NUS.FCRF"

STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
    "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}
_STATE_BY_NAME = {v.lower(): k for k, v in STATES.items()}

# Country -> currency, for the countries this search reaches. Euro countries share one exchange rate.
CURRENCY = {
    "USA": "USD", "CAN": "CAD", "MEX": "MXN", "BRA": "BRL", "ARG": "ARS", "CHL": "CLP", "COL": "COP",
    "GBR": "GBP", "IRL": "EUR", "DEU": "EUR", "FRA": "EUR", "NLD": "EUR", "BEL": "EUR", "LUX": "EUR",
    "AUT": "EUR", "ITA": "EUR", "ESP": "EUR", "PRT": "EUR", "FIN": "EUR", "GRC": "EUR", "EST": "EUR",
    "LVA": "EUR", "LTU": "EUR", "SVK": "EUR", "SVN": "EUR", "HRV": "EUR", "CHE": "CHF", "SWE": "SEK",
    "NOR": "NOK", "DNK": "DKK", "POL": "PLN", "CZE": "CZK", "HUN": "HUF", "ROU": "RON", "ISR": "ILS",
    "JPN": "JPY", "KOR": "KRW", "CHN": "CNY", "IND": "INR", "SGP": "SGD", "HKG": "HKD", "AUS": "AUD",
    "NZL": "NZD", "ARE": "AED", "MYS": "MYR", "THA": "THB", "TUR": "TRY", "ZAF": "ZAR", "CRI": "CRC",
}
_DOLLARS = {"USD", "CAD", "AUD", "NZD", "SGD", "HKD"}

# Location words -> country. English and local names, plus 2-letter codes that aren't US states.
COUNTRY_NAMES = {
    "united states": "USA", "united states of america": "USA", "usa": "USA", "us": "USA", "u.s.": "USA",
    "canada": "CAN", "mexico": "MEX", "méxico": "MEX", "brazil": "BRA", "brasil": "BRA", "argentina": "ARG",
    "chile": "CHL", "colombia": "COL", "united kingdom": "GBR", "uk": "GBR", "gb": "GBR", "england": "GBR",
    "scotland": "GBR", "wales": "GBR", "great britain": "GBR", "ireland": "IRL", "ie": "IRL",
    "germany": "DEU", "deutschland": "DEU", "france": "FRA", "fr": "FRA", "netherlands": "NLD",
    "the netherlands": "NLD", "nederland": "NLD", "holland": "NLD", "nl": "NLD", "belgium": "BEL",
    "belgique": "BEL", "belgië": "BEL", "be": "BEL", "luxembourg": "LUX", "lu": "LUX", "austria": "AUT",
    "österreich": "AUT", "at": "AUT", "italy": "ITA", "italia": "ITA", "it": "ITA", "spain": "ESP",
    "españa": "ESP", "es": "ESP", "portugal": "PRT", "pt": "PRT", "finland": "FIN", "suomi": "FIN",
    "fi": "FIN", "greece": "GRC", "estonia": "EST", "latvia": "LVA", "lithuania": "LTU", "slovakia": "SVK",
    "slovenia": "SVN", "croatia": "HRV", "switzerland": "CHE", "schweiz": "CHE", "suisse": "CHE",
    "svizzera": "CHE", "ch": "CHE", "sweden": "SWE", "sverige": "SWE", "se": "SWE", "norway": "NOR",
    "norge": "NOR", "no": "NOR", "denmark": "DNK", "danmark": "DNK", "dk": "DNK", "poland": "POL",
    "polska": "POL", "pl": "POL", "czech republic": "CZE", "czechia": "CZE", "cz": "CZE", "hungary": "HUN",
    "romania": "ROU", "israel": "ISR", "japan": "JPN", "jp": "JPN", "south korea": "KOR",
    "korea": "KOR", "republic of korea": "KOR", "kr": "KOR", "china": "CHN", "cn": "CHN", "india": "IND",
    "singapore": "SGP", "sg": "SGP", "hong kong": "HKG", "hk": "HKG", "australia": "AUS", "au": "AUS",
    "new zealand": "NZL", "nz": "NZL", "united arab emirates": "ARE", "uae": "ARE", "malaysia": "MYS",
    "my": "MYS", "thailand": "THA", "th": "THA", "turkey": "TUR", "türkiye": "TUR", "south africa": "ZAF",
    "za": "ZAF", "costa rica": "CRI",
}

# Cities that often appear without a country. US places are matched separately ("City, ST").
CITIES = {
    "london": "GBR", "manchester": "GBR", "bristol": "GBR", "edinburgh": "GBR", "glasgow": "GBR",
    "oxford": "GBR", "abingdon": "GBR", "culham": "GBR", "harwell": "GBR", "munich": "DEU",
    "münchen": "DEU", "berlin": "DEU", "hamburg": "DEU", "frankfurt": "DEU", "stuttgart": "DEU",
    "jena": "DEU", "oberkochen": "DEU", "dresden": "DEU", "darmstadt": "DEU", "karlsruhe": "DEU",
    "aachen": "DEU", "garching": "DEU", "ottobrunn": "DEU", "erlangen": "DEU", "paris": "FRA",
    "toulouse": "FRA", "grenoble": "FRA", "lyon": "FRA", "saclay": "FRA", "amsterdam": "NLD",
    "eindhoven": "NLD", "veldhoven": "NLD", "delft": "NLD", "rotterdam": "NLD", "almere": "NLD",
    "nijmegen": "NLD", "veenendaal": "NLD", "best": "NLD", "zurich": "CHE", "zürich": "CHE", "geneva": "CHE", "genève": "CHE",
    "basel": "CHE", "lausanne": "CHE", "villigen": "CHE", "meyrin": "CHE", "vienna": "AUT", "wien": "AUT",
    "brussels": "BEL", "leuven": "BEL", "louvain-la-neuve": "BEL", "copenhagen": "DNK", "lund": "SWE",
    "stockholm": "SWE", "gothenburg": "SWE", "oslo": "NOR", "helsinki": "FIN", "dublin": "IRL",
    "madrid": "ESP", "barcelona": "ESP", "milan": "ITA", "rome": "ITA", "warsaw": "POL", "prague": "CZE",
    "tel aviv": "ISR", "tokyo": "JPN", "kawasaki": "JPN", "hamamatsu": "JPN", "osaka": "JPN", "yokohama": "JPN",
    "seoul": "KOR", "hwaseong": "KOR", "suwon": "KOR", "shanghai": "CHN", "beijing": "CHN",
    "shenzhen": "CHN", "bangalore": "IND", "bengaluru": "IND", "hyderabad": "IND", "pune": "IND",
    "sydney": "AUS", "melbourne": "AUS", "toronto": "CAN", "montreal": "CAN", "vancouver": "CAN",
    "ottawa": "CAN", "são paulo": "BRA", "sao paulo": "BRA", "campinas": "BRA", "rio de janeiro": "BRA",
    "buenos aires": "ARG", "bariloche": "ARG", "santiago": "CHL", "mexico city": "MEX",
}

# Suburbs that host this search's employers but aren't in their metro area's name -> a city that is.
SUBURBS = {
    ("beaverton", "OR"): "portland", ("tigard", "OR"): "portland", ("tualatin", "OR"): "portland",
    ("wilsonville", "OR"): "portland", ("lake oswego", "OR"): "portland", ("gresham", "OR"): "portland",
    ("forest grove", "OR"): "portland", ("oregon city", "OR"): "portland", ("camas", "WA"): "vancouver",
    ("livermore", "CA"): "oakland", ("pleasanton", "CA"): "oakland", ("hayward", "CA"): "oakland",
    ("palo alto", "CA"): "san jose", ("mountain view", "CA"): "san jose", ("milpitas", "CA"): "san jose",
    ("cupertino", "CA"): "san jose", ("menlo park", "CA"): "san francisco",
    ("redwood city", "CA"): "san francisco", ("san mateo", "CA"): "san francisco",
    ("redmond", "WA"): "seattle", ("kirkland", "WA"): "seattle", ("bothell", "WA"): "seattle",
    ("bedford", "MA"): "boston", ("billerica", "MA"): "boston", ("lexington", "MA"): "boston",
    ("burlington", "MA"): "boston", ("waltham", "MA"): "boston", ("woburn", "MA"): "boston",
    ("golden", "CO"): "denver", ("broomfield", "CO"): "denver",
}

_SYMBOLS = [("us$", "USD"), ("s$", "SGD"), ("r$", "BRL"), ("c$", "CAD"), ("ca$", "CAD"), ("a$", "AUD"),
            ("hk$", "HKD"), ("€", "EUR"), ("£", "GBP"), ("¥", "JPY"), ("₹", "INR"), ("₩", "KRW"), ("$", "$")]
_CODES = re.compile(r"\b(" + "|".join(sorted(set(CURRENCY.values()))) + r")\b")
_NUMBER = re.compile(r"(?<![\d.,])(\d{1,3}(?:[,.   ]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)"
                     r"(\s?(?i:k|m|mm|million)\b)?(\s?%)?")
PERIODS = {"year": 1, "month": 12, "week": 52, "day": 260, "hour": 2080}
_PERIOD_WORDS = [("hour", r"hour|\bhr\b|/h\b|stunde|heure"), ("month", r"month|/mo\b|monat|mois|mensal|mês|\bmes\b"),
                 ("week", r"week|/wk\b|woche"), ("day", r"/day|per day|daily|\btag\b")]


@dataclass
class Pay:
    low: float
    high: float
    currency: str = ""   # ISO code; "" = the local currency of the job's country; "$" = some dollar
    period: str = "year"

    def annual(self) -> tuple[float, float]:
        k = PERIODS.get(self.period, 1)
        return self.low * k, self.high * k

    def text(self) -> str:
        def fmt(v: float) -> str:
            return f"{v / 1e6:.2g}M" if v >= 1e6 else f"{v / 1000:.0f}K" if v >= 1000 else f"{v:g}"
        cur = "$" if self.currency in ("USD", "$") else f"{self.currency} " if self.currency else ""
        amount = fmt(self.low) if self.low == self.high else f"{fmt(self.low)}-{fmt(self.high)}"
        return f"{cur}{amount}/{'yr' if self.period == 'year' else self.period}"


@dataclass
class Place:
    label: str
    level: float          # price level, US average = 1.0
    country: str = "USA"  # ISO3


@dataclass
class PayResult:
    index: float          # midpoint of the range
    low: float
    high: float
    pay: Pay
    place: Place

    def text(self) -> str:
        """Short form for phone alerts and listings."""
        spread = f" ({self.low:.2f}-{self.high:.2f})" if round(self.low, 2) != round(self.high, 2) else ""
        return f"pay index {self.index:.2f}{spread}: {self.pay.text()} in {self.place.label}"


def _parts(location: str) -> list[str]:
    return [p.strip().lower() for p in re.split(r"[,;()/|>\-–]", location) if p.strip()]


def _amount(token: str) -> float:
    token = re.sub(r"[   ]", "", token)
    m = re.fullmatch(r"(\d+(?:[.,]\d{3})*)(?:[.,](\d{1,2}))?", token)
    if not m:
        return 0.0
    return float(re.sub(r"[.,]", "", m.group(1)) + (f".{m.group(2)}" if m.group(2) else ""))


def parse_salary(text: str) -> Pay | None:
    """'$155K-200K/yr', '€80.000 – €95.000', '$45.50 - $60/hour' -> Pay. None if no amount is found."""
    if not text:
        return None
    low = text.lower()
    currency = next((code for sym, code in _SYMBOLS if sym in low), "")
    if m := _CODES.search(text):
        currency = m.group(1).upper()
    amounts = []
    for m in _NUMBER.finditer(text):
        if m.group(3):  # a percentage (equity), not money
            continue
        value = _amount(m.group(1))
        suffix = (m.group(2) or "").strip().lower()
        value *= 1000 if suffix == "k" else 1e6 if suffix else 1
        if value > 0:
            amounts.append(value)
        if len(amounts) == 2:
            break
    if not amounts:
        return None
    lo, hi = amounts[0], amounts[-1]
    if hi < lo:
        hi = lo
    period = next((p for p, rx in _PERIOD_WORDS if re.search(rx, low)), "")
    if not period:
        period = "hour" if hi < 500 else "year"
    return Pay(lo, hi, currency, period)


# A money amount with a currency marker, and an optional second amount after a range separator.
_MONEY = (r"(?:(?:US|S|R|C|CA|A|HK)?\$|€|£|\b(?:USD|EUR|GBP|CHF|SGD|CAD|AUD|SEK|DKK|NOK)\b)\s?\d[\d,.\u00a0\u202f]*\d(?:\s?[kK]\b)?"
          r"|\d[\d,.\u00a0\u202f]*\d(?:\s?[kK]\b)?\s?(?:€|\b(?:EUR|USD|CHF|GBP)\b)")
_PAY_RANGE = re.compile(rf"(?:{_MONEY})(?:\s*(?:-|–|—|to|and|bis|à)\s*(?:(?:{_MONEY})|\d[\d,.]*\d(?:\s?[kK]\b)?))?")
# A range with no currency marker ("Principal: 150,000-210,000"), counted only right after a marked one.
_BARE_RANGE = re.compile(r"(?<![\d.,])\d{2,3}(?:[,.]\d{3})+(?:[.,]\d{2})?\s*(?:-|–|—|to)\s*\$?\d{2,3}(?:[,.]\d{3})+"
                         r"|(?<![\d.,])\d{2,3}\s?[kK]\s*(?:-|–|—|to)\s*\d{2,3}\s?[kK]\b")
_NOT_BASE = re.compile(r"(?i)bonus|sign-on|signing|relocation|stipend|allowance|referral|tuition|401")
_PAY_WORDS = re.compile(r"(?i)salary|\bpay\b|compensation|\bbase\b|\bwage|gehalt|vergütung|salaire|rémunération"
                        r"|hourly rate|annual|per year")


def _plausible(pay: Pay, currency: str) -> bool:
    low, high = pay.annual()
    return low >= 20_000 and not (high > 2_000_000 and currency in ("$", "USD", "EUR", "GBP", "CHF"))


def pay_in_description(text: str) -> Pay | None:
    """Salary amounts or ranges in a posting's text: currency amounts within a few lines after words
    like 'salary', 'pay range' or 'compensation', so funding rounds and revenue figures elsewhere are
    skipped, and not next to 'bonus', 'relocation' or 'stipend'. When the posting gives several ranges
    (by level: "Level 3 ... and Level 4 ..."), returns the span from the lowest to the highest in the
    first one's currency, so a higher level isn't missed. None when the posting states no pay."""
    found: list[Pay] = []
    text = text or ""
    for m in _PAY_RANGE.finditer(text):
        if not _PAY_WORDS.search(text[max(0, m.start() - 250):m.start()]):
            continue
        if _NOT_BASE.search(text[max(0, m.start() - 40):m.end() + 40]):
            continue
        pay = parse_salary(m.group(0))
        if pay is None:
            continue
        if period := next((p for p, rx in _PERIOD_WORDS if re.search(rx, text[m.end():m.end() + 40].lower())), None):
            pay.period = period
        if not _plausible(pay, pay.currency):
            continue
        if found and (pay.currency, pay.period) != (found[0].currency, found[0].period):
            continue
        found.append(pay)
        # Unmarked ranges just after this one are more levels in the same currency.
        for bare in _BARE_RANGE.finditer(text[m.end():m.end() + 200]):
            extra = parse_salary(bare.group(0))
            if extra and extra.period == pay.period and _plausible(extra, pay.currency):
                found.append(Pay(extra.low, extra.high, pay.currency, pay.period))
    if not found:
        return None
    return Pay(min(p.low for p in found), max(p.high for p in found), found[0].currency, found[0].period)


# ---------------------------------------------------------------------------- the data file
def _bea_rows(blob: bytes, prefix: str) -> tuple[int, list[tuple[str, str, float]]]:
    """(latest year, [(fips, name, all-items RPP)]) from a BEA regional zip."""
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        name = next(n for n in z.namelist() if n.startswith(prefix) and n.endswith(".csv"))
        reader = csv.reader(io.StringIO(z.read(name).decode("utf-8-sig", errors="replace")))
        header = next(reader)
        years = [i for i, h in enumerate(header) if h.strip().isdigit()]
        latest, rows = 0, []
        for r in reader:
            if len(r) <= years[-1] or r[4].strip() != "1":  # LineCode 1 = all items
                continue
            for i in reversed(years):
                try:
                    value = float(r[i])
                except ValueError:
                    continue
                latest = max(latest, int(header[i]))
                rows.append((r[0].strip().strip('"'), r[1].strip(), value))
                break
        return latest, rows


def _wb_values(payload: list) -> dict[str, dict[int, float]]:
    out: dict[str, dict[int, float]] = {}
    for r in payload[1] or []:
        if r.get("value") and r.get("countryiso3code"):
            out.setdefault(r["countryiso3code"], {})[int(r["date"])] = r["value"]
    return out


async def build_data(client: httpx.AsyncClient | None = None) -> dict:
    """Download the BEA and World Bank tables into the col_data.json structure."""
    own = client is None
    client = client or httpx.AsyncClient(timeout=60, follow_redirects=True,
                                         headers={"User-Agent": "jobalerts cost-of-living update"})
    try:
        blobs = {}
        for key, url in BEA_URLS.items():
            r = await client.get(url)
            r.raise_for_status()
            blobs[key] = r.content
        wb = {}
        for code in (WB_PPP, WB_FX):
            r = await client.get(WB_URL.format(code, date.today().year - 8, date.today().year))
            r.raise_for_status()
            wb[code] = _wb_values(r.json())
    finally:
        if own:
            await client.aclose()

    msa_year, msa_rows = _bea_rows(blobs["msa"], "MARPP")
    state_year, state_rows = _bea_rows(blobs["state"], "SARPP")
    metros = {re.sub(r"\s*\(Metropolitan Statistical Area\)", "", n): v
              for fips, n, v in msa_rows if fips not in ("00000", "00999")}
    nonmetro = next((v for fips, _, v in msa_rows if fips == "00999"), None)
    states = {_STATE_BY_NAME[n.lower()]: v for _, n, v in state_rows if n.lower() in _STATE_BY_NAME}
    countries = {}
    for iso3, ppp in wb[WB_PPP].items():
        # PPP and exchange rate from the same year: with high inflation (Argentina) a mismatch is off 30x.
        years = set(ppp) & set(wb[WB_FX].get(iso3, {}))
        if iso3 in CURRENCY and years:
            year = max(years)
            countries[iso3] = {"ppp": ppp[year], "fx": wb[WB_FX][iso3][year], "year": year}
    return {
        "built": date.today().isoformat(),
        "sources": {
            "us": f"BEA Regional Price Parities, all items, {msa_year} (MARPP, SARPP)",
            "countries": f"World Bank {WB_PPP} / {WB_FX}, latest year with both, per country",
        },
        "us_metros": metros, "us_nonmetro": nonmetro, "us_states": states, "countries": countries,
    }


# ---------------------------------------------------------------------------- the index
class PayIndex:
    def __init__(self, data: dict, reference_salary: float = 155_000, reference_location: str = "Portland, OR",
                 overrides: dict[str, str] | None = None):
        self.data = data
        self.metros: dict[str, float] = data["us_metros"]
        self.states: dict[str, float] = data["us_states"]
        self.countries: dict[str, dict] = data["countries"]
        self.overrides = {k.lower(): v for k, v in (overrides or {}).items()}
        self._cities: dict[tuple[str, str], str] = {}
        for name in self.metros:
            cities, _, states = name.rpartition(", ")
            for city in re.split(r"[-/]+", cities):
                for st in states.split("-"):
                    self._cities.setdefault((city.strip().lower(), st), name)
        self.reference_salary = reference_salary
        self.home: Place | None = None
        home = self.resolve(reference_location)
        if home is None:
            raise ValueError(f"pay.reference_location {reference_location!r} isn't a known place")
        self.home = home

    @classmethod
    def load(cls, path: Path, **kwargs) -> PayIndex:
        return cls(json.loads(path.read_text(encoding="utf-8")), **kwargs)

    # -- places
    def _metro(self, name: str) -> Place:
        return Place(f"{name} metro (RPP {self.metros[name]:.1f})", self.metros[name] / 100)

    def _state(self, st: str) -> Place | None:
        if st not in self.states:
            return None
        return Place(f"{STATES[st]} (state RPP {self.states[st]:.1f})", self.states[st] / 100)

    def _country(self, iso3: str) -> Place | None:
        if iso3 == "USA":
            return Place("US average", 1.0)
        c = self.countries.get(iso3)
        if not c:
            return None
        level = c["ppp"] / c["fx"]
        return Place(f"{iso3} (price level {level:.2f}x US)", level, iso3)

    def _override(self, text: str) -> Place | None:
        for needle, target in self.overrides.items():
            if needle in text:
                if target.lower() == "home":
                    return self.home
                if target in self.metros:
                    return self._metro(target)
                return self._state(target) or self._country(target.upper())
        return None

    def resolve(self, location: str, remote: bool = False) -> Place | None:
        """Best price level for a free-text location. Remote jobs take the home price level unless the
        location names a foreign country (a remote job in Germany means living in Germany)."""
        text = location.lower()
        if place := self._override(text):
            return place
        foreign = self._foreign_country(location)
        if remote or "remote" in text:
            if foreign and foreign != "USA":
                return self._country(foreign)
            if self.home:
                return self.home
        # "Hillsboro, OR", "Boston, Massachusetts", Workday-style "US-OR-Hillsboro"
        pairs = [(c, s) for c, s in re.findall(r"([A-Za-z][A-Za-z .'\-]*?),\s*([A-Z]{2})\b", location)]
        pairs += [(c, _STATE_BY_NAME[s.lower()]) for c, s in re.findall(
            r"([A-Za-z][A-Za-z .'\-]*?),\s*([A-Za-z][A-Za-z ]+?)(?=,|;|\)|$)", location) if s.lower() in _STATE_BY_NAME]
        pairs += [(c, s) for s, c in re.findall(r"\bUSA?[-, ]+([A-Z]{2})[-, ]+([A-Za-z][A-Za-z .']*)", location)]
        pairs += re.findall(r"([A-Za-z][A-Za-z .']*?)-([A-Z]{2})\b", location)  # "Wilmington-MA"
        pairs += [(c, st) for c in re.findall(r"(?i)greater (.+?) area", location)
                  for (city, st) in self._cities if city == c.strip().lower()][:1]
        parts = _parts(location)  # "US > Arizona > Phoenix"
        pairs += [(c, _STATE_BY_NAME[s]) for s in parts if s in _STATE_BY_NAME for c in parts if c != s]
        for city, st in pairs:
            city = re.sub(r"^.*\b(?:hybrid|onsite|on-site|usa?)\b[\s\-:]*", "", city.strip(), flags=re.I).lower()
            if name := self._cities.get((SUBURBS.get((city, st), city), st)):
                return self._metro(name)
        if foreign and foreign != "USA":
            return self._country(foreign)
        for _, st in pairs:
            if place := self._state(st):
                return place
        if foreign == "USA":
            return self._country("USA")
        return None

    def _foreign_country(self, location: str) -> str | None:
        """ISO3 of a country named in the location (USA included), else of a known foreign city."""
        parts = _parts(location)
        for part in reversed(parts):
            if part in COUNTRY_NAMES:
                return COUNTRY_NAMES[part]
        for part in parts:
            if part in CITIES:
                return CITIES[part]
        return None

    # -- the index
    def to_usd(self, amount: float, currency: str) -> float | None:
        if currency == "USD":
            return amount
        holder = next((iso3 for iso3, cur in CURRENCY.items() if cur == currency and iso3 in self.countries), None)
        return amount / self.countries[holder]["fx"] if holder else None

    def index(self, pay: Pay | None, location: str, remote: bool = False) -> PayResult | None:
        if pay is None or pay.high <= 0:
            return None
        place = self.resolve(location, remote)
        if place is None:
            return None
        local = CURRENCY.get(place.country, "USD")
        currency = pay.currency or local
        if currency == "$":
            currency = local if local in _DOLLARS else "USD"
        lo, hi = pay.annual()
        lo_usd, hi_usd = self.to_usd(lo, currency), self.to_usd(hi, currency)
        if lo_usd is None or hi_usd is None:
            return None
        reference = self.reference_salary / self.home.level
        low, high = lo_usd / place.level / reference, hi_usd / place.level / reference
        return PayResult((low + high) / 2, low, high, Pay(pay.low, pay.high, currency, pay.period), place)


# ---------------------------------------------------------------------------- keeping the data current
# BEA publishes new RPPs each December; the World Bank revises PPPs and exchange rates a few times a
# year. The service checks the data's age daily and rebuilds it when it's older than pay.refresh_days.
# A rebuild only replaces the file after it passes these sanity checks, so a changed download format
# can't silently zero out the index.
MIN_METROS, MIN_STATES, MIN_COUNTRIES = 300, 50, 30


def check_data(data: dict, reference_location: str) -> None:
    """Raises ValueError if a freshly built table looks broken."""
    if len(data.get("us_metros") or {}) < MIN_METROS:
        raise ValueError(f"only {len(data.get('us_metros') or {})} US metro areas")
    if len(data.get("us_states") or {}) < MIN_STATES:
        raise ValueError(f"only {len(data.get('us_states') or {})} states")
    if len(data.get("countries") or {}) < MIN_COUNTRIES:
        raise ValueError(f"only {len(data.get('countries') or {})} countries")
    if not all(60 < v < 160 for v in data["us_metros"].values()):
        raise ValueError("a metro RPP is outside 60-160")
    if not all(0.1 < c["ppp"] / c["fx"] < 3 for c in data["countries"].values()):
        raise ValueError("a country price level is outside 0.1-3x US")
    PayIndex(data, reference_location=reference_location)  # the home location must still resolve


def data_age_days(data: dict) -> int:
    try:
        return (date.today() - date.fromisoformat(data.get("built", ""))).days
    except ValueError:
        return 10_000


def newest_data(*paths: Path) -> tuple[dict, Path] | None:
    """The most recently built of the given col_data.json files (the committed seed, the refreshed copy)."""
    found = []
    for p in paths:
        try:
            found.append((json.loads(p.read_text(encoding="utf-8")), p))
        except (OSError, ValueError):
            continue
    return max(found, key=lambda dp: dp[0].get("built", ""), default=None)


def write_data(data: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
