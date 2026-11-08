"""Cost-of-living pay index: what a base salary buys where the job is, relative to a reference.

    index = (take-home pay / local price level) / (reference take-home pay / home price level)

Take-home pay is the salary in US dollars after income and payroll taxes where the job is (see
taxes.py; pay.taxes: false compares gross pay instead). With the defaults, $155K base in Portland
is 1.0; $155K somewhere with Portland's prices but no state income tax is about 1.10.

Price levels are all relative to the US average (= 1.0) and come from `col_data.json`, which
`jobalerts pay --update` builds from free, official sources:

- US metro areas and states: BEA Regional Price Parities (RPP). One "all items" number per metro
  area or state, plus its parts (goods, housing rents, utilities, other services), from the CPI and
  the Census's American Community Survey rents. Released each December for the year before last.
- US metro rents for a family: HUD Fair Market Rents (FMR) for a 4-bedroom home, rent plus
  utilities, set each October for the coming fiscal year from what recent movers pay.
- Other countries: World Bank purchasing-power parity for household consumption (PA.NUS.PRVT.PP)
  divided by the official exchange rate (PA.NUS.FCRF). That ratio is the country's price level
  against the US. Country-wide only: there is no free official city-level series outside the US.

Family housing (pay.housing_share, 0 = off): the all-items RPP weighs housing at about 17% of
spending and prices it at what all renters pay, long-time tenants included. A family that moves
pays today's rent for a bigger home, and rent is a larger share of what it spends. So a US place's
price level is instead

    (1 - housing_share) x RPP goods and other services  +  housing_share x family rent level

where the family rent level is the metro's 4-bedroom FMR, scaled onto the RPP's index (US = 100) by
the median ratio of RPP housing + utilities to 4-bedroom FMR across metros. Places without an FMR
(states) use RPP housing + utilities. The weights that combine the RPP parts are fitted to the
all-items RPP when the data is built. Countries abroad keep the World Bank price level.

Renting or buying (pay.buy): every US place gets a second price level for a family that buys. The
housing share is then priced at the monthly cost of owning a typical 4-bedroom home there instead
of the 4-bedroom FMR, on the same dollar scale:

    own cost / month = (loan x mortgage rate + price x (property tax + upkeep)) / 12 + utilities

with the price from Zillow's 4-bedroom Home Value Index (metro, else state), the loan the price less
the down payment, and the 30-year rate from Freddie Mac's weekly survey (PMMS). Paying
down principal is saving, not cost, so it's left out; so are the mortgage-interest deduction and
what the down payment would have earned. The buy index compares buying there with buying at the
reference location at today's prices and rates.

Benefits, bonus and equity are not included; this is base pay against prices.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import httpx

from .taxes import Taxes, TakeHome

log = logging.getLogger(__name__)

BEA_URLS = {"msa": "https://apps.bea.gov/regional/zip/MARPP.zip", "state": "https://apps.bea.gov/regional/zip/SARPP.zip"}
BEA_PARTS = {"2": "goods", "3": "housing", "4": "utilities", "5": "other"}  # RPP LineCode -> part
# HUD's public ArcGIS layer of Fair Market Rents (no key needed; the huduser.gov downloads sit behind a bot check).
FMR_URL = "https://services.arcgis.com/VTyQ9soqVukalItT/arcgis/rest/services/Fair_Market_Rents/FeatureServer/0"
WB_URL = "https://api.worldbank.org/v2/country/all/indicator/{}?format=json&date={}:{}&per_page=20000"
WB_PPP, WB_FX = "PA.NUS.PRVT.PP", "PA.NUS.FCRF"
# Zillow Home Value Index, 4 bedrooms, middle tier (free research data; attribution to Zillow required).
ZILLOW_URLS = {
    "msa": "https://files.zillowstatic.com/research/public_csvs/zhvi/Metro_zhvi_bdrmcnt_4_uc_sfrcondo_tier_0.33_0.67_sm_sa_month.csv",
    "state": "https://files.zillowstatic.com/research/public_csvs/zhvi/State_zhvi_bdrmcnt_4_uc_sfrcondo_tier_0.33_0.67_sm_sa_month.csv",
}
PMMS_URL = "https://www.freddiemac.com/pmms/docs/PMMS_history.csv"  # Freddie Mac's weekly mortgage survey

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


# pay_note of a scored job with no pay index: the AI found no pay, or the place isn't known.
NO_PAY = "no pay index: pay not stated, or the place isn't known"


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
class Ownership:
    """What buying a home costs besides the price (pay.buy in config.yaml)."""
    down_payment: float = 0.20               # share of the price
    property_tax: float = 0.011              # a year, share of the price, where the state isn't listed
    property_tax_by_state: dict[str, float] = field(default_factory=dict)
    upkeep: float = 0.0075                   # insurance and maintenance a year, share of the price
    utilities: float = 350.0                 # $/month at US-average utility prices (scaled by the RPP's)

    def monthly(self, price: float, rate_pct: float, state: str, utilities_rpp: float = 100.0) -> float:
        tax = self.property_tax_by_state.get(state, self.property_tax)
        yearly = price * (1 - self.down_payment) * rate_pct / 100 + price * (tax + self.upkeep)
        return yearly / 12 + self.utilities * utilities_rpp / 100


@dataclass
class Place:
    label: str
    level: float          # price level, US average = 1.0 (renting, with family housing on)
    country: str = "USA"  # ISO3
    state: str = ""       # US state code, when known (for state and local taxes)
    city: str = ""        # lower case, when known (for city taxes)
    buy_level: float | None = None  # price level for a family that buys a 4-bedroom home (US places only)
    buy_note: str = ""    # "own $1.87M 4-bed: $12.4K/mo, $374K down"


def _money(v: float) -> str:
    return f"${v / 1e6:.2f}M" if v >= 1e6 else f"${v / 1000:.0f}K" if v >= 10_000 else f"${v / 1000:.1f}K"


@dataclass
class PayResult:
    index: float          # midpoint of the range (renting)
    low: float
    high: float
    pay: Pay
    place: Place
    buy_low: float | None = None   # the same with the family buying (None: no home prices for the place)
    buy_high: float | None = None

    @property
    def buy_index(self) -> float | None:
        return None if self.buy_low is None else (self.buy_low + self.buy_high) / 2

    @property
    def best_high(self) -> float:
        """Top of the range, renting or buying, whichever is higher: what pay.min_index is checked against."""
        return max(self.high, self.buy_high or 0.0)

    def text(self) -> str:
        """Short form for phone alerts and listings."""
        def spread(lo: float, hi: float) -> str:
            return f"{lo:.2f}-{hi:.2f}" if round(lo, 2) != round(hi, 2) else ""
        if self.buy_low is None:
            s = spread(self.low, self.high)
            return f"pay index {self.index:.2f}{f' ({s})' if s else ''}: {self.pay.text()} in {self.place.label}"
        ranges = ", ".join(f"{k} {v}" for k, v in (("rent", spread(self.low, self.high)),
                                                    ("buy", spread(self.buy_low, self.buy_high))) if v)
        return (f"pay index {self.index:.2f} rent · {self.buy_index:.2f} buy{f' ({ranges})' if ranges else ''}: "
                f"{self.pay.text()} in {self.place.label}; {self.place.buy_note}")


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
def _bea_rows(blob: bytes, prefix: str) -> tuple[int, list[tuple[str, str, float]], dict[str, dict[str, float]]]:
    """(latest year, [(fips, name, all-items RPP)], {fips: {part: RPP}}) from a BEA regional zip.
    LineCode 1 is all items; 2-5 are the parts in BEA_PARTS."""
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        name = next(n for n in z.namelist() if n.startswith(prefix) and n.endswith(".csv"))
        reader = csv.reader(io.StringIO(z.read(name).decode("utf-8-sig", errors="replace")))
        header = next(reader)
        years = [i for i, h in enumerate(header) if h.strip().isdigit()]
        latest, rows, parts = 0, [], {}
        for r in reader:
            line = r[4].strip() if len(r) > years[-1] else ""
            if line != "1" and line not in BEA_PARTS:
                continue
            for i in reversed(years):
                try:
                    value = float(r[i])
                except ValueError:
                    continue
                fips = r[0].strip().strip('"')
                if line == "1":
                    latest = max(latest, int(header[i]))
                    rows.append((fips, r[1].strip(), value))
                else:
                    parts.setdefault(fips, {})[BEA_PARTS[line]] = value
                break
        return latest, rows, parts


async def _fmr_rows(client: httpx.AsyncClient) -> tuple[str, list[dict]]:
    """(date the layer was last updated, [{FMR_CODE, FMR_AREANAME, FMR_4BDR}]) for HUD's metro FMR areas."""
    r = await client.get(FMR_URL, params={"f": "json"})
    r.raise_for_status()
    stamp = (r.json().get("editingInfo") or {}).get("lastEditDate")
    updated = date.fromtimestamp(stamp / 1000).isoformat() if stamp else "unknown"
    rows: list[dict] = []
    while True:
        r = await client.get(f"{FMR_URL}/query", params={
            "where": "FMR_CODE LIKE 'METRO%'", "outFields": "FMR_CODE,FMR_AREANAME,FMR_4BDR",
            "returnGeometry": "false", "orderByFields": "OBJECTID", "resultOffset": len(rows),
            "resultRecordCount": 1000, "f": "json"})
        r.raise_for_status()
        page = r.json()
        if "error" in page:
            raise ValueError(f"HUD FMR query failed: {page['error']}")
        rows += [f["attributes"] for f in page.get("features") or []]
        if not page.get("features") or not page.get("exceededTransferLimit"):
            return updated, rows


