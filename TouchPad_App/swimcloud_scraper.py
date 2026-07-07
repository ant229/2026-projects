#!/usr/bin/env python3
"""
SwimCloud 200 Breaststroke Men Scraper
---------------------------------------
Collects top-times data for the 200 Breaststroke (SCY, Men) from SwimCloud's
internal JSON API and inserts them into a local PostgreSQL database.

SwimCloud is behind Cloudflare's bot-protection, which blocks stock Python
requests via TLS fingerprinting.  This script uses curl_cffi, a drop-in
requests-compatible library that impersonates Chrome's full TLS stack,
bypassing the challenge without requiring a browser window.

HTTP flow
  1. curl_cffi.requests.Session(impersonate="chrome124")
       – visit the human-readable HTML page, parse with BeautifulSoup
       – paginate the JSON API (/api/splashes/top_times/)

Fields extracted per swimmer
  name, team, time_seconds, graduation_year, event,
  pct_dropped, power_index, meet_date, seed_seconds

PostgreSQL tables created (if absent)
  schools  (id, name, division)
  swimmers (id, name, graduation_year, school_id, state, country)
  times    (id, swimmer_id, event, time_seconds, meet_date,
            power_index, pct_dropped, seed_seconds, meet_name)

Duplicates are silently handled via ON CONFLICT … DO UPDATE.

Records target
  REGION = "lsc_VA" yields 344 all-time VA records (7 pages) → well above 50.
  Set GRAD_YEAR_FILTER = 2026 to narrow to the class of 2026 (~20-30 rows).
  Set GRAD_YEAR_FILTER = None to collect all grad years (344 rows).

Install
  pip install curl_cffi beautifulsoup4 lxml psycopg2-binary
"""

import time
from typing import Optional
from curl_cffi import requests          # drop-in replacement for requests
from bs4 import BeautifulSoup
import psycopg2

# ── Configuration ─────────────────────────────────────────────────────────────

# SwimCloud region key. Options:
#   "lsc_VA"  – Virginia Swimming LSC          (344 all-time records)
#   "lsc_PV"  – Potomac Valley Swimming        (350 all-time records, incl. N. VA)
#   "country_USA" – all US swimmers            (756+ per season)
REGION = "lsc_VA"

# Event: stroke=3 (Breast), distance=200, course=1 (SCY yards)
EVENT_CODE  = "3|200|1"
GENDER      = "M"

# High-school graduation year filter (recruiting class of 2026).
# Set to None to collect all graduation years.
GRAD_YEAR_FILTER: Optional[int] = None  # 344 rows → guaranteed 50+
# GRAD_YEAR_FILTER = 2026            # ~20-30 rows if using lsc_VA

# Polite delay between API pages (seconds)
PAGE_DELAY = 1.5

DB_PARAMS = {
    "dbname":   "swimcloud",
    "user":     "postgres",   # adjust to your local PostgreSQL role
    "password": "",
    "host":     "localhost",
    "port":     5432,
}

BASE_URL  = "https://www.swimcloud.com"
API_URL   = f"{BASE_URL}/api/splashes/top_times/"
TIMES_URL = f"{BASE_URL}/times/"

# ── HTTP session (curl_cffi = requests-compatible + Chrome TLS) ───────────────

def build_session() -> requests.Session:
    """
    Return a curl_cffi Session that impersonates Chrome 124's TLS fingerprint.
    SwimCloud's Cloudflare WAF blocks plain Python requests via JA3/JA4 TLS
    fingerprinting; impersonating Chrome bypasses this for the JSON API.
    """
    session = requests.Session(impersonate="chrome124")
    session.headers.update({
        "Accept-Language": "en-US,en;q=0.9",
        "Referer":         TIMES_URL,
    })
    return session


# ── HTML fetch + BeautifulSoup parse ─────────────────────────────────────────

