from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import List

from services.sonia_service import fetch_historical_rates as fetch_sonia_rates
from services.sofr_service import fetch_historical_rates as fetch_sofr_rates
from services.chart_service import generate_rate_chart

TWO_PLACES = Decimal('0.01')


def _as_decimal(value) -> Decimal:
    return Decimal(str(value))


def _day_count_denominator(day_count: str) -> int:
    if day_count.upper() == 'ACT/365':
        return 365
    return 360


def _resolve_rate_for_day(rate_history: List[dict], day: date) -> dict | None:
    applicable = None
    for entry in rate_history:
        entry_date = date.fromisoformat(entry['date'])
        if entry_date <= day:
            applicable = entry
        else:
            break
    return applicable


def _date_range(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def _round_money(value: Decimal) -> Decimal:
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def _build_rate_history(rate_history: List[dict], start_date: date, end_date: date) -> List[dict]:
    record_map = {date.fromisoformat(item['date']): item for item in rate_history}
    summary = []
    for day in _date_range(start_date, end_date):
        item = record_map.get(day)
        if item is not None:
            summary.append({
                'date': day.isoformat(),
                'reference_rate': item['rate'],
                'source': item.get('source', 'Official rate source')
            })
    return summary


def _fetch_reference_rates(reference_rate: str, start_date: date, end_date: date) -> List[dict]:
    if reference_rate.upper() == 'SOFR':
        return fetch_sofr_rates(start_date, end_date)
    if reference_rate.upper() == 'SONIA':
        return fetch_sonia_rates(start_date, end_date)
    raise ValueError(f'Reference rate {reference_rate} is not supported. Use SOFR or SONIA.')


def _attach_rate_chart(result: dict, rate_history: List[dict], reference_rate: str,
                       start_date: date, end_date: date) -> dict:
    try:
        print(f"Generating rate chart for {reference_rate} from {start_date} to {end_date} with {len(rate_history)} observations.")
        result['rate_chart'] = generate_rate_chart(
            rate_history,
            reference_rate,
            start_date.isoformat(),
            end_date.isoformat(),
        )
    except Exception as exc:
        import traceback

        print("RATE CHART ERROR:")
        traceback.print_exc()
        
        result['rate_chart'] = None
        result['rate_chart_error'] = str(exc)
    return result


def calculate_historical_loan(principal: float, reference_rate: str, spread: float, start_date: str,
                             end_date: str, day_count: str = 'ACT/360') -> dict:
    loan_start = date.fromisoformat(start_date)
    loan_end = date.fromisoformat(end_date)
    if loan_end < loan_start:
        raise ValueError('End date must be on or after the start date.')

    rate_history = _fetch_reference_rates(reference_rate, loan_start, loan_end)
    principal_decimal = _as_decimal(principal)
    spread_decimal = _as_decimal(spread)
    denominator = Decimal(_day_count_denominator(day_count))
    total_interest = Decimal('0')
    daily_breakdown = []

    for day in _date_range(loan_start, loan_end):
        applicable = _resolve_rate_for_day(rate_history, day)
        if applicable is None:
            continue
        reference_rate_value = _as_decimal(applicable['rate'])
        effective_rate = reference_rate_value + spread_decimal
        daily_interest = principal_decimal * (effective_rate / Decimal('100')) * (Decimal('1') / denominator)
        total_interest += daily_interest
        daily_breakdown.append({
            'date': day.isoformat(),
            'reference_rate': float(reference_rate_value),
            'effective_rate': float(effective_rate),
            'daily_interest': float(_round_money(daily_interest)),
            'source': applicable.get('source', 'Official rate source')
        })

    total_repayment = principal_decimal + total_interest
    average_reference_rate = Decimal('0')
    latest_reference_rate = Decimal('0')
    if daily_breakdown:
        average_reference_rate = sum(_as_decimal(item['reference_rate']) for item in daily_breakdown) / Decimal(len(daily_breakdown))
        latest_reference_rate = _as_decimal(daily_breakdown[-1]['reference_rate'])

    average_effective_rate = average_reference_rate + spread_decimal
    latest_effective_rate = latest_reference_rate + spread_decimal

    result = {
        'loan_status': 'HISTORICAL',
        'principal': float(_round_money(principal_decimal)),
        'total_interest': float(_round_money(total_interest)),
        'total_repayment': float(_round_money(total_repayment)),
        'rate_history': _build_rate_history(rate_history, loan_start, loan_end),
        'daily_interest_breakdown': daily_breakdown,
        'daily_interest_breakdown_total': len(daily_breakdown),
        'average_reference_rate': float(average_reference_rate),
        'average_effective_rate': float(average_effective_rate),
        'latest_reference_rate': float(latest_reference_rate),
        'latest_effective_rate': float(latest_effective_rate),
        'day_count': day_count,
        'start_date': loan_start.isoformat(),
        'end_date': loan_end.isoformat(),
        'reference_rate': reference_rate,
        'spread': float(spread),
        'convention_note': 'Prototype convention: for non-business days, the most recently published business-day observation is carried forward for the calendar day.'
    }
    return _attach_rate_chart(result, rate_history, reference_rate, loan_start, loan_end)


def calculate_active_loan(principal: float, reference_rate: str, spread: float, start_date: str, as_of_date: str | None = None) -> dict:
    loan_start = date.fromisoformat(start_date)
    today = date.fromisoformat(as_of_date) if as_of_date else date.today()
    if today < loan_start:
        raise ValueError('The active loan as-of date cannot be earlier than the loan start date.')

    rate_history = _fetch_reference_rates(reference_rate, loan_start, today)
    principal_decimal = _as_decimal(principal)
    spread_decimal = _as_decimal(spread)
    denominator = Decimal(360)
    total_interest = Decimal('0')
    daily_breakdown = []

    for day in _date_range(loan_start, today):
        applicable = _resolve_rate_for_day(rate_history, day)
        if applicable is None:
            continue
        reference_rate_value = _as_decimal(applicable['rate'])
        effective_rate = reference_rate_value + spread_decimal
        daily_interest = principal_decimal * (effective_rate / Decimal('100')) * (Decimal('1') / denominator)
        total_interest += daily_interest
        daily_breakdown.append({
            'date': day.isoformat(),
            'reference_rate': float(reference_rate_value),
            'effective_rate': float(effective_rate),
            'daily_interest': float(_round_money(daily_interest)),
            'source': applicable.get('source', 'Official rate source')
        })

    if not rate_history:
        raise ValueError(f'No published {reference_rate} observations were available for this active loan.')

    latest_rate = rate_history[-1]
    latest_rate_value = _as_decimal(latest_rate['rate'])
    current_effective_rate = latest_rate_value + spread_decimal
    current_amount = principal_decimal + total_interest
    last_update = date.fromisoformat(latest_rate['date'])

    result = {
        'loan_status': 'ACTIVE',
        'principal': float(_round_money(principal_decimal)),
        'reference_rate': reference_rate,
        'spread': float(spread),
        'latest_available_reference_rate': float(latest_rate_value),
        'current_effective_rate': float(current_effective_rate),
        'accrued_interest_so_far': float(_round_money(total_interest)),
        'current_amount': float(_round_money(current_amount)),
        'last_rate_update': last_update.isoformat(),
        'consulted_rate_history': _build_rate_history(rate_history, loan_start, today),
        'rate_history': _build_rate_history(rate_history, loan_start, today),
        'daily_interest_breakdown': daily_breakdown,
        'daily_interest_breakdown_total': len(daily_breakdown),
        'future_interest_note': 'Not yet accrued / dependent on future reference-rate observations.',
        'convention_note': 'Prototype convention: the latest published reference-rate observation is used as the current accrual rate, and future values are not forecast.'
    }
    return _attach_rate_chart(result, rate_history, reference_rate, loan_start, last_update)