def _metro_fmr(cbsa: str, metro: str, areas: list[dict]) -> int | None:
    """4-bedroom FMR for a BEA metro. HUD splits some metros into several FMR areas; take the one
    coded for the whole metro ("METRO41940M41940"), else the one named after its first city."""
    if not areas:
        return None
    first = re.split(r"[-,]", metro)[0].strip().lower()
    pick = (next((a for a in areas if a["FMR_CODE"].endswith(f"M{cbsa}")), None)
            or next((a for a in areas if first in a["FMR_AREANAME"].lower()), None) or areas[0])
    return pick.get("FMR_4BDR") or None


def _fit_weights(parts: list[dict[str, float]], totals: list[float]) -> dict[str, float]:
    """Least-squares weights that best rebuild the all-items RPP from its parts, clipped at zero and
    summing to 1. (BEA doesn't publish the expenditure weights it combines the parts with.)"""
    names = list(BEA_PARTS.values())
    n = len(names)
    # normal equations A w = b, solved by Gaussian elimination
    a = [[sum(p[i] * p[j] for p in parts) for j in names] for i in names]
    b = [sum(p[i] * t for p, t in zip(parts, totals)) for i in names]
    for c in range(n):
        pivot = max(range(c, n), key=lambda r: abs(a[r][c]))
        a[c], a[pivot], b[c], b[pivot] = a[pivot], a[c], b[pivot], b[c]
        for r in range(c + 1, n):
            f = a[r][c] / a[c][c]
            a[r] = [x - f * y for x, y in zip(a[r], a[c])]
            b[r] -= f * b[c]
    w = [0.0] * n
    for c in reversed(range(n)):
        w[c] = (b[c] - sum(a[c][k] * w[k] for k in range(c + 1, n))) / a[c][c]
    w = [max(x, 0.0) for x in w]
    return {name: round(x / sum(w), 4) for name, x in zip(names, w)}