def fetch_html_page(session: requests.Session) -> BeautifulSoup:
    """
    Fetch the human-readable Top Times HTML page and parse it with
    BeautifulSoup.  If the results table is accessible (it may be behind CF on
    first visit), print column headers and sample rows.  Either way, return the
    soup object for any downstream use.
    """
    resp = session.get(
        TIMES_URL,
        params={
            "dont_group":   "false",
            "event":        EVENT_CODE,
            "event_course": "Y",
            "gender":       GENDER,
            "region":       REGION,
        },
        timeout=20,
    )
    soup = BeautifulSoup(resp.text, "lxml")

    page_title = soup.title.get_text(strip=True) if soup.title else "(none)"
    print(f"[HTML] status={resp.status_code}  title={page_title!r}")

    table = soup.find("table")
    if table:
        headers = [th.get_text(strip=True) for th in table.find_all("th")]
        print(f"[HTML] Column headers: {headers}")
        for row in table.find_all("tr")[1:4]:
            cells = [td.get_text(strip=True) for td in row.find_all("td")]
            if cells:
                print(f"[HTML]   {cells}")
    else:
        print(
            "[HTML] No results table in HTML response "
            "(Cloudflare challenge — data will come from JSON API)."
        )
    return soup


# ── JSON API helpers ──────────────────────────────────────────────────────────

def _api_params(page: int) -> dict:
    return {
        "dont_group":  "false",
        "event":       EVENT_CODE,
        "eventcourse": "Y",
        "gender":      GENDER,
        "region":      REGION,
        "page":        page,
    }


def fetch_api_page(session: requests.Session, page: int) -> dict:
    """Fetch one JSON page, retrying on 429 rate-limit responses."""
    for attempt in range(4):
        resp = session.get(API_URL, params=_api_params(page), timeout=20)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 429:
            wait = 15 * (attempt + 1)
            print(f"  [API] Rate limited — waiting {wait}s…")
            time.sleep(wait)
        else:
            print(f"  [API] HTTP {resp.status_code} on page {page}, "
                  f"attempt {attempt + 1}")
            time.sleep(5)
    return {}


# ── Field extraction helpers ──────────────────────────────────────────────────

def calc_pct_dropped(seed: Optional[str], evt_time: Optional[str]) -> Optional[float]:
    """Return the percentage improvement from seed to event time."""
    try:
        s, e = float(seed), float(evt_time)
        if s > 0:
            return round((s - e) / s * 100, 2)
    except (TypeError, ValueError):
        pass
    return None


# ── Database helpers ──────────────────────────────────────────────────────────

DDL = """
CREATE TABLE IF NOT EXISTS schools (
    id       SERIAL PRIMARY KEY,
    name     TEXT   NOT NULL,
    division TEXT,
    UNIQUE (name)
);

CREATE TABLE IF NOT EXISTS swimmers (
    id              INTEGER PRIMARY KEY,
    name            TEXT    NOT NULL,
    graduation_year INTEGER,
    school_id       INTEGER REFERENCES schools (id),
    state           TEXT,
    country         TEXT
);

CREATE TABLE IF NOT EXISTS times (
    id           INTEGER PRIMARY KEY,
    swimmer_id   INTEGER NOT NULL REFERENCES swimmers (id),
    event        TEXT    NOT NULL,
    time_seconds NUMERIC(8, 2) NOT NULL,
    meet_date    DATE,
    power_index  NUMERIC(6, 2),
    pct_dropped  NUMERIC(5, 2),
    seed_seconds NUMERIC(8, 2),
    meet_name    TEXT
);
"""


