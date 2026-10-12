from decimal import Decimal, ROUND_HALF_UP
from typing import Sequence

CENT = Decimal('0.01')


def validate_drawdown(amount: Decimal, undrawn: Decimal) -> None:
    if amount <= 0:
        raise ValueError('Drawdown amount must be greater than zero.')
    if amount > undrawn:
        raise ValueError('Drawdown amount exceeds the undrawn facility amount.')


def weighted_average_spread(commitments: Sequence[tuple[Decimal, Decimal]]) -> Decimal:
    total = sum((amount for amount, _ in commitments), Decimal('0'))
    if total <= 0:
        raise ValueError('At least one positive commitment is required.')
    weighted_total = sum((amount * spread for amount, spread in commitments), Decimal('0'))
    return weighted_total / total


def pro_rata_allocations(
    amount: Decimal,
    participants: Sequence[tuple[int, Decimal]],
) -> list[tuple[int, Decimal]]:
    if amount <= 0:
        raise ValueError('Allocation amount must be greater than zero.')
    if not participants or any(commitment <= 0 for _, commitment in participants):
        raise ValueError('Positive active participant commitments are required.')

    total = sum((commitment for _, commitment in participants), Decimal('0'))
    rounded = [
        (participant_id, (amount * commitment / total).quantize(CENT, rounding=ROUND_HALF_UP))
        for participant_id, commitment in participants
    ]
    remainder = amount - sum((share for _, share in rounded), Decimal('0'))
    largest_index = max(range(len(participants)), key=lambda index: participants[index][1])
    participant_id, share = rounded[largest_index]
    rounded[largest_index] = (participant_id, share + remainder)
    return rounded


def daily_interest(
    principal: Decimal,
    reference_rate: Decimal,
    spread: Decimal,
    day_count_convention: str,
) -> tuple[Decimal, Decimal]:
    if principal < 0:
        raise ValueError('Principal cannot be negative.')
    if day_count_convention not in {'ACT/360', 'ACT/365'}:
        raise ValueError('Day count convention must be ACT/360 or ACT/365.')
    effective_rate = reference_rate + spread
    divisor = Decimal('360') if day_count_convention == 'ACT/360' else Decimal('365')
    interest = principal * effective_rate / Decimal('100') / divisor
    return effective_rate, interest.quantize(Decimal('0.00000001'), rounding=ROUND_HALF_UP)