def _housing_rpp(parts: dict[str, float], weights: dict[str, float]) -> float:
    """RPP housing and utilities together (an FMR is rent plus utilities)."""
    wh, wu = weights["housing"], weights["utilities"]
    return (wh * parts["housing"] + wu * parts["utilities"]) / (wh + wu)


def family_level(parts: dict[str, float], weights: dict[str, float], fmr_scale: float, share: float) -> float:
    """Price level (US average = 1.0) with housing at `share` of spending; see the module docstring."""
    wg, wo = weights["goods"], weights["other"]
    other = (wg * parts["goods"] + wo * parts["other"]) / (wg + wo)
    housing = parts["fmr4"] * fmr_scale if parts.get("fmr4") else _housing_rpp(parts, weights)
    return ((1 - share) * other + share * housing) / 100


def own_level(parts: dict[str, float], weights: dict[str, float], fmr_scale: float, share: float,
              own_monthly: float) -> float:
    """Price level with housing at `share` of spending priced at the monthly cost of owning, on the
    same dollar-to-index scale as the FMR."""
    return family_level(dict(parts, fmr4=own_monthly), weights, fmr_scale, share)


def _zillow_rows(text: str) -> tuple[str, list[tuple[str, str, float]]]:
    """(latest month, [(region name, state, value)]) from a Zillow ZHVI CSV, latest value per region."""
    reader = csv.DictReader(io.StringIO(text))
    months = [h for h in reader.fieldnames or [] if re.fullmatch(r"\d{4}-\d{2}-\d{2}", h)]
    if not months:
        raise ValueError("Zillow CSV has no monthly columns")
    rows, latest = [], ""
    for r in reader:
        month = next((m for m in reversed(months) if r.get(m)), None)
        if month:
            latest = max(latest, month)
            rows.append((r["RegionName"], r.get("StateName") or "", float(r[month])))
    return latest[:7], rows