def init_db(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(DDL)
    conn.commit()
    print("[DB] Schema ready (schools / swimmers / times).")


def upsert_school(cur, name: str, division: Optional[str]) -> int:
    cur.execute(
        """
        INSERT INTO schools (name, division)
        VALUES (%s, %s)
        ON CONFLICT (name) DO UPDATE
            SET division = EXCLUDED.division
        RETURNING id
        """,
        (name or "Unknown", division),
    )
    return cur.fetchone()[0]


def upsert_swimmer(cur, sw: dict, school_id: int) -> None:
    cur.execute(
        """
        INSERT INTO swimmers
            (id, name, graduation_year, school_id, state, country)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (id) DO UPDATE
            SET name            = EXCLUDED.name,
                graduation_year = EXCLUDED.graduation_year,
                school_id       = EXCLUDED.school_id,
                state           = EXCLUDED.state,
                country         = EXCLUDED.country
        """,
        (
            sw["id"],
            sw.get("display_name") or sw.get("name", "Unknown"),
            sw.get("gradhs"),
            school_id,
            sw.get("state"),
            sw.get("country"),
        ),
    )


def upsert_time(cur, rec: dict, swimmer_id: int) -> None:
    evt   = rec.get("eventtime")
    seed  = rec.get("seedtime")
    power = rec.get("fina_points")
    cur.execute(
        """
        INSERT INTO times
            (id, swimmer_id, event, time_seconds, meet_date,
             power_index, pct_dropped, seed_seconds, meet_name)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (id) DO UPDATE
            SET time_seconds = EXCLUDED.time_seconds,
                meet_date    = EXCLUDED.meet_date,
                power_index  = EXCLUDED.power_index,
                pct_dropped  = EXCLUDED.pct_dropped,
                seed_seconds = EXCLUDED.seed_seconds,
                meet_name    = EXCLUDED.meet_name
        """,
        (
            rec["id"],
            swimmer_id,
            "200 Breast SCY",
            float(evt) if evt else None,
            rec.get("dateofswim"),
            float(power) if power else None,
            calc_pct_dropped(seed, evt),
            float(seed) if seed else None,
            rec.get("name"),               # meet name lives at top level
        ),
    )


# ── Main orchestration ────────────────────────────────────────────────────────

def scrape_and_load() -> None:
    session = build_session()

    # ── Step 1: fetch HTML page → BeautifulSoup ───────────────────────────────
    print("\n[Scraper] Fetching HTML overview page…")
    fetch_html_page(session)

    # ── Step 2: discover pagination via first JSON page ───────────────────────
    print("\n[Scraper] Fetching page 1 of JSON API…")
    first_page = fetch_api_page(session, 1)
    if not first_page:
        raise RuntimeError("Failed to fetch page 1 from SwimCloud API.")

    total      = first_page.get("count", 0)
    page_count = first_page.get("page_count", 0)
    print(
        f"[API] {total} total records across {page_count} pages "
        f"(region={REGION!r}, grad_year_filter={GRAD_YEAR_FILTER})"
    )

    # ── Step 3: connect to PostgreSQL, create schema ──────────────────────────
    conn = psycopg2.connect(**DB_PARAMS)
    init_db(conn)

    inserted = skipped = errors = 0

    # ── Step 4: paginate and insert ───────────────────────────────────────────
    for page_num in range(1, page_count + 1):
        if page_num > 1:
            time.sleep(PAGE_DELAY)
            page_data = fetch_api_page(session, page_num)
        else:
            page_data = first_page

        results = page_data.get("results", [])
        page_inserted = 0

        for rec in results:
            swimmer = rec.get("swimmer") or {}
            team    = rec.get("team")    or {}

            # Optional class-of-2026 filter
            if GRAD_YEAR_FILTER is not None:
                if swimmer.get("gradhs") != GRAD_YEAR_FILTER:
                    continue

            evt_time = rec.get("eventtime")
            if not evt_time:
                skipped += 1
                continue

            try:
                with conn.cursor() as cur:
                    school_id = upsert_school(
                        cur,
                        team.get("name"),
                        str(team.get("orgcode", "")) if team else None,
                    )
                    upsert_swimmer(cur, swimmer, school_id)
                    upsert_time(cur, rec, swimmer["id"])
                conn.commit()
                page_inserted += 1
                inserted += 1
            except psycopg2.Error as exc:
                conn.rollback()
                print(f"\n  [DB] Error on record {rec.get('id')}: {exc}")
                errors += 1

        print(
            f"[Page {page_num:2d}/{page_count}] "
            f"{len(results)} API records  →  {page_inserted} inserted/updated"
        )

    conn.close()

    print(
        f"\n[Done] inserted/updated={inserted}, "
        f"skipped(no-time)={skipped}, db_errors={errors}"
    )
    if GRAD_YEAR_FILTER is not None and inserted < 50:
        print(
            "  Tip: set GRAD_YEAR_FILTER = None to collect all Virginia LSC "
            f"swimmers ({total} records) instead of only class of {GRAD_YEAR_FILTER}."
        )


if __name__ == "__main__":
    scrape_and_load()
