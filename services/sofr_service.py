from __future__ import annotations

import csv
import io
from datetime import date, timedelta
from typing import List

import requests

NEW_YORK_FED_SOFR_URL = (
    "https://markets.newyorkfed.org/api/rates/secured/sofr/search.json"
    "?startDate={start}&endDate={end}&type=rate"
)
FRED_SOFR_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=SOFR"


def _parse_rate(raw_value: str | None) -> float | None:
    if raw_value is None:
        return None
    cleaned = raw_value.strip().replace('%', '').replace(',', '')
    if not cleaned or cleaned.lower() in {'null', 'n/a', 'nan'}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def fetch_historical_rates(start_date: date, end_date: date) -> List[dict]:
    headers = {'User-Agent': 'ARRA/1.0'}

    try:
        response = requests.get(
            NEW_YORK_FED_SOFR_URL.format(start=start_date.isoformat(), end=end_date.isoformat()),
            timeout=15,
            headers=headers,
        )
        response.raise_for_status()
        rows = []
        for item in response.json().get('refRates', []):
            try:
                observation_day = date.fromisoformat(item['effectiveDate'])
            except (KeyError, TypeError, ValueError):
                continue
            rate = _parse_rate(str(item.get('percentRate')))
            if rate is None or not start_date <= observation_day <= end_date:
                continue
            rows.append({
                'date': observation_day.isoformat(),
                'rate': rate,
                'source': 'New York Fed',
            })
        rows.sort(key=lambda item: item['date'])
        if rows:
            return rows
    except (requests.RequestException, ValueError):
        pass

    response = requests.get(FRED_SOFR_URL, timeout=15, headers=headers)
    response.raise_for_status()
    rows = []
    reader = csv.DictReader(io.StringIO(response.text))
    for row in reader:
        record_date = row.get('observation_date') or row.get('DATE')
        if not record_date:
            continue
        try:
            observation_day = date.fromisoformat(record_date)
        except ValueError:
            continue
        if observation_day < start_date or observation_day > end_date:
            continue
        rate = _parse_rate(row.get('SOFR') or row.get('VALUE') or row.get('Value'))
        if rate is None:
            continue
        rows.append({
            'date': observation_day.isoformat(),
            'rate': rate,
            'source': 'New York Fed / FRED',
        })
    rows.sort(key=lambda item: item['date'])
    return rows


def fetch_latest_rate(as_of_date: date | None = None) -> dict:
    anchor_date = as_of_date or date.today()
    recent_start = max(date(2000, 1, 1), anchor_date - timedelta(days=3650))
    history = fetch_historical_rates(recent_start, anchor_date)
    if not history:
        raise ValueError('No SOFR observations were available for the requested date range.')
    return history[-1]