def _metro_home(metro: str, homes: dict[tuple[str, str], float]) -> float | None:
    """Zillow names a metro by its first city ("San Jose, CA"); BEA lists the principal cities. Try each."""
    cities, _, states = re.sub(r"\s*\*$|\s*Metropolitan Statistical Area$", "", metro).rpartition(", ")
    for city in re.split(r"[-/]", cities):
        for st in states.split("-"):
            if (v := homes.get((city.strip().lower(), st.strip()))) is not None:
                return v
    return None


def _pmms_rate(text: str) -> tuple[str, float]:
    """(ISO date, percent) of the latest 30-year fixed rate in Freddie Mac's PMMS history CSV."""
    latest = None
    for r in csv.DictReader(io.StringIO(text)):
        try:
            month, day, year = (int(x) for x in r["date"].split("/"))
            rate = float(r["pmms30"])
        except (KeyError, ValueError, AttributeError):
            continue
        when = date(year, month, day)
        if latest is None or when > latest[0]:
            latest = (when, rate)
    if latest is None:
        raise ValueError("no 30-year rate in the Freddie Mac PMMS CSV")
    return latest[0].isoformat(), latest[1]


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
        fmr_updated, fmr_rows = await _fmr_rows(client)
        zillow = {}
        for key, url in ZILLOW_URLS.items():
            r = await client.get(url)
            r.raise_for_status()
            zillow[key] = _zillow_rows(r.text)
        r = await client.get(PMMS_URL)
        r.raise_for_status()
        rate_day, rate = _pmms_rate(r.text)
    finally:
        if own:
            await client.aclose()

    msa_year, msa_rows, msa_parts = _bea_rows(blobs["msa"], "MARPP")
    state_year, state_rows, state_parts = _bea_rows(blobs["state"], "SARPP")
    metros, metro_parts = {}, {}
    fmr_areas: dict[str, list[dict]] = {}
    for row in fmr_rows:
        if m := re.match(r"METRO(\d{5})", row.get("FMR_CODE") or ""):
            fmr_areas.setdefault(m.group(1), []).append(row)
    zillow_month, zillow_metros = zillow["msa"]
    homes = {}
    for region, _, value in zillow_metros:
        city, _, st = region.rpartition(", ")
        homes[(city.lower(), st)] = value
    for fips, n, v in msa_rows:
        if fips in ("00000", "00999"):
            continue
        name = re.sub(r"\s*\(Metropolitan Statistical Area\)", "", n)
        metros[name] = v
        if set(BEA_PARTS.values()) <= set(msa_parts.get(fips, {})):
            metro_parts[name] = dict(msa_parts[fips])
            if fmr := _metro_fmr(fips, name, fmr_areas.get(fips, [])):
                metro_parts[name]["fmr4"] = fmr
            if home := _metro_home(name, homes):
                metro_parts[name]["home4"] = round(home)
    nonmetro = next((v for fips, _, v in msa_rows if fips == "00999"), None)
    states, state_part_map = {}, {}
    for fips, n, v in state_rows:
        if n.lower() in _STATE_BY_NAME:
            st = _STATE_BY_NAME[n.lower()]
            states[st] = v
            if set(BEA_PARTS.values()) <= set(state_parts.get(fips, {})):
                state_part_map[st] = dict(state_parts[fips])
    state_homes = {_STATE_BY_NAME[name.lower()]: v for name, _, v in zillow["state"][1] if name.lower() in _STATE_BY_NAME}
    for st, value in state_homes.items():
        if st in state_part_map:
            state_part_map[st]["home4"] = round(value)
    fitted = [(metro_parts[k], metros[k]) for k in metro_parts]
    weights = _fit_weights([p for p, _ in fitted], [t for _, t in fitted]) if len(fitted) >= 10 else {}
    ratios = sorted(_housing_rpp(p, weights) / p["fmr4"] for p, _ in fitted if p.get("fmr4")) if weights else []
    fmr_scale = ratios[len(ratios) // 2] if ratios else 0.0
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
            "housing": f"HUD Fair Market Rents, 4 bedrooms, metro areas (layer updated {fmr_updated})",
            "homes": f"Zillow Home Value Index (ZHVI), 4 bedrooms, metro and state, {zillow_month}; "
                     f"data by Zillow Group",
            "mortgage": f"Freddie Mac PMMS 30-year fixed rate, week of {rate_day}",
        },
        "mortgage_rate": rate,
        "us_metros": metros, "us_nonmetro": nonmetro, "us_states": states, "countries": countries,
        "us_metro_parts": metro_parts, "us_state_parts": state_part_map,
        "rpp_weights": weights, "fmr_scale": round(fmr_scale, 6),
    }


