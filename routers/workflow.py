from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from psycopg2.extras import RealDictCursor

from database import get_connection
from services.workflow_rules import (
    daily_interest,
    pro_rata_allocations,
    validate_drawdown,
    weighted_average_spread,
)

Money = Decimal
Rate = Decimal
class LoanRequestCreate(BaseModel):
    requested_amount: Money = Field(gt=0)
    currency: str = Field(pattern=r'^(USD|GBP)$')
    purpose: str = Field(min_length=3, max_length=4000)
    tenor_months: int = Field(gt=0, le=600)


class ReviewUpdate(BaseModel):
    status: str = Field(pattern=r'^(UNDER_REVIEW|APPROVED|REJECTED)$')
    review_notes: str | None = Field(default=None, max_length=4000)


class FacilityCreate(BaseModel):
    loan_request_id: int = Field(gt=0)
    facility_name: str = Field(min_length=2, max_length=200)
    facility_amount: Money = Field(gt=0)
    currency: str = Field(pattern=r'^(USD|GBP)$')
    start_date: date
    maturity_date: date
    reference_rate: str = Field(pattern=r'^(SOFR|SONIA)$')
    day_count_convention: str = Field(pattern=r'^(ACT/360|ACT/365)$')


class ProposalCreate(BaseModel):
    commitment_amount: Money = Field(gt=0)
    offered_spread: Rate = Field(ge=0, le=100)


class ParticipantStatus(BaseModel):
    status: str = Field(pattern=r'^(ACTIVE|REJECTED)$')


class DrawdownCreate(BaseModel):
    drawdown_amount: Money = Field(gt=0)
    drawdown_date: date


class RateObservationCreate(BaseModel):
    reference_rate: str = Field(pattern=r'^(SOFR|SONIA)$')
    observation_date: date
    rate: Rate = Field(ge=0, le=100)
    source: str | None = Field(default=None, max_length=200)


class PaymentCreate(BaseModel):
    schedule_id: int = Field(gt=0)
    payment_amount: Money = Field(gt=0)
    payment_date: date
    payment_reference: str | None = Field(default=None, max_length=100)


