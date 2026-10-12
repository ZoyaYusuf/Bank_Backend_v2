from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import List
from urllib.parse import urlencode

import requests

SONIA_URL = (
    "https://www.bankofengland.co.uk/boeapps/database/"
    "_iadb-fromshowcolumns.asp"
)
SONIA_SERIES_CODE = 'IUDSOIA'

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


def _try_candidate_urls(start_date: date, end_date: date) -> List[dict]:
    query = urlencode({
        'csv.x': 'yes',
        'Datefrom': start_date.strftime('%d/%b/%Y'),
        'Dateto': end_date.strftime('%d/%b/%Y'),
        'SeriesCodes': SONIA_SERIES_CODE,
        'CSVF': 'TN',
        'UsingCodes': 'Y',
        'VPD': 'Y',
        'VFD': 'N',
    })
    response = requests.get(
        f'{SONIA_URL}?{query}',
        timeout=30,
        headers={
            'User-Agent': (
                'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            )
        },
    )
    response.raise_for_status()

    text = response.text.strip()
    if not text or 'DATE' not in text.upper() or '<html' in text.lower():
        return []

    data = []
    lines = text.splitlines()
    for line in lines[1:]:
        columns = [column.strip().strip('"') for column in line.split(',')]
        if len(columns) < 2:
            continue
        try:
            observation_day = date.fromisoformat(columns[0])
        except ValueError:
            try:
                observation_day = datetime.strptime(columns[0], '%d %b %Y').date()
            except ValueError:
                continue
        if observation_day < start_date or observation_day > end_date:
            continue
        rate = _parse_rate(columns[1])
        if rate is not None:
            data.append({
                'date': observation_day.isoformat(),
                'rate': rate,
                'source': 'Bank of England',
            })
    data.sort(key=lambda item: item['date'])
    return data


def fetch_historical_rates(start_date: date, end_date: date) -> List[dict]:
    records = _try_candidate_urls(start_date, end_date)
    if not records:
        raise ValueError('SONIA data could not be retrieved from the official Bank of England source for the requested period.')
    return records


def fetch_latest_rate(as_of_date: date | None = None) -> dict:
    anchor_date = as_of_date or date.today()
    recent_start = max(date(2000, 1, 1), anchor_date - timedelta(days=3650))
    history = fetch_historical_rates(recent_start, anchor_date)
    if not history:
        raise ValueError('No SONIA observations were available for the requested date range.')
    return history[-1]