# ---------------------------------------------------------------------------- the index
class PayIndex:
    def __init__(self, data: dict, reference_salary: float = 155_000, reference_location: str = "Portland, OR",
                 overrides: dict[str, str] | None = None, taxes: Taxes | None = None, housing_share: float = 0.0,
                 ownership: Ownership | None = None):
        self.data = data
        self.taxes = taxes
        self.metros: dict[str, float] = data["us_metros"]
        self.states: dict[str, float] = data["us_states"]
        self.countries: dict[str, dict] = data["countries"]
        # Family housing needs the RPP parts and fitted weights (tables built before 2026-11 lack them).
        self.housing_share = housing_share if housing_share > 0 and data.get("rpp_weights") else 0.0
        # Buying needs family housing on and home prices plus a mortgage rate (tables built before 2026-11-08 lack them).
        self.ownership = ownership if self.housing_share and data.get("mortgage_rate") else None
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
        if self.ownership and home.buy_level is None:
            log.warning("Buy index off: no home price for %s", home.label)
            self.ownership = None

    @classmethod
    def load(cls, path: Path, **kwargs) -> PayIndex:
        return cls(json.loads(path.read_text(encoding="utf-8")), **kwargs)

    # -- places
    def _level(self, rpp: float, parts: dict | None) -> tuple[float, str]:
        """(price level, how it was worked out) for a US metro or state."""
        if not self.housing_share or not parts:
            return rpp / 100, f"RPP {rpp:.1f}"
        level = family_level(parts, self.data["rpp_weights"], self.data["fmr_scale"], self.housing_share)
        rent = f"4-bedroom FMR ${parts['fmr4']:,}" if parts.get("fmr4") else "RPP rents"
        return level, f"RPP {rpp:.1f}; family {level * 100:.1f} with {self.housing_share:.0%} housing at {rent}"

    def own_monthly(self, parts: dict | None, state: str) -> float | None:
        """Monthly cost of owning the place's typical 4-bedroom home; None without home prices."""
        if not self.ownership or not parts or not parts.get("home4"):
            return None
        return self.ownership.monthly(parts["home4"], self.data["mortgage_rate"], state, parts.get("utilities", 100.0))

    def _buy(self, parts: dict | None, state: str) -> tuple[float | None, str]:
        """(price level when buying, note) for a US metro or state."""
        own = self.own_monthly(parts, state)
        if own is None:
            return None, ""
        level = own_level(parts, self.data["rpp_weights"], self.data["fmr_scale"], self.housing_share, own)
        price = parts["home4"]
        return level, (f"own {_money(price)} 4-bed at {self.data['mortgage_rate']:.2f}%: "
                       f"{_money(own)}/mo, {_money(price * self.ownership.down_payment)} down")

    def _metro(self, name: str, state: str = "", city: str = "") -> Place:
        state = state or name.rpartition(", ")[2].split("-")[0]  # the metro's first state
        parts = (self.data.get("us_metro_parts") or {}).get(name)
        level, how = self._level(self.metros[name], parts)
        state_home = ((self.data.get("us_state_parts") or {}).get(state) or {}).get("home4")
        if parts and not parts.get("home4") and state_home:  # Zillow doesn't list every metro: the state's value
            parts = dict(parts, home4=state_home)
        buy, note = self._buy(parts, state)
        return Place(f"{name} metro ({how})", level, "USA", state, city, buy, note)

    def _state(self, st: str, city: str = "") -> Place | None:
        if st not in self.states:
            return None
        parts = (self.data.get("us_state_parts") or {}).get(st)
        level, how = self._level(self.states[st], parts)
        buy, note = self._buy(parts, st)
        return Place(f"{STATES[st]} (state {how})", level, "USA", st, city, buy, note)

    def _country(self, iso3: str) -> Place | None:
        if iso3 == "USA":
            return Place("US average", 1.0)
        c = self.countries.get(iso3)
        if not c:
            return None
        level = c["ppp"] / c["fx"]
        return Place(f"{iso3} (price level {level:.2f}x US)", level, iso3)

    def _override(self, text: str) -> Place | None:
        """pay.locations: whole words of the location -> "home", a metro name, a state code, an ISO3
        country, or any place the resolver understands ("Palo Alto, CA")."""
        for needle, target in self.overrides.items():
            if re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", text):
                if target.lower() == "home":
                    return self.home
                if target in self.metros:
                    return self._metro(target)
                return (self._state(target) or self._country(target.upper())
                        or self.resolve(target, use_overrides=False))
        return None

    def resolve(self, location: str, remote: bool = False, use_overrides: bool = True) -> Place | None:
        """Best price level for a free-text location. Remote jobs take the home price level unless the
        location names a foreign country (a remote job in Germany means living in Germany)."""
        text = location.lower()
        if use_overrides and (place := self._override(text)):
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
        pairs = [(re.sub(r"^.*\b(?:hybrid|onsite|on-site|usa?)\b[\s\-:]*", "", c.strip(), flags=re.I).lower(), st)
                 for c, st in pairs]
        for city, st in pairs:
            if name := self._cities.get((SUBURBS.get((city, st), city), st)):
                return self._metro(name, st, city)
        if foreign and foreign != "USA":
            return self._country(foreign)
        for city, st in pairs:
            if place := self._state(st, city):
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
    def take_home(self, gross_usd: float, place: Place) -> TakeHome | None:
        """Pay after taxes where the place is; None with taxes off."""
        if not self.taxes:
            return None
        return self.taxes.take_home(gross_usd, place.country, place.state, place.city)

    def _net(self, gross_usd: float, place: Place) -> float:
        th = self.take_home(gross_usd, place)
        return th.net if th else gross_usd

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
        ref_net, net_lo, net_hi = self._net(self.reference_salary, self.home), self._net(lo_usd, place), self._net(hi_usd, place)
        reference = ref_net / self.home.level
        low, high = net_lo / place.level / reference, net_hi / place.level / reference
        buy_low = buy_high = None
        if self.ownership and place.buy_level:
            buy_ref = ref_net / self.home.buy_level
            buy_low, buy_high = net_lo / place.buy_level / buy_ref, net_hi / place.buy_level / buy_ref
        return PayResult((low + high) / 2, low, high, Pay(pay.low, pay.high, currency, pay.period), place,
                         buy_low, buy_high)