def _decimal(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _date_value(value: Any) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _row(cursor: RealDictCursor) -> dict[str, Any] | None:
    result = cursor.fetchone()
    return {key: _json_value(value) for key, value in result.items()} if result else None


def _rows(cursor: RealDictCursor) -> list[dict[str, Any]]:
    return [{key: _json_value(value) for key, value in result.items()} for result in cursor.fetchall()]


def _require_user(user: dict[str, Any], user_id: int, role: str | tuple[str, ...]) -> None:
    roles = (role,) if isinstance(role, str) else role
    if user['user_id'] != user_id or user['user_type'] not in roles:
        raise HTTPException(status_code=403, detail='You are not authorized to access this resource.')


def _facility_for_user(cursor: RealDictCursor, facility_id: int, user: dict[str, Any], lock: bool = False) -> dict[str, Any]:
    cursor.execute('SELECT * FROM facilities WHERE facility_id = %s' + (' FOR UPDATE' if lock else ''), (facility_id,))
    facility = _row(cursor)
    if not facility:
        raise HTTPException(status_code=404, detail='Facility not found.')

    role = user['user_type']
    allowed = (
        (role == 'COMPANY' and facility['company_user_id'] == user['user_id'])
        or (role == 'AGENT' and facility['agent_user_id'] == user['user_id'])
    )
    if role == 'BANK':
        cursor.execute(
            'SELECT 1 FROM facility_participants WHERE facility_id = %s AND bank_user_id = %s',
            (facility_id, user['user_id']),
        )
        allowed = cursor.fetchone() is not None
    if not allowed:
        raise HTTPException(status_code=403, detail='You are not associated with this facility.')
    return facility


def _facility_ids_for_user(cursor: RealDictCursor, user: dict[str, Any]) -> list[int]:
    role, user_id = user['user_type'], user['user_id']
    if role == 'COMPANY':
        cursor.execute('SELECT facility_id FROM facilities WHERE company_user_id = %s', (user_id,))
    elif role == 'AGENT':
        cursor.execute('SELECT facility_id FROM facilities WHERE agent_user_id = %s', (user_id,))
    else:
        cursor.execute('SELECT facility_id FROM facility_participants WHERE bank_user_id = %s', (user_id,))
    return [row['facility_id'] for row in cursor.fetchall()]


def _undrawn(cursor: RealDictCursor, facility_id: int, facility_amount: Decimal) -> Decimal:
    cursor.execute(
        "SELECT COALESCE(SUM(drawdown_amount), 0) AS total FROM drawdowns WHERE facility_id = %s AND status IN ('REQUESTED', 'APPROVED', 'DISBURSED', 'REPAID')",
        (facility_id,),
    )
    return max(facility_amount - _decimal(cursor.fetchone()['total']), Decimal('0'))


def _uncommitted(cursor: RealDictCursor, facility_id: int, facility_amount: Decimal) -> Decimal:
    cursor.execute(
        "SELECT COALESCE(SUM(commitment_amount), 0) AS total FROM facility_participants WHERE facility_id = %s AND status IN ('PROPOSED', 'ACTIVE')",
        (facility_id,),
    )
    return max(facility_amount - _decimal(cursor.fetchone()['total']), Decimal('0'))


def _run_accruals(
    cursor: RealDictCursor,
    agent_user_id: int,
    accrual_date: date,
    facility_id: int | None = None,
) -> dict[str, Any]:
    conditions = ['f.agent_user_id = %s', "f.status = 'ACTIVE'", "d.status = 'DISBURSED'"]
    parameters: list[Any] = [agent_user_id]
    if facility_id is not None:
        conditions.append('f.facility_id = %s')
        parameters.append(facility_id)
    cursor.execute(
        f"""
        SELECT d.drawdown_id, d.facility_id, d.drawdown_amount, d.drawdown_date
        FROM drawdowns d JOIN facilities f USING (facility_id)
        WHERE {' AND '.join(conditions)}
        ORDER BY d.facility_id, d.drawdown_date, d.drawdown_id
        FOR UPDATE OF d
        """,
        parameters,
    )
    drawdowns = cursor.fetchall()
    inserted = 0
    missing_rate_days = 0
    missing_term_days = 0

    for drawdown in drawdowns:
        if _date_value(drawdown['drawdown_date']) > accrual_date:
            continue
        drawdown_id = drawdown['drawdown_id']
        current_date = accrual_date
        cursor.execute(
            'SELECT 1 FROM interest_accruals WHERE facility_id = %s AND drawdown_id = %s AND accrual_date = %s',
            (drawdown['facility_id'], drawdown_id, current_date),
        )
        if cursor.fetchone():
            continue

        cursor.execute(
            """
            SELECT reference_rate, spread, day_count_convention
            FROM loan_rate_terms
            WHERE facility_id = %s AND effective_from <= %s
              AND (effective_to IS NULL OR effective_to >= %s)
            ORDER BY effective_from DESC LIMIT 1
            """,
            (drawdown['facility_id'], current_date, current_date),
        )
        terms = cursor.fetchone()
        if not terms:
            missing_term_days += 1
            continue

        cursor.execute(
            """
            SELECT rate FROM reference_rate_observations
            WHERE reference_rate = %s AND observation_date = %s
            """,
            (terms['reference_rate'], current_date),
        )
        observation = cursor.fetchone()
        if not observation:
            missing_rate_days += 1
            continue

        principal = _decimal(drawdown['drawdown_amount'])
        reference_rate = _decimal(observation['rate'])
        spread = _decimal(terms['spread'])
        effective_rate, interest = daily_interest(
            principal,
            reference_rate,
            spread,
            terms['day_count_convention'],
        )
        cursor.execute(
            """
            INSERT INTO interest_accruals
                (facility_id, drawdown_id, accrual_date, principal_amount, reference_rate,
                 spread, effective_rate, day_count_convention, interest_amount, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'ACCRUED')
            ON CONFLICT (facility_id, drawdown_id, accrual_date) DO NOTHING
            RETURNING accrual_id
            """,
            (
                drawdown['facility_id'], drawdown_id, current_date, principal,
                reference_rate, spread, effective_rate,
                terms['day_count_convention'], interest,
            ),
        )
        if cursor.fetchone():
            inserted += 1
            cursor.execute(
                """
                INSERT INTO ledger_entries
                    (facility_id, transaction_type, reference_id, transaction_date,
                     debit_amount, credit_amount, description)
                VALUES (%s, 'INTEREST_ACCRUAL', %s, %s, %s, 0, 'Daily interest accrual')
                """,
                (drawdown['facility_id'], drawdown_id, current_date, interest),
            )

    return {
        'rows_written': inserted,
        'missing_rate_days': missing_rate_days,
        'missing_term_days': missing_term_days,
    }


def _refresh_repayment_schedule(cursor: RealDictCursor, facility_id: int) -> dict[str, Any] | None:
    cursor.execute('SELECT maturity_date FROM facilities WHERE facility_id = %s', (facility_id,))
    facility = cursor.fetchone()
    if not facility:
        raise HTTPException(status_code=404, detail='Facility not found.')
    cursor.execute(
        """
        SELECT COALESCE(SUM(drawdown_amount), 0) AS principal
        FROM drawdowns WHERE facility_id = %s AND status IN ('DISBURSED', 'REPAID')
        """,
        (facility_id,),
    )
    principal = _decimal(cursor.fetchone()['principal'])
    if principal <= 0:
        return None
    cursor.execute(
        'SELECT COALESCE(SUM(interest_amount), 0) AS interest FROM interest_accruals WHERE facility_id = %s',
        (facility_id,),
    )
    interest = _decimal(cursor.fetchone()['interest'])
    total_due = principal + interest
    cursor.execute(
        'SELECT schedule_id FROM repayment_schedules WHERE facility_id = %s ORDER BY schedule_id LIMIT 1 FOR UPDATE',
        (facility_id,),
    )
    existing = cursor.fetchone()
    cursor.execute(
        """
        SELECT COALESCE(SUM(payment_amount), 0) AS paid
        FROM payments WHERE facility_id = %s AND status = 'COMPLETED'
        """,
        (facility_id,),
    )
    paid = _decimal(cursor.fetchone()['paid'])
    status = 'PAID' if paid >= total_due else 'PARTIALLY_PAID' if paid > 0 else 'PENDING'
    if existing:
        cursor.execute(
            """
            UPDATE repayment_schedules
            SET due_date = %s, principal_due = %s, interest_due = %s, status = %s
            WHERE schedule_id = %s
            RETURNING schedule_id, facility_id, due_date, principal_due, interest_due, total_due, status
            """,
            (facility['maturity_date'], principal, interest, status, existing['schedule_id']),
        )
    else:
        cursor.execute(
            """
            INSERT INTO repayment_schedules (facility_id, due_date, principal_due, interest_due, status)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING schedule_id, facility_id, due_date, principal_due, interest_due, total_due, status
            """,
            (facility_id, facility['maturity_date'], principal, interest, status),
        )
    return _row(cursor)


def _make_router(auth_dependency: Callable[..., Any]) -> APIRouter:
    router = APIRouter()

    @router.get('/api/company/{user_id}/overview')
    def company_overview(user_id: int, user: dict[str, Any] = Depends(auth_dependency)):
        _require_user(user, user_id, 'COMPANY')
        with get_connection() as connection, connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT COALESCE(SUM(facility_amount), 0) AS total_facility,
                       COUNT(*) FILTER (WHERE status = 'ACTIVE') AS active_facilities
                FROM facilities WHERE company_user_id = %s
                """,
                (user_id,),
            )
            totals = cursor.fetchone()
            cursor.execute(
                """
                SELECT
                    COALESCE((SELECT SUM(d.drawdown_amount)
                              FROM drawdowns d JOIN facilities f USING (facility_id)
                              WHERE f.company_user_id = %s AND d.status IN ('DISBURSED', 'REPAID')), 0)
                    - COALESCE((SELECT SUM(pa.allocated_amount)
                                FROM payment_allocations pa JOIN payments p USING (payment_id)
                                JOIN facilities f USING (facility_id)
                                WHERE f.company_user_id = %s AND p.status = 'COMPLETED'
                                  AND pa.allocation_type = 'PRINCIPAL'), 0) AS outstanding_drawn
                """,
                (user_id, user_id),
            )
            outstanding_drawn = max(_decimal(cursor.fetchone()['outstanding_drawn']), Decimal('0'))
            cursor.execute(
                """
                SELECT COALESCE(SUM(d.drawdown_amount), 0) AS gross_drawn
                FROM drawdowns d JOIN facilities f USING (facility_id)
                WHERE f.company_user_id = %s AND d.status IN ('REQUESTED', 'APPROVED', 'DISBURSED', 'REPAID')
                """,
                (user_id,),
            )
            gross_drawn = _decimal(cursor.fetchone()['gross_drawn'])
            cursor.execute(
                """
                SELECT COALESCE(SUM(ia.interest_amount), 0) AS accrued
                FROM interest_accruals ia JOIN facilities f USING (facility_id)
                WHERE f.company_user_id = %s AND ia.status IN ('ACCRUED', 'DUE')
                """,
                (user_id,),
            )
            accrued = _decimal(cursor.fetchone()['accrued'])
            cursor.execute(
                """
                SELECT due_date, total_due, currency
                FROM repayment_schedules rs JOIN facilities f USING (facility_id)
                WHERE f.company_user_id = %s AND rs.status IN ('PENDING', 'PARTIALLY_PAID', 'OVERDUE')
                  AND due_date >= CURRENT_DATE
                ORDER BY due_date LIMIT 1
                """,
                (user_id,),
            )
            next_repayment = _row(cursor)
            cursor.execute(
                "SELECT COUNT(*) AS count FROM loan_requests WHERE company_user_id = %s AND status IN ('PENDING', 'UNDER_REVIEW')",
                (user_id,),
            )
            open_requests = cursor.fetchone()['count']
            total_facility = _decimal(totals['total_facility'])
            return {
                'user': {
                    'user_id': user['user_id'],
                    'username': user['username'],
                    'user_type': user['user_type'],
                    'organization_name': user['organization_name'],
                },
                'total_facility': str(total_facility),
                'total_drawn': str(outstanding_drawn),
                'undrawn': str(max(total_facility - gross_drawn, Decimal('0'))),
                'accrued_interest': str(accrued),
                'next_repayment': next_repayment,
                'open_requests': open_requests,
                'active_facilities': totals['active_facilities'],
            }

    @router.get('/api/company/{user_id}/loan-requests')
    def company_requests(user_id: int, user: dict[str, Any] = Depends(auth_dependency)):
        _require_user(user, user_id, 'COMPANY')
        with get_connection() as connection, connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                'SELECT loan_request_id, requested_amount, currency, purpose, requested_date, tenor_months, status, review_notes FROM loan_requests WHERE company_user_id = %s ORDER BY requested_date DESC, loan_request_id DESC',
                (user_id,),
            )
            return _rows(cursor)

    @router.post('/api/company/{user_id}/loan-requests', status_code=201)
    def create_company_request(user_id: int, body: LoanRequestCreate, user: dict[str, Any] = Depends(auth_dependency)):
        _require_user(user, user_id, 'COMPANY')
        with get_connection() as connection, connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                INSERT INTO loan_requests
                    (company_user_id, requested_amount, currency, purpose, requested_date, tenor_months, status)
                VALUES (%s, %s, %s, %s, CURRENT_DATE, %s, 'PENDING')
                RETURNING loan_request_id, requested_amount, currency, purpose, requested_date, tenor_months, status
                """,
                (user_id, body.requested_amount, body.currency, body.purpose.strip(), body.tenor_months),
            )
            return _row(cursor)

    @router.patch('/api/loan-requests/{request_id}/cancel')
    def cancel_request(request_id: int, user: dict[str, Any] = Depends(auth_dependency)):
        if user['user_type'] != 'COMPANY':
            raise HTTPException(status_code=403, detail='Only the requesting company can cancel a request.')
        with get_connection() as connection, connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute('SELECT status, company_user_id FROM loan_requests WHERE loan_request_id = %s FOR UPDATE', (request_id,))
            request = _row(cursor)
            if not request:
                raise HTTPException(status_code=404, detail='Loan request not found.')
            if request['company_user_id'] != user['user_id']:
                raise HTTPException(status_code=403, detail='You do not own this loan request.')
            if request['status'] != 'PENDING':
                raise HTTPException(status_code=409, detail='Only pending loan requests can be cancelled.')
            cursor.execute("UPDATE loan_requests SET status = 'CANCELLED' WHERE loan_request_id = %s RETURNING loan_request_id, status", (request_id,))
            return _row(cursor)

    @router.get('/api/company/{user_id}/facilities')
    def company_facilities(user_id: int, user: dict[str, Any] = Depends(auth_dependency)):
        _require_user(user, user_id, 'COMPANY')
        with get_connection() as connection, connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT f.facility_id, f.loan_request_id, f.facility_name, f.facility_amount,
                       f.currency, f.start_date, f.maturity_date, f.status,
                       lt.reference_rate, lt.spread, lt.day_count_convention
                FROM facilities f LEFT JOIN LATERAL (
                    SELECT reference_rate, spread, day_count_convention FROM loan_rate_terms
                    WHERE facility_id = f.facility_id ORDER BY effective_from DESC LIMIT 1
                ) lt ON TRUE
                WHERE f.company_user_id = %s ORDER BY f.created_at DESC
                """,
                (user_id,),
            )
            return _rows(cursor)

    @router.get('/api/company/{user_id}/repayments')
    def company_repayments(user_id: int, user: dict[str, Any] = Depends(auth_dependency)):
        _require_user(user, user_id, 'COMPANY')
        with get_connection() as connection, connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT rs.schedule_id, rs.facility_id, f.facility_name, rs.due_date,
                       rs.principal_due, rs.interest_due, rs.total_due, rs.status, f.currency
                FROM repayment_schedules rs JOIN facilities f USING (facility_id)
                WHERE f.company_user_id = %s ORDER BY rs.due_date
                """,
                (user_id,),
            )
            return _rows(cursor)

    @router.get('/api/company/{user_id}/payments')
    def company_payments(user_id: int, user: dict[str, Any] = Depends(auth_dependency)):
        _require_user(user, user_id, 'COMPANY')
        with get_connection() as connection, connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT p.payment_id, p.facility_id, p.repayment_schedule_id, p.payment_amount,
                       p.currency, p.payment_date, p.payment_reference, p.status,
                       COALESCE(json_agg(json_build_object('type', pa.allocation_type, 'amount', pa.allocated_amount))
                         FILTER (WHERE pa.payment_allocation_id IS NOT NULL), '[]') AS allocations
                FROM payments p JOIN facilities f USING (facility_id)
                LEFT JOIN payment_allocations pa USING (payment_id)
                WHERE p.payer_user_id = %s
                GROUP BY p.payment_id, f.facility_name
                ORDER BY p.payment_date DESC, p.payment_id DESC
                """,
                (user_id,),
            )
            return _rows(cursor)

    @router.post('/api/company/{user_id}/payments', status_code=201)
    def create_company_payment(user_id: int, body: PaymentCreate, user: dict[str, Any] = Depends(auth_dependency)):
        _require_user(user, user_id, 'COMPANY')
        if body.payment_date > date.today():
            raise HTTPException(status_code=400, detail='Payment date cannot be in the future.')
        with get_connection() as connection, connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT rs.schedule_id, rs.facility_id, rs.total_due, rs.status,
                       f.currency, f.agent_user_id
                FROM repayment_schedules rs JOIN facilities f USING (facility_id)
                WHERE rs.schedule_id = %s AND f.company_user_id = %s FOR UPDATE OF rs
                """,
                (body.schedule_id, user_id),
            )
            schedule = _row(cursor)
            if not schedule:
                raise HTTPException(status_code=404, detail='Repayment schedule not found.')
            if not schedule['agent_user_id']:
                raise HTTPException(status_code=409, detail='This facility has no assigned agent to receive the payment.')
            if schedule['status'] == 'PAID':
                raise HTTPException(status_code=409, detail='Repayment is already paid.')
            cursor.execute(
                """
                SELECT COALESCE(SUM(payment_amount), 0) AS paid FROM payments
                WHERE repayment_schedule_id = %s AND status IN ('PENDING', 'PROCESSING', 'COMPLETED')
                """,
                (body.schedule_id,),
            )
            outstanding = _decimal(schedule['total_due']) - _decimal(cursor.fetchone()['paid'])
            if outstanding <= 0:
                raise HTTPException(status_code=409, detail='There is no unpaid repayment balance.')
            if body.payment_amount > outstanding:
                raise HTTPException(status_code=400, detail='Payment exceeds the unpaid repayment amount.')
            reference = body.payment_reference or f'ARRA-{uuid4().hex[:16].upper()}'
            cursor.execute(
                """
                INSERT INTO payments (facility_id, repayment_schedule_id, payer_user_id, receiver_user_id,
                    payment_amount, payment_date, currency, payment_reference, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'PENDING')
                RETURNING payment_id, facility_id, repayment_schedule_id, payment_amount, payment_date, payment_reference, status
                """,
                (schedule['facility_id'], body.schedule_id, user_id, schedule['agent_user_id'],
                 body.payment_amount, body.payment_date, schedule['currency'], reference),
            )
            return _row(cursor)

    @router.get('/api/agent/{user_id}/overview')
    def agent_overview(user_id: int, user: dict[str, Any] = Depends(auth_dependency)):
        _require_user(user, user_id, 'AGENT')
        today = date.today()
        with get_connection() as connection, connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute("SELECT status, COUNT(*) AS count FROM loan_requests GROUP BY status")
            request_counts = {row['status']: row['count'] for row in cursor.fetchall()}
            cursor.execute("SELECT status, COUNT(*) AS count FROM facilities WHERE agent_user_id = %s GROUP BY status", (user_id,))
            facility_counts = {row['status']: row['count'] for row in cursor.fetchall()}
            cursor.execute(
                """
                SELECT COUNT(*) AS count FROM drawdowns d JOIN facilities f USING (facility_id)
                WHERE f.agent_user_id = %s AND d.status = 'REQUESTED'
                """,
                (user_id,),
            )
            drawdowns_pending = cursor.fetchone()['count']
            cursor.execute(
                """
                SELECT COUNT(*) AS count FROM facility_participants fp
                JOIN facilities f USING (facility_id)
                WHERE f.agent_user_id = %s AND fp.status = 'PROPOSED'
                """,
                (user_id,),
            )
            proposals_pending = cursor.fetchone()['count']
            cursor.execute(
                "SELECT reference_rate, rate FROM reference_rate_observations WHERE observation_date = %s",
                (today,),
            )
            rates_today = {row['reference_rate']: str(row['rate']) for row in cursor.fetchall()}
            cursor.execute('SELECT COUNT(*) AS count FROM interest_accruals WHERE accrual_date = %s', (today,))
            accrual_count = cursor.fetchone()['count']
            return {
                'user': {
                    'user_id': user['user_id'],
                    'username': user['username'],
                    'user_type': user['user_type'],
                    'organization_name': user['organization_name'],
                },
                'pending_requests': request_counts.get('PENDING', 0),
                'under_review': request_counts.get('UNDER_REVIEW', 0),
                'facilities_by_status': facility_counts,
                'drawdowns_awaiting_approval': drawdowns_pending,
                'proposals_awaiting_decision': proposals_pending,
                'rates_today': rates_today,
                'accruals_run': accrual_count > 0,
                'accrual_rows_today': accrual_count,
            }

    @router.get('/api/agent/{user_id}/loan-requests')
    def agent_requests(user_id: int, status: str | None = Query(default=None), user: dict[str, Any] = Depends(auth_dependency)):
        _require_user(user, user_id, 'AGENT')
        with get_connection() as connection, connection.cursor(cursor_factory=RealDictCursor) as cursor:
            if status:
                cursor.execute(
                    """
                    SELECT lr.*, u.organization_name AS company_name
                    FROM loan_requests lr JOIN users u ON u.user_id = lr.company_user_id
                    WHERE lr.status = %s ORDER BY lr.requested_date, lr.loan_request_id
                    """,
                    (status.upper(),),
                )
            else:
                cursor.execute(
                    """
                    SELECT lr.*, u.organization_name AS company_name
                    FROM loan_requests lr JOIN users u ON u.user_id = lr.company_user_id
                    ORDER BY lr.requested_date, lr.loan_request_id
                    """
                )
            return _rows(cursor)

    @router.patch('/api/loan-requests/{request_id}/review')
    def review_request(request_id: int, body: ReviewUpdate, user: dict[str, Any] = Depends(auth_dependency)):
        if user['user_type'] != 'AGENT':
            raise HTTPException(status_code=403, detail='Only agents can review loan requests.')
        with get_connection() as connection, connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute('SELECT status FROM loan_requests WHERE loan_request_id = %s FOR UPDATE', (request_id,))
            current = _row(cursor)
            if not current:
                raise HTTPException(status_code=404, detail='Loan request not found.')
            allowed = {'PENDING': {'UNDER_REVIEW', 'APPROVED', 'REJECTED'}, 'UNDER_REVIEW': {'UNDER_REVIEW', 'APPROVED', 'REJECTED'}}
            if body.status not in allowed.get(current['status'], set()):
                raise HTTPException(status_code=409, detail=f"Cannot change request from {current['status']} to {body.status}.")
            cursor.execute(
                'UPDATE loan_requests SET status = %s, review_notes = %s WHERE loan_request_id = %s RETURNING loan_request_id, status, review_notes',
                (body.status, body.review_notes, request_id),
            )
            return _row(cursor)

    @router.post('/api/agent/{user_id}/facilities', status_code=201)
    def create_facility(user_id: int, body: FacilityCreate, user: dict[str, Any] = Depends(auth_dependency)):
        _require_user(user, user_id, 'AGENT')
        if body.maturity_date <= body.start_date:
            raise HTTPException(status_code=422, detail='Maturity date must be after the start date.')
        with get_connection() as connection, connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute('SELECT * FROM loan_requests WHERE loan_request_id = %s FOR UPDATE', (body.loan_request_id,))
            request = _row(cursor)
            if not request:
                raise HTTPException(status_code=404, detail='Loan request not found.')
            if request['status'] != 'APPROVED':
                raise HTTPException(status_code=409, detail='Facility can only be created from an approved request.')
            if request['currency'] != body.currency:
                raise HTTPException(status_code=400, detail='Facility currency must match the approved loan request.')
            if body.facility_amount > _decimal(request['requested_amount']):
                raise HTTPException(status_code=400, detail='Facility amount cannot exceed the approved request amount.')
            cursor.execute('SELECT 1 FROM facilities WHERE loan_request_id = %s', (body.loan_request_id,))
            if cursor.fetchone():
                raise HTTPException(status_code=409, detail='A facility already exists for this loan request.')
            cursor.execute(
                """
                INSERT INTO facilities (loan_request_id, company_user_id, agent_user_id, facility_name,
                    facility_amount, currency, start_date, maturity_date, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'PENDING')
                RETURNING facility_id, loan_request_id, facility_name, facility_amount, currency,
                          start_date, maturity_date, status
                """,
                (body.loan_request_id, request['company_user_id'], user_id, body.facility_name.strip(),
                 body.facility_amount, body.currency, body.start_date, body.maturity_date),
            )
            facility = _row(cursor)
            cursor.execute(
                """
                INSERT INTO loan_rate_terms
                    (facility_id, reference_rate, spread, day_count_convention, effective_from)
                VALUES (%s, %s, 0, %s, %s)
                """,
                (facility['facility_id'], body.reference_rate, body.day_count_convention, body.start_date),
            )
            return facility

    @router.get('/api/agent/{user_id}/facilities')
    def agent_facilities(user_id: int, user: dict[str, Any] = Depends(auth_dependency)):
        _require_user(user, user_id, 'AGENT')
        with get_connection() as connection, connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT f.facility_id, f.facility_name, f.facility_amount, f.currency, f.start_date,
                       f.maturity_date, f.status, c.organization_name AS company_name,
                       COALESCE(SUM(fp.commitment_amount) FILTER (WHERE fp.status = 'ACTIVE'), 0) AS committed
                FROM facilities f JOIN users c ON c.user_id = f.company_user_id
                LEFT JOIN facility_participants fp USING (facility_id)
                WHERE f.agent_user_id = %s
                GROUP BY f.facility_id, c.organization_name ORDER BY f.created_at DESC
                """,
                (user_id,),
            )
            return _rows(cursor)

    @router.get('/api/agent/{user_id}/proposals')
    def agent_proposals(user_id: int, status: str = 'PROPOSED', user: dict[str, Any] = Depends(auth_dependency)):
        _require_user(user, user_id, 'AGENT')
        with get_connection() as connection, connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT fp.participant_id, fp.facility_id, fp.commitment_amount,
                       fp.commitment_percentage, fp.offered_spread, fp.status,
                       f.facility_name, f.facility_amount, f.currency, b.organization_name AS bank_name,
                       (SELECT SUM(active.commitment_amount * active.offered_spread)
                               / NULLIF(SUM(active.commitment_amount), 0)
                        FROM facility_participants active
                        WHERE active.facility_id = f.facility_id
                          AND active.status IN ('ACTIVE', 'PROPOSED')) AS weighted_average_spread_preview
                FROM facility_participants fp JOIN facilities f USING (facility_id)
                JOIN users b ON b.user_id = fp.bank_user_id
                WHERE f.agent_user_id = %s AND fp.status = %s
                ORDER BY f.facility_id, fp.participant_id
                """,
                (user_id, status.upper()),
            )
            return _rows(cursor)

    @router.patch('/api/participants/{participant_id}')
    def decide_proposal(participant_id: int, body: ParticipantStatus, user: dict[str, Any] = Depends(auth_dependency)):
        if user['user_type'] != 'AGENT':
            raise HTTPException(status_code=403, detail='Only agents can decide proposals.')
        with get_connection() as connection, connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
            SELECT fp.status, fp.commitment_amount, f.agent_user_id, f.facility_amount
            FROM facility_participants fp
            JOIN facilities f USING (facility_id) WHERE fp.participant_id = %s FOR UPDATE OF fp
            """,
            (participant_id,),
        )
            participant = _row(cursor)
            if not participant:
                raise HTTPException(status_code=404, detail='Proposal not found.')
            if participant['agent_user_id'] != user['user_id']:
                raise HTTPException(status_code=403, detail='Proposal is not assigned to your agency.')
            if participant['status'] != 'PROPOSED':
                raise HTTPException(status_code=409, detail='Only proposed participations can be decided.')
            cursor.execute(
                """
                UPDATE facility_participants
                SET status = %s
                WHERE participant_id = %s RETURNING participant_id, status
                """,
                (body.status, participant_id),
            )
            return _row(cursor)

    @router.post('/api/facilities/{facility_id}/activate')
    def activate_facility(facility_id: int, user: dict[str, Any] = Depends(auth_dependency)):
        if user['user_type'] != 'AGENT':
            raise HTTPException(status_code=403, detail='Only the assigned agent can activate a facility.')
        with get_connection() as connection, connection.cursor(cursor_factory=RealDictCursor) as cursor:
            facility = _facility_for_user(cursor, facility_id, user, lock=True)
            if facility['status'] != 'PENDING':
                raise HTTPException(status_code=409, detail='Only pending facilities can be activated.')
            cursor.execute(
                'SELECT participant_id, commitment_amount, offered_spread FROM facility_participants WHERE facility_id = %s AND status = %s ORDER BY participant_id FOR UPDATE',
                (facility_id, 'ACTIVE'),
            )
            participants = cursor.fetchall()
            total = sum((_decimal(row['commitment_amount']) for row in participants), Decimal('0'))
            amount = _decimal(facility['facility_amount'])
            if total < amount:
                raise HTTPException(
                    status_code=409,
                    detail='Accepted bank commitments must cover the full facility amount before activation.',
                )
            avg_spread = weighted_average_spread([(_decimal(row['commitment_amount']), _decimal(row['offered_spread'])) for row in participants])
            cursor.execute(
                """
                SELECT reference_rate, day_count_convention FROM loan_rate_terms
                WHERE facility_id = %s ORDER BY effective_from DESC LIMIT 1
                """,
                (facility_id,),
            )
            terms = _row(cursor)
            if not terms:
                raise HTTPException(status_code=409, detail='Facility has no reference rate terms.')
            cursor.execute(
                """
                UPDATE loan_rate_terms SET spread = %s
                WHERE rate_term_id = (
                    SELECT rate_term_id FROM loan_rate_terms
                    WHERE facility_id = %s ORDER BY effective_from DESC LIMIT 1
                )
                """,
                (avg_spread, facility_id),
            )
            cursor.execute("UPDATE facilities SET status = 'ACTIVE' WHERE facility_id = %s RETURNING facility_id, status", (facility_id,))
            return {'facility': _row(cursor), 'weighted_average_spread': str(avg_spread)}

    @router.get('/api/agent/{user_id}/drawdowns')
    def agent_drawdowns(
        user_id: int,
        status: str = Query(default='ALL', pattern=r'^(ALL|REQUESTED|APPROVED|DISBURSED|REPAID)$'),
        user: dict[str, Any] = Depends(auth_dependency),
    ):
        _require_user(user, user_id, 'AGENT')
        with get_connection() as connection, connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT d.drawdown_id, d.facility_id, d.drawdown_amount, d.currency, d.drawdown_date,
                       d.status, f.facility_name, c.organization_name AS company_name
                FROM drawdowns d JOIN facilities f USING (facility_id)
                JOIN users c ON c.user_id = f.company_user_id
                WHERE f.agent_user_id = %s AND (%s = 'ALL' OR d.status = %s)
                ORDER BY d.created_at
                """,
                (user_id, status, status),
            )
            return _rows(cursor)

    @router.patch('/api/drawdowns/{drawdown_id}/approve')
    def approve_drawdown(drawdown_id: int, user: dict[str, Any] = Depends(auth_dependency)):
        if user['user_type'] != 'AGENT':
            raise HTTPException(status_code=403, detail='Only agents can approve drawdowns.')
        with get_connection() as connection, connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT d.*, f.agent_user_id FROM drawdowns d JOIN facilities f USING (facility_id)
                WHERE d.drawdown_id = %s FOR UPDATE OF d
                """,
                (drawdown_id,),
            )
            drawdown = _row(cursor)
            if not drawdown:
                raise HTTPException(status_code=404, detail='Drawdown not found.')
            if drawdown['agent_user_id'] != user['user_id']:
                raise HTTPException(status_code=403, detail='Drawdown is not assigned to your agency.')
            if drawdown['status'] != 'REQUESTED':
                raise HTTPException(status_code=409, detail='Only requested drawdowns can be approved.')
            cursor.execute('SELECT participant_id, commitment_amount FROM facility_participants WHERE facility_id = %s AND status = %s ORDER BY participant_id FOR UPDATE', (drawdown['facility_id'], 'ACTIVE'))
            participants = [(row['participant_id'], _decimal(row['commitment_amount'])) for row in cursor.fetchall()]
            allocations = pro_rata_allocations(_decimal(drawdown['drawdown_amount']), participants)
            for participant_id, allocated in allocations:
                cursor.execute(
                    'INSERT INTO drawdown_allocations (drawdown_id, participant_id, allocated_amount) VALUES (%s, %s, %s)',
                    (drawdown_id, participant_id, allocated),
                )
            cursor.execute("UPDATE drawdowns SET status = 'APPROVED' WHERE drawdown_id = %s RETURNING drawdown_id, status", (drawdown_id,))
            return {'drawdown': _row(cursor), 'allocations': [{'participant_id': pid, 'allocated_amount': str(value)} for pid, value in allocations]}

    @router.patch('/api/drawdowns/{drawdown_id}/disburse')
    def disburse_drawdown(drawdown_id: int, user: dict[str, Any] = Depends(auth_dependency)):
        if user['user_type'] != 'AGENT':
            raise HTTPException(status_code=403, detail='Only agents can disburse drawdowns.')
        with get_connection() as connection, connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                'SELECT d.*, f.agent_user_id FROM drawdowns d JOIN facilities f USING (facility_id) WHERE drawdown_id = %s FOR UPDATE OF d',
                (drawdown_id,),
            )
            drawdown = _row(cursor)
            if not drawdown:
                raise HTTPException(status_code=404, detail='Drawdown not found.')
            if drawdown['agent_user_id'] != user['user_id']:
                raise HTTPException(status_code=403, detail='Drawdown is not assigned to your agency.')
            if drawdown['status'] != 'APPROVED':
                raise HTTPException(status_code=409, detail='Drawdown must be approved before disbursement.')
            cursor.execute(
                """
                INSERT INTO ledger_entries (facility_id, transaction_type, reference_id, transaction_date,
                    debit_amount, credit_amount, description)
                VALUES (%s, 'DRAWDOWN', %s, %s, %s, 0, %s)
                """,
                (drawdown['facility_id'], drawdown_id, drawdown['drawdown_date'], drawdown['drawdown_amount'], f'Drawdown {drawdown_id} disbursed'),
            )
            cursor.execute(
                "UPDATE drawdowns SET status = 'DISBURSED' WHERE drawdown_id = %s RETURNING drawdown_id, status",
                (drawdown_id,),
            )
            result = _row(cursor)
            return result

    @router.post('/api/reference-rates', status_code=201)
    def add_reference_rate(body: RateObservationCreate, user: dict[str, Any] = Depends(auth_dependency)):
        if user['user_type'] != 'AGENT':
            raise HTTPException(status_code=403, detail='Only agents can record reference rates.')
        with get_connection() as connection, connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                INSERT INTO reference_rate_observations (reference_rate, observation_date, rate, source)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (reference_rate, observation_date) DO UPDATE
                SET rate = EXCLUDED.rate, source = EXCLUDED.source
                RETURNING observation_id, reference_rate, observation_date, rate, source
                """,
                (body.reference_rate, body.observation_date, body.rate, body.source),
            )
            return _row(cursor)

    @router.get('/api/reference-rates')
    def get_reference_rates(
        reference_rate: str | None = Query(default=None, pattern=r'^(SOFR|SONIA)$'),
        from_date: date | None = Query(default=None, alias='from'),
        to_date: date | None = Query(default=None, alias='to'),
        user: dict[str, Any] = Depends(auth_dependency),
    ):
        if user['user_type'] not in {'AGENT', 'BANK', 'COMPANY'}:
            raise HTTPException(status_code=403, detail='Unsupported account type.')
        if from_date and to_date and from_date > to_date:
            raise HTTPException(status_code=400, detail='From date must not be after to date.')
        clauses, values = [], []
        if reference_rate:
            clauses.append('reference_rate = %s')
            values.append(reference_rate)
        if from_date:
            clauses.append('observation_date >= %s')
            values.append(from_date)
        if to_date:
            clauses.append('observation_date <= %s')
            values.append(to_date)
        query = 'SELECT observation_id, reference_rate, observation_date, rate, source FROM reference_rate_observations'
        if clauses:
            query += ' WHERE ' + ' AND '.join(clauses)
        query += ' ORDER BY observation_date DESC, reference_rate'
        with get_connection() as connection, connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(query, values)
            return _rows(cursor)

    @router.post('/api/accruals/run')
    def run_accruals(accrual_date: date = Query(alias='date'), user: dict[str, Any] = Depends(auth_dependency)):
        if user['user_type'] != 'AGENT':
            raise HTTPException(status_code=403, detail='Only agents can run accruals.')
        with get_connection() as connection, connection.cursor(cursor_factory=RealDictCursor) as cursor:
            result = _run_accruals(cursor, user['user_id'], accrual_date)
        return {
            'date': accrual_date.isoformat(),
            'rows_written': result['rows_written'],
            'missing_rate_days': result['missing_rate_days'],
            'missing_term_days': result['missing_term_days'],
        }

    @router.post('/api/facilities/{facility_id}/repayment-schedule')
    def recalculate_repayment(facility_id: int, user: dict[str, Any] = Depends(auth_dependency)):
        if user['user_type'] != 'AGENT':
            raise HTTPException(status_code=403, detail='Only the assigned agent can calculate repayments.')
        with get_connection() as connection, connection.cursor(cursor_factory=RealDictCursor) as cursor:
            facility = _facility_for_user(cursor, facility_id, user, lock=True)
            if facility['status'] != 'ACTIVE':
                raise HTTPException(status_code=409, detail='Repayment schedules require an active facility.')
            schedule = _refresh_repayment_schedule(cursor, facility_id)
            if schedule is None:
                raise HTTPException(
                    status_code=409,
                    detail='A repayment schedule requires a disbursed drawdown with outstanding principal.',
                )
            return schedule

    @router.get('/api/agent/{user_id}/payments')
    def agent_payments(user_id: int, user: dict[str, Any] = Depends(auth_dependency)):
        _require_user(user, user_id, 'AGENT')
        with get_connection() as connection, connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT p.payment_id, p.facility_id, p.repayment_schedule_id, p.payer_user_id,
                       p.payment_amount, p.payment_date, p.currency, p.payment_reference, p.status,
                       u.organization_name AS company_name
                FROM payments p JOIN facilities f USING (facility_id)
                JOIN users u ON u.user_id = p.payer_user_id
                WHERE f.agent_user_id = %s ORDER BY p.payment_date DESC, p.payment_id DESC
                """,
                (user_id,),
            )
            return _rows(cursor)

    @router.patch('/api/payments/{payment_id}/complete')
    def complete_payment(payment_id: int, user: dict[str, Any] = Depends(auth_dependency)):
        if user['user_type'] != 'AGENT':
            raise HTTPException(status_code=403, detail='Only the assigned agent can complete payments.')
        with get_connection() as connection, connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT p.*, f.agent_user_id FROM payments p JOIN facilities f USING (facility_id)
                WHERE payment_id = %s FOR UPDATE OF p
                """,
                (payment_id,),
            )
            payment = _row(cursor)
            if not payment:
                raise HTTPException(status_code=404, detail='Payment not found.')
            if payment['agent_user_id'] != user['user_id'] or payment['receiver_user_id'] != user['user_id']:
                raise HTTPException(status_code=403, detail='Payment is not assigned to your agency.')
            if payment['status'] != 'PENDING':
                raise HTTPException(status_code=409, detail='Only pending payments can be completed.')
            if not payment['repayment_schedule_id']:
                raise HTTPException(status_code=409, detail='Payment is not linked to a repayment schedule.')
            cursor.execute(
                'SELECT * FROM repayment_schedules WHERE schedule_id = %s FOR UPDATE',
                (payment['repayment_schedule_id'],),
            )
            schedule = _row(cursor)
            if not schedule:
                raise HTTPException(status_code=404, detail='Repayment schedule not found.')
            if schedule['status'] == 'PAID':
                raise HTTPException(status_code=409, detail='Repayment is already paid.')
            cursor.execute(
                """
                SELECT COALESCE(SUM(pa.allocated_amount), 0) AS allocated
                FROM payment_allocations pa JOIN payments p USING (payment_id)
                WHERE p.repayment_schedule_id = %s AND p.status = 'COMPLETED'
                  AND pa.allocation_type = 'PRINCIPAL'
                """,
                (schedule['schedule_id'],),
            )
            prior_principal = _decimal(cursor.fetchone()['allocated'])
            principal_due = max(_decimal(schedule['principal_due']) - prior_principal, Decimal('0'))
            principal_paid = min(_decimal(payment['payment_amount']), principal_due)
            remainder = _decimal(payment['payment_amount']) - principal_paid
            cursor.execute(
                """
                SELECT COALESCE(SUM(pa.allocated_amount), 0) AS allocated
                FROM payment_allocations pa JOIN payments p USING (payment_id)
                WHERE p.repayment_schedule_id = %s AND p.status = 'COMPLETED'
                  AND pa.allocation_type = 'INTEREST'
                """,
                (schedule['schedule_id'],),
            )
            prior_interest = _decimal(cursor.fetchone()['allocated'])
            interest_due = max(_decimal(schedule['interest_due']) - prior_interest, Decimal('0'))
            interest_paid = min(remainder, interest_due)
            fee_paid = remainder - interest_paid
            cursor.execute('SELECT participant_id, commitment_amount FROM facility_participants WHERE facility_id = %s AND status = %s ORDER BY participant_id FOR UPDATE', (payment['facility_id'], 'ACTIVE'))
            participants = [(row['participant_id'], _decimal(row['commitment_amount'])) for row in cursor.fetchall()]
            if not participants:
                raise HTTPException(status_code=409, detail='No active bank participants are available for payment allocation.')
            for allocation_type, total in [('PRINCIPAL', principal_paid), ('INTEREST', interest_paid), ('FEE', fee_paid)]:
                if total <= 0:
                    continue
                for participant_id, amount in pro_rata_allocations(total, participants):
                    cursor.execute(
                        """
                        INSERT INTO payment_allocations (payment_id, participant_id, allocation_type, allocated_amount)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (payment_id, participant_id, allocation_type, amount),
                    )
            cursor.execute("UPDATE payments SET status = 'COMPLETED' WHERE payment_id = %s", (payment_id,))
            cursor.execute(
                """
                INSERT INTO ledger_entries (facility_id, transaction_type, reference_id, transaction_date,
                    debit_amount, credit_amount, description)
                VALUES (%s, 'REPAYMENT', %s, %s, 0, %s, %s)
                """,
                (payment['facility_id'], payment_id, payment['payment_date'], payment['payment_amount'], f'Payment {payment_id} completed'),
            )
            cursor.execute(
                """
                SELECT COALESCE(SUM(payment_amount), 0) AS paid
                FROM payments
                WHERE repayment_schedule_id = %s AND status = 'COMPLETED'
                """,
                (schedule['schedule_id'],),
            )
            paid = _decimal(cursor.fetchone()['paid'])
            new_status = 'PAID' if paid >= _decimal(schedule['total_due']) else 'PARTIALLY_PAID'
            cursor.execute('UPDATE repayment_schedules SET status = %s WHERE schedule_id = %s', (new_status, schedule['schedule_id']))
            cursor.execute(
                """
                SELECT COALESCE(SUM(interest_amount), 0) AS accrued
                FROM interest_accruals WHERE facility_id = %s
                """,
                (payment['facility_id'],),
            )
            total_accrued_interest = _decimal(cursor.fetchone()['accrued'])
            cursor.execute(
                """
                SELECT COALESCE(SUM(pa.allocated_amount), 0) AS paid
                FROM payment_allocations pa JOIN payments p USING (payment_id)
                WHERE p.facility_id = %s AND p.status = 'COMPLETED'
                  AND pa.allocation_type = 'INTEREST'
                """,
                (payment['facility_id'],),
            )
            interest_paid = _decimal(cursor.fetchone()['paid'])
            if interest_paid >= total_accrued_interest:
                cursor.execute(
                    "UPDATE interest_accruals SET status = 'PAID' WHERE facility_id = %s AND status IN ('ACCRUED', 'DUE')",
                    (payment['facility_id'],),
                )
            if new_status == 'PAID':
                cursor.execute(
                    "UPDATE drawdowns SET status = 'REPAID' WHERE facility_id = %s AND status = 'DISBURSED'",
                    (payment['facility_id'],),
                )
                cursor.execute(
                    "UPDATE interest_accruals SET status = 'PAID' WHERE facility_id = %s AND status IN ('ACCRUED', 'DUE')",
                    (payment['facility_id'],),
                )
            return {
                'payment_id': payment_id,
                'status': 'COMPLETED',
                'schedule_status': new_status,
                'allocations': {
                    'principal': str(principal_paid),
                    'interest': str(interest_paid),
                    'fee': str(fee_paid),
                },
            }

    @router.get('/api/bank/{user_id}/overview')
    def bank_overview(user_id: int, user: dict[str, Any] = Depends(auth_dependency)):
        _require_user(user, user_id, 'BANK')
        with get_connection() as connection, connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT COUNT(*) AS count, COALESCE(SUM(commitment_amount), 0) AS total
                FROM facility_participants WHERE bank_user_id = %s AND status = 'ACTIVE'
                """,
                (user_id,),
            )
            committed = cursor.fetchone()
            cursor.execute(
                """
                SELECT COUNT(*) AS count, COALESCE(SUM(da.allocated_amount), 0) AS total
                FROM drawdown_allocations da JOIN facility_participants fp USING (participant_id)
                JOIN drawdowns d USING (drawdown_id)
                WHERE fp.bank_user_id = %s AND d.status IN ('DISBURSED', 'REPAID')
                """,
                (user_id,),
            )
            funded = cursor.fetchone()
            cursor.execute(
                """
                SELECT COUNT(*) AS count, COALESCE(SUM(ia.interest_amount * COALESCE(fp.commitment_percentage, 0) / 100), 0) AS total
                FROM interest_accruals ia JOIN facility_participants fp USING (facility_id)
                WHERE fp.bank_user_id = %s AND fp.status = 'ACTIVE' AND ia.status IN ('ACCRUED', 'DUE')
                """,
                (user_id,),
            )
            interest = cursor.fetchone()
            cursor.execute(
                """
                SELECT COUNT(*) AS count FROM facilities f
                WHERE f.status = 'PENDING' AND NOT EXISTS
                  (SELECT 1 FROM facility_participants fp WHERE fp.facility_id = f.facility_id AND fp.bank_user_id = %s)
                """,
                (user_id,),
            )
            opportunities = cursor.fetchone()['count']
            cursor.execute("SELECT COUNT(*) AS count FROM facility_participants WHERE bank_user_id = %s AND status = 'PROPOSED'", (user_id,))
            proposals = cursor.fetchone()['count']
            return {
                'user': {
                    'user_id': user['user_id'],
                    'username': user['username'],
                    'user_type': user['user_type'],
                    'organization_name': user['organization_name'],
                },
                'total_committed': str(_decimal(committed['total'])),
                'total_funded': str(_decimal(funded['total'])),
                'accrued_interest_share': str(_decimal(interest['total'])),
                'open_opportunities': opportunities,
                'pending_proposals': proposals,
            }

    @router.get('/api/bank/{user_id}/opportunities')
    def bank_opportunities(user_id: int, user: dict[str, Any] = Depends(auth_dependency)):
        _require_user(user, user_id, 'BANK')
        with get_connection() as connection, connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT f.facility_id, f.facility_name, f.facility_amount, f.currency, f.start_date,
                       f.maturity_date, c.organization_name AS company_name,
                       lt.reference_rate, lt.spread,
                       obs.rate AS latest_reference_rate,
                       f.facility_amount - COALESCE((
                           SELECT SUM(fp.commitment_amount) FROM facility_participants fp
                           WHERE fp.facility_id = f.facility_id AND fp.status IN ('PROPOSED', 'ACTIVE')
                       ), 0) AS uncommitted
                FROM facilities f JOIN users c ON c.user_id = f.company_user_id
                LEFT JOIN LATERAL (
                    SELECT reference_rate, spread FROM loan_rate_terms
                    WHERE facility_id = f.facility_id ORDER BY effective_from DESC LIMIT 1
                ) lt ON TRUE
                LEFT JOIN LATERAL (
                    SELECT rate FROM reference_rate_observations
                    WHERE reference_rate = lt.reference_rate
                    ORDER BY observation_date DESC LIMIT 1
                ) obs ON TRUE
                WHERE f.status = 'PENDING'
                  AND NOT EXISTS (SELECT 1 FROM facility_participants fp
                                  WHERE fp.facility_id = f.facility_id AND fp.bank_user_id = %s)
                ORDER BY f.created_at DESC
                """,
                (user_id,),
            )
            return _rows(cursor)

    @router.post('/api/facilities/{facility_id}/participants', status_code=201)
    def create_proposal(facility_id: int, body: ProposalCreate, user: dict[str, Any] = Depends(auth_dependency)):
        if user['user_type'] != 'BANK':
            raise HTTPException(status_code=403, detail='Only banks can propose facility participation.')
        with get_connection() as connection, connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute('SELECT * FROM facilities WHERE facility_id = %s FOR UPDATE', (facility_id,))
            facility = _row(cursor)
            if not facility:
                raise HTTPException(status_code=404, detail='Facility not found.')
            if facility['status'] != 'PENDING':
                raise HTTPException(status_code=409, detail='Proposals are only allowed on pending facilities.')
            cursor.execute('SELECT 1 FROM facility_participants WHERE facility_id = %s AND bank_user_id = %s', (facility_id, user['user_id']))
            if cursor.fetchone():
                raise HTTPException(status_code=409, detail='Your bank already has a proposal for this facility.')
            uncommitted = _uncommitted(cursor, facility_id, _decimal(facility['facility_amount']))
            if body.commitment_amount > uncommitted:
                raise HTTPException(status_code=400, detail='Commitment exceeds the remaining facility amount.')
            commitment_percentage = (
                _decimal(body.commitment_amount) / _decimal(facility['facility_amount']) * Decimal('100')
            )
            cursor.execute(
                """
                INSERT INTO facility_participants
                    (facility_id, bank_user_id, commitment_amount, commitment_percentage, offered_spread, status)
                VALUES (%s, %s, %s, %s, %s, 'PROPOSED')
                RETURNING participant_id, facility_id, bank_user_id, commitment_amount,
                          commitment_percentage, offered_spread, status
                """,
                (
                    facility_id, user['user_id'], body.commitment_amount,
                    commitment_percentage, body.offered_spread,
                ),
            )
            return _row(cursor)

    @router.patch('/api/participants/{participant_id}/withdraw')
    def withdraw_proposal(participant_id: int, user: dict[str, Any] = Depends(auth_dependency)):
        if user['user_type'] != 'BANK':
            raise HTTPException(status_code=403, detail='Only the proposing bank can withdraw.')
        with get_connection() as connection, connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute('SELECT bank_user_id, status FROM facility_participants WHERE participant_id = %s FOR UPDATE', (participant_id,))
            proposal = _row(cursor)
            if not proposal:
                raise HTTPException(status_code=404, detail='Proposal not found.')
            if proposal['bank_user_id'] != user['user_id']:
                raise HTTPException(status_code=403, detail='You do not own this proposal.')
            if proposal['status'] != 'PROPOSED':
                raise HTTPException(status_code=409, detail='Only proposed participations can be withdrawn.')
            cursor.execute("UPDATE facility_participants SET status = 'REJECTED' WHERE participant_id = %s RETURNING participant_id, status", (participant_id,))
            return _row(cursor)

    @router.get('/api/bank/{user_id}/participations')
    def bank_participations(user_id: int, user: dict[str, Any] = Depends(auth_dependency)):
        _require_user(user, user_id, 'BANK')
        with get_connection() as connection, connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT fp.participant_id, fp.facility_id, fp.commitment_amount,
                       fp.commitment_percentage, fp.offered_spread, fp.status,
                       f.facility_name, f.facility_amount, f.currency, f.start_date, f.maturity_date
                FROM facility_participants fp JOIN facilities f USING (facility_id)
                WHERE fp.bank_user_id = %s ORDER BY fp.joined_at DESC NULLS LAST
                """,
                (user_id,),
            )
            return _rows(cursor)

    @router.get('/api/bank/{user_id}/portfolio')
    def bank_portfolio(user_id: int, user: dict[str, Any] = Depends(auth_dependency)):
        _require_user(user, user_id, 'BANK')
        with get_connection() as connection, connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT f.facility_id, f.facility_name, f.facility_amount, f.currency,
                       f.maturity_date, fp.commitment_amount, fp.commitment_percentage,
                       COALESCE((SELECT SUM(da.allocated_amount) FROM drawdown_allocations da
                         JOIN facility_participants p USING (participant_id)
                         JOIN drawdowns d USING (drawdown_id)
                         WHERE p.participant_id = fp.participant_id AND d.status IN ('DISBURSED', 'REPAID')), 0) AS funded,
                       COALESCE((SELECT SUM(ia.interest_amount * fp.commitment_percentage / 100)
                         FROM interest_accruals ia WHERE ia.facility_id = f.facility_id
                         AND ia.status IN ('ACCRUED', 'DUE')), 0) AS accrued_interest
                FROM facility_participants fp JOIN facilities f USING (facility_id)
                WHERE fp.bank_user_id = %s AND fp.status = 'ACTIVE' AND f.status = 'ACTIVE'
                ORDER BY f.maturity_date
                """,
                (user_id,),
            )
            return _rows(cursor)

    @router.get('/api/bank/{user_id}/payments')
    def bank_payments(user_id: int, user: dict[str, Any] = Depends(auth_dependency)):
        _require_user(user, user_id, 'BANK')
        with get_connection() as connection, connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT p.payment_id, p.facility_id, p.payment_amount, p.payment_date,
                       p.currency, p.payment_reference, p.status,
                       COALESCE(json_agg(json_build_object('type', pa.allocation_type, 'amount', pa.allocated_amount))
                         FILTER (WHERE pa.payment_allocation_id IS NOT NULL), '[]') AS allocations
                FROM payments p JOIN facility_participants fp ON fp.facility_id = p.facility_id
                LEFT JOIN payment_allocations pa ON pa.payment_id = p.payment_id AND pa.participant_id = fp.participant_id
                WHERE fp.bank_user_id = %s AND p.status = 'COMPLETED'
                GROUP BY p.payment_id ORDER BY p.payment_date DESC
                """,
                (user_id,),
            )
            return _rows(cursor)

    @router.get('/api/facilities/{facility_id}')
    def facility_detail(facility_id: int, user: dict[str, Any] = Depends(auth_dependency)):
        with get_connection() as connection, connection.cursor(cursor_factory=RealDictCursor) as cursor:
            facility = _facility_for_user(cursor, facility_id, user)
            cursor.execute(
                """
                SELECT f.*, c.organization_name AS company_name, a.organization_name AS agent_name
                FROM facilities f JOIN users c ON c.user_id = f.company_user_id
                LEFT JOIN users a ON a.user_id = f.agent_user_id
                WHERE f.facility_id = %s
                """,
                (facility_id,),
            )
            facility = _row(cursor)
            cursor.execute(
                """
                SELECT fp.participant_id, u.organization_name AS bank_name, fp.commitment_amount,
                       fp.commitment_percentage, fp.offered_spread, fp.status
                FROM facility_participants fp JOIN users u ON u.user_id = fp.bank_user_id
                WHERE fp.facility_id = %s ORDER BY fp.participant_id
                """,
                (facility_id,),
            )
            participants = _rows(cursor)
            cursor.execute(
                'SELECT reference_rate, spread, day_count_convention, effective_from, effective_to FROM loan_rate_terms WHERE facility_id = %s ORDER BY effective_from DESC',
                (facility_id,),
            )
            terms = _rows(cursor)
            cursor.execute(
                'SELECT drawdown_id, drawdown_amount, drawdown_date, currency, status FROM drawdowns WHERE facility_id = %s ORDER BY drawdown_date DESC',
                (facility_id,),
            )
            drawdowns = _rows(cursor)
            cursor.execute(
                """
                SELECT ia.accrual_id, ia.drawdown_id, ia.accrual_date, ia.principal_amount,
                       ia.reference_rate, ia.spread, ia.effective_rate, ia.interest_amount,
                       ia.day_count_convention, ia.status
                FROM interest_accruals ia WHERE facility_id = %s ORDER BY accrual_date DESC LIMIT 500
                """,
                (facility_id,),
            )
            accruals = _rows(cursor)
            cursor.execute(
                """
                SELECT observation_date, rate FROM reference_rate_observations
                WHERE reference_rate = %s ORDER BY observation_date DESC LIMIT 1
                """,
                (terms[0]['reference_rate'],) if terms else ('SOFR',),
            )
            latest_rate = _row(cursor)
            return {
                'facility': facility,
                'participants': participants,
                'rate_terms': terms,
                'drawdowns': drawdowns,
                'interest_accruals': accruals,
                'latest_observation': latest_rate,
            }

    @router.post('/api/facilities/{facility_id}/drawdowns', status_code=201)
    def request_drawdown(facility_id: int, body: DrawdownCreate, user: dict[str, Any] = Depends(auth_dependency)):
        if user['user_type'] != 'COMPANY':
            raise HTTPException(status_code=403, detail='Only the company can request a drawdown.')
        with get_connection() as connection, connection.cursor(cursor_factory=RealDictCursor) as cursor:
            facility = _facility_for_user(cursor, facility_id, user, lock=True)
            if facility['status'] != 'ACTIVE':
                raise HTTPException(status_code=409, detail='Drawdowns require an active facility.')
            if not _date_value(facility['start_date']) <= body.drawdown_date <= _date_value(facility['maturity_date']):
                raise HTTPException(status_code=400, detail='Drawdown date must be within the facility term.')
            available = _undrawn(cursor, facility_id, _decimal(facility['facility_amount']))
            try:
                validate_drawdown(body.drawdown_amount, available)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            cursor.execute(
                """
                INSERT INTO drawdowns (facility_id, requested_by_user_id, drawdown_amount, drawdown_date, currency, status)
                VALUES (%s, %s, %s, %s, %s, 'REQUESTED')
                RETURNING drawdown_id, facility_id, drawdown_amount, drawdown_date, currency, status
                """,
                (facility_id, user['user_id'], body.drawdown_amount, body.drawdown_date, facility['currency']),
            )
            return _row(cursor)

    @router.get('/api/agent/{user_id}/facilities/{facility_id}/commitments')
    def commitment_preview(user_id: int, facility_id: int, user: dict[str, Any] = Depends(auth_dependency)):
        _require_user(user, user_id, 'AGENT')
        with get_connection() as connection, connection.cursor(cursor_factory=RealDictCursor) as cursor:
            facility = _facility_for_user(cursor, facility_id, user)
            cursor.execute('SELECT commitment_amount, offered_spread FROM facility_participants WHERE facility_id = %s AND status = %s', (facility_id, 'ACTIVE'))
            commitments = [(_decimal(row['commitment_amount']), _decimal(row['offered_spread'])) for row in cursor.fetchall()]
            return {
                'committed': str(sum((amount for amount, _ in commitments), Decimal('0'))),
                'required': str(facility['facility_amount']),
                'weighted_average_spread': str(weighted_average_spread(commitments)) if commitments else None,
            }

    return router


def create_workflow_router(auth_dependency: Callable[..., Any]) -> APIRouter:
    return _make_router(auth_dependency)