# ---------------------------------------------------------------------------- keeping the data current
# BEA publishes new RPPs each December; the World Bank revises PPPs and exchange rates a few times a
# year. The service checks the data's age daily and rebuilds it when it's older than pay.refresh_days.
# A rebuild only replaces the file after it passes these sanity checks, so a changed download format
# can't silently zero out the index.
MIN_METROS, MIN_STATES, MIN_COUNTRIES = 300, 50, 30


def check_data(data: dict, reference_location: str, housing_share: float = 0.4, buy: bool = False) -> None:
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
    if housing_share:
        parts, weights = data.get("us_metro_parts") or {}, data.get("rpp_weights") or {}
        if sum(1 for p in parts.values() if p.get("fmr4")) < MIN_METROS:
            raise ValueError(f"only {sum(1 for p in parts.values() if p.get('fmr4'))} metros with a HUD Fair Market Rent")
        if set(weights) != set(BEA_PARTS.values()) or not data.get("fmr_scale"):
            raise ValueError("no RPP part weights or FMR scale")
        levels = [family_level(p, weights, data["fmr_scale"], housing_share) for p in parts.values()]
        if not all(0.5 < v < 2.5 for v in levels):
            raise ValueError("a family price level is outside 0.5-2.5")
    if buy and housing_share:
        homes = [p["home4"] for p in (data.get("us_metro_parts") or {}).values() if p.get("home4")]
        if len(homes) < MIN_METROS:
            raise ValueError(f"only {len(homes)} metros with a Zillow home value")
        if not all(50_000 < v < 10_000_000 for v in homes):
            raise ValueError("a metro home value is outside $50K-10M")
        if not 1 < (data.get("mortgage_rate") or 0) < 20:
            raise ValueError(f"mortgage rate {data.get('mortgage_rate')} is outside 1-20%")
    PayIndex(data, reference_location=reference_location, housing_share=housing_share)  # home must still resolve


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
