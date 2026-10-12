from __future__ import annotations

import logging
import os
import hashlib
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote

import bcrypt
import jwt
import psycopg2
from psycopg2 import errorcodes
import requests
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from psycopg2.extras import RealDictCursor
from starlette.datastructures import UploadFile as StarletteUploadFile

from database import get_connection
from services.interest_service import calculate_active_loan, calculate_historical_loan
from services.sofr_service import fetch_historical_rates as fetch_sofr_rates
from services.sonia_service import fetch_historical_rates as fetch_sonia_rates
from routers.workflow import create_workflow_router

load_dotenv(Path(__file__).with_name('.env'))

JWT_SECRET_KEY = os.getenv('JWT_SECRET_KEY') or os.getenv('JWT_SECRET')
if not JWT_SECRET_KEY:
    raise RuntimeError('JWT_SECRET_KEY must be configured in the backend environment. Set JWT_SECRET_KEY or JWT_SECRET in the backend .env file.')
JWT_ALGORITHM = 'HS256'
MAX_VERIFICATION_DOCUMENT_SIZE = 10 * 1024 * 1024
ALLOWED_DOCUMENT_TYPES = {
    '.pdf': ('application/pdf',),
    '.png': ('image/png',),
    '.jpg': ('image/jpeg',),
    '.jpeg': ('image/jpeg',),
}

app = FastAPI(title='ARRA Platform API', version='0.2.0')
logger = logging.getLogger(__name__)
CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        'CORS_ORIGINS',
        'http://localhost:5173,http://127.0.0.1:5173,https://bank-frontend-kappa.vercel.app,https://bank-frontend-v2.vercel.app',
    ).split(',')
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=['*'],
    allow_headers=['*'],
)

class LoanRequest(BaseModel):
    principal: float = Field(..., gt=0)
    reference_rate: str = Field(default='SOFR')
    spread: float = Field(default=2.0, ge=0)
    start_date: str
    end_date: str | None = None
    day_count: str = Field(default='ACT/360')
    as_of_date: str | None = None


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=120)
    password: str = Field(..., min_length=8, max_length=200)


class VerificationReviewRequest(BaseModel):
    status: str = Field(..., pattern=r'^(APPROVED|REJECTED)$')
    notes: str | None = Field(default=None, max_length=4000)

    @model_validator(mode='after')
    def require_rejection_notes(self):
        if self.status == 'REJECTED' and not (self.notes or '').strip():
            raise ValueError('Review notes are required when rejecting a signup.')
        return self


class CompanySignupProfile(BaseModel):
    legal_name: str = Field(min_length=2, max_length=200)
    registration_number: str = Field(min_length=1, max_length=100)
    country: str = Field(min_length=2, max_length=100)
    business_address: str = Field(min_length=5, max_length=1000)
    contact_email: str = Field(pattern=r'^[^@\s]+@[^@\s]+\.[^@\s]+$', max_length=254)
    contact_phone: str = Field(min_length=5, max_length=40)
    website: str | None = Field(default=None, max_length=500)
    industry: str = Field(min_length=2, max_length=120)
    incorporated_on: date
    annual_revenue: Decimal | None = Field(default=None, ge=0, max_digits=20, decimal_places=2)
    revenue_currency: str = Field(pattern=r'^[A-Z]{3}$')
    employee_count: int | None = Field(default=None, ge=0)


class BankSignupProfile(BaseModel):
    legal_name: str = Field(min_length=2, max_length=200)
    registration_number: str = Field(min_length=1, max_length=100)
    country: str = Field(min_length=2, max_length=100)
    business_address: str = Field(min_length=5, max_length=1000)
    contact_email: str = Field(pattern=r'^[^@\s]+@[^@\s]+\.[^@\s]+$', max_length=254)
    contact_phone: str = Field(min_length=5, max_length=40)
    website: str | None = Field(default=None, max_length=500)
    banking_license_number: str = Field(min_length=1, max_length=150)
    regulator: str = Field(min_length=2, max_length=200)
    bank_type: str = Field(min_length=2, max_length=100)
    swift_bic: str = Field(min_length=8, max_length=11)
    total_assets: Decimal | None = Field(default=None, ge=0, max_digits=20, decimal_places=2)
    assets_currency: str = Field(pattern=r'^[A-Z]{3}$')

    @field_validator('swift_bic')
    @classmethod
    def validate_swift_bic(cls, value: str) -> str:
        if not re.fullmatch(r'[A-Za-z0-9]{8}(?:[A-Za-z0-9]{3})?', value):
            raise ValueError('SWIFT/BIC must be 8 or 11 letters and numbers, with no spaces.')
        return value.upper()


@app.get('/health')
def health_check() -> dict:
    return {'status': 'ok'}


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')


def _verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode('utf-8'), password_hash.encode('utf-8'))
    except ValueError:
        return False


def _create_token(user: dict[str, Any]) -> str:
    payload = {
        'user_id': user['user_id'],
        'username': user['username'],
        'user_type': user['user_type'],
        'exp': datetime.now(timezone.utc) + timedelta(hours=8),
    }
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


async def _get_authenticated_user(request: Request) -> dict[str, Any]:
    auth_header = request.headers.get('authorization', '')
    if not auth_header.startswith('Bearer '):
        raise HTTPException(status_code=401, detail='Authentication token missing or invalid.')

    token = auth_header.replace('Bearer ', '', 1).strip()
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError as exc:  # pragma: no cover - runtime guard
        raise HTTPException(status_code=401, detail='Authentication token is invalid or expired.') from exc

    user_id = payload.get('user_id')
    username = payload.get('username')
    user_type = payload.get('user_type')
    if user_id is None or username is None or user_type is None:
        raise HTTPException(status_code=401, detail='Authentication token is malformed.')

    try:
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    'SELECT user_id, username, user_type, organization_name, verification_status FROM users WHERE user_id = %s',
                    (user_id,),
                )
                row = cursor.fetchone()
    except (RuntimeError, psycopg2.Error) as exc:
        raise HTTPException(status_code=503, detail='Database connection unavailable.') from exc

    if row is None:
        raise HTTPException(status_code=401, detail='Authenticated user no longer exists.')

    db_user = {
        'user_id': row[0],
        'username': row[1],
        'user_type': row[2],
        'organization_name': row[3],
        'verification_status': row[4],
    }

    if db_user['user_type'] != user_type or db_user['username'] != username:
        raise HTTPException(status_code=401, detail='Token does not match the current user record.')
    if db_user['verification_status'] != 'APPROVED':
        raise HTTPException(status_code=403, detail='This account is not approved for access.')

    return db_user


app.include_router(create_workflow_router(_get_authenticated_user))


@app.post('/api/auth/login')
def login(request: LoginRequest):
    username = request.username.strip()
    password = request.password

    try:
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    'SELECT user_id, username, password_hash, user_type, organization_name, verification_status, verification_notes FROM users WHERE username = %s',
                    (username,),
                )
                row = cursor.fetchone()
    except (RuntimeError, psycopg2.Error) as exc:
        raise HTTPException(status_code=503, detail='Database connection unavailable.') from exc

    if row is None:
        raise HTTPException(status_code=401, detail='Invalid username or password.')

    user_id, stored_username, stored_hash, user_type, organization_name, verification_status, verification_notes = row
    if not stored_hash or not _verify_password(password, stored_hash):
        raise HTTPException(status_code=401, detail='Invalid username or password.')
    if verification_status != 'APPROVED':
        if verification_status == 'REJECTED':
            detail = 'Your registration was rejected.'
            if verification_notes:
                detail = f'{detail} Review notes: {verification_notes}'
        else:
            detail = 'Your account is awaiting verification by the ARRA Agent.'
        raise HTTPException(status_code=403, detail=detail)

    user = {
        'user_id': user_id,
        'username': stored_username,
        'user_type': user_type,
        'organization_name': organization_name,
    }

    return {
        'token': _create_token(user),
        'user': user,
    }


def _form_value(form, name: str, *, required: bool = True) -> str | None:
    value = form.get(name)
    if not isinstance(value, str):
        if required:
            raise HTTPException(status_code=422, detail=f'{name.replace("_", " ").capitalize()} is required.')
        return None
    value = value.strip()
    if required and not value:
        raise HTTPException(status_code=422, detail=f'{name.replace("_", " ").capitalize()} is required.')
    return value or None


def _document_content(filename: str, declared_type: str, content: bytes) -> str:
    extension = Path(filename).suffix.lower()
    allowed_types = ALLOWED_DOCUMENT_TYPES.get(extension)
    if not allowed_types or declared_type not in allowed_types:
        raise HTTPException(status_code=400, detail='Upload a PDF, PNG, or JPEG verification document.')
    if extension == '.pdf' and not content.startswith(b'%PDF-'):
        raise HTTPException(status_code=400, detail='The uploaded file is not a valid PDF document.')
    if extension == '.png' and not content.startswith(b'\x89PNG\r\n\x1a\n'):
        raise HTTPException(status_code=400, detail='The uploaded file is not a valid PNG image.')
    if extension in {'.jpg', '.jpeg'} and not content.startswith(b'\xff\xd8\xff'):
        raise HTTPException(status_code=400, detail='The uploaded file is not a valid JPEG image.')
    return allowed_types[0]


def _require_strong_password(password: str) -> None:
    if len(password) < 8:
        raise HTTPException(status_code=400, detail='Password must be at least 8 characters long.')
    if len(password.encode('utf-8')) > 72:
        raise HTTPException(status_code=400, detail='Password must be no more than 72 bytes for bcrypt.')
    if not any(character.isupper() for character in password):
        raise HTTPException(status_code=400, detail='Password must include at least one uppercase letter.')
    if not any(character.islower() for character in password):
        raise HTTPException(status_code=400, detail='Password must include at least one lowercase letter.')
    if not any(character.isdigit() for character in password):
        raise HTTPException(status_code=400, detail='Password must include at least one number.')
    if not any(not character.isalnum() for character in password):
        raise HTTPException(status_code=400, detail='Password must include at least one special character.')


@app.post('/api/auth/signup', status_code=201)
async def signup(request: Request):
    form = await request.form(max_files=1, max_fields=20)
    username = _form_value(form, 'username')
    password = _form_value(form, 'password')
    confirm_password = _form_value(form, 'confirm_password')
    user_type = (_form_value(form, 'user_type') or '').upper()
    if user_type not in {'COMPANY', 'BANK'}:
        raise HTTPException(status_code=400, detail='Choose either COMPANY or BANK to create an account.')
    if len(username) < 3 or len(username) > 120:
        raise HTTPException(status_code=400, detail='Username must be between 3 and 120 characters.')
    if password != confirm_password:
        raise HTTPException(status_code=400, detail='Passwords do not match.')
    _require_strong_password(password)

    profile_fields = {
        'legal_name': _form_value(form, 'legal_name'),
        'registration_number': _form_value(form, 'registration_number'),
        'country': _form_value(form, 'country'),
        'business_address': _form_value(form, 'business_address'),
        'contact_email': _form_value(form, 'contact_email'),
        'contact_phone': _form_value(form, 'contact_phone'),
        'website': _form_value(form, 'website', required=False),
    }
    if user_type == 'COMPANY':
        profile_fields.update({
            'industry': _form_value(form, 'industry'),
            'incorporated_on': _form_value(form, 'incorporated_on'),
            'annual_revenue': _form_value(form, 'annual_revenue', required=False),
            'revenue_currency': _form_value(form, 'revenue_currency'),
            'employee_count': _form_value(form, 'employee_count', required=False),
        })
    else:
        profile_fields.update({
            'banking_license_number': _form_value(form, 'banking_license_number'),
            'regulator': _form_value(form, 'regulator'),
            'bank_type': _form_value(form, 'bank_type'),
            'swift_bic': _form_value(form, 'swift_bic'),
            'total_assets': _form_value(form, 'total_assets', required=False),
            'assets_currency': _form_value(form, 'assets_currency'),
        })
    try:
        profile_model = CompanySignupProfile if user_type == 'COMPANY' else BankSignupProfile
        profile_fields = profile_model.model_validate(profile_fields).model_dump()
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail=exc.errors(include_context=False, include_input=False),
        ) from exc

    document = form.get('document')
    if not isinstance(document, StarletteUploadFile) or not document.filename:
        raise HTTPException(status_code=422, detail='Upload one verification document.')
    filename = Path(document.filename.replace('\\', '/')).name
    if len(filename) > 255:
        raise HTTPException(status_code=400, detail='The document filename must be 255 characters or fewer.')
    content = await document.read(MAX_VERIFICATION_DOCUMENT_SIZE + 1)
    if not content:
        raise HTTPException(status_code=400, detail='The verification document is empty.')
    if len(content) > MAX_VERIFICATION_DOCUMENT_SIZE:
        raise HTTPException(status_code=413, detail='The verification document must be 10 MB or smaller.')
    content_type = _document_content(filename, document.content_type or '', content)
    organization_name = profile_fields['legal_name']

    try:
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute('SELECT 1 FROM users WHERE username = %s', (username,))
                if cursor.fetchone():
                    raise HTTPException(status_code=409, detail='Username already exists.')

                cursor.execute(
                    """INSERT INTO users
                       (username, password_hash, user_type, organization_name, verification_status)
                       VALUES (%s, %s, %s, %s, 'PENDING') RETURNING user_id""",
                    (username, _hash_password(password), user_type, organization_name),
                )
                user_id = cursor.fetchone()[0]

                if user_type == 'COMPANY':
                    cursor.execute(
                        """INSERT INTO company_profiles
                           (user_id, legal_name, registration_number, country, industry, incorporated_on,
                            business_address, contact_email, contact_phone, website, annual_revenue,
                            revenue_currency, employee_count)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (
                            user_id, profile_fields['legal_name'], profile_fields['registration_number'],
                            profile_fields['country'], profile_fields['industry'], profile_fields['incorporated_on'],
                            profile_fields['business_address'], profile_fields['contact_email'],
                            profile_fields['contact_phone'], profile_fields['website'], profile_fields['annual_revenue'],
                            profile_fields['revenue_currency'], profile_fields['employee_count'],
                        ),
                    )
                else:
                    cursor.execute(
                        """INSERT INTO bank_profiles
                           (user_id, legal_name, registration_number, banking_license_number, country,
                            regulator, bank_type, swift_bic, business_address, contact_email, contact_phone,
                            website, total_assets, assets_currency)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (
                            user_id, profile_fields['legal_name'], profile_fields['registration_number'],
                            profile_fields['banking_license_number'], profile_fields['country'],
                            profile_fields['regulator'], profile_fields['bank_type'], profile_fields['swift_bic'],
                            profile_fields['business_address'], profile_fields['contact_email'],
                            profile_fields['contact_phone'], profile_fields['website'], profile_fields['total_assets'],
                            profile_fields['assets_currency'],
                        ),
                    )

                cursor.execute(
                    """INSERT INTO verification_documents
                       (user_id, original_filename, content_type, file_size, sha256, file_content)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (user_id, filename, content_type, len(content), hashlib.sha256(content).hexdigest(), psycopg2.Binary(content)),
                )
    except HTTPException:
        raise
    except psycopg2.IntegrityError as exc:
        if exc.pgcode == errorcodes.UNIQUE_VIOLATION:
            raise HTTPException(status_code=409, detail='Username already exists.') from exc
        logger.exception('Database constraint rejected signup for username %s.', username)
        raise HTTPException(status_code=400, detail='Profile details could not be saved. Check the values and try again.') from exc
    except (RuntimeError, psycopg2.Error) as exc:
        logger.exception('Database error while creating account for username %s.', username)
        raise HTTPException(status_code=503, detail='Unable to create account because the database is unavailable.') from exc

    return {'message': 'Registration submitted successfully. Your account is pending Agent verification.', 'verification_status': 'PENDING'}


def _require_agent(user: dict[str, Any]) -> None:
    if user['user_type'] != 'AGENT':
        raise HTTPException(status_code=403, detail='Only an ARRA agent can review organization signups.')


def _verification_value(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


@app.get('/api/agent/verifications')
def pending_verifications(user: dict[str, Any] = Depends(_get_authenticated_user)):
    _require_agent(user)
    try:
        with get_connection() as connection, connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT u.user_id, u.username, u.user_type, u.organization_name, u.created_at,
                       d.document_id, d.original_filename, d.content_type, d.file_size, d.uploaded_at,
                       cp.registration_number AS company_registration_number, cp.country AS company_country,
                       cp.industry, cp.incorporated_on, cp.business_address AS company_address,
                       cp.contact_email AS company_email, cp.contact_phone AS company_phone, cp.website AS company_website,
                       cp.annual_revenue, cp.revenue_currency, cp.employee_count,
                       bp.registration_number AS bank_registration_number, bp.banking_license_number,
                       bp.country AS bank_country, bp.regulator, bp.bank_type, bp.swift_bic,
                       bp.business_address AS bank_address, bp.contact_email AS bank_email,
                       bp.contact_phone AS bank_phone, bp.website AS bank_website,
                       bp.total_assets, bp.assets_currency
                FROM users u
                LEFT JOIN LATERAL (
                    SELECT * FROM verification_documents
                    WHERE user_id = u.user_id
                    ORDER BY uploaded_at DESC, document_id DESC LIMIT 1
                ) d ON TRUE
                LEFT JOIN company_profiles cp ON cp.user_id = u.user_id
                LEFT JOIN bank_profiles bp ON bp.user_id = u.user_id
                WHERE u.verification_status = 'PENDING'
                  AND u.user_type IN ('COMPANY', 'BANK')
                ORDER BY u.created_at, u.user_id
                """
            )
            return [
                {key: _verification_value(value) for key, value in row.items()}
                for row in cursor.fetchall()
            ]
    except (RuntimeError, psycopg2.Error) as exc:
        logger.exception('Unable to load pending organization verifications.')
        raise HTTPException(status_code=503, detail='Unable to load verification requests.') from exc


@app.get('/api/agent/verifications/{user_id}/documents/{document_id}')
def get_verification_document(
    user_id: int,
    document_id: int,
    user: dict[str, Any] = Depends(_get_authenticated_user),
):
    _require_agent(user)
    try:
        with get_connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT original_filename, content_type, file_content
                   FROM verification_documents
                   WHERE user_id = %s AND document_id = %s""",
                (user_id, document_id),
            )
            row = cursor.fetchone()
    except (RuntimeError, psycopg2.Error) as exc:
        logger.exception('Unable to retrieve verification document %s for user %s.', document_id, user_id)
        raise HTTPException(status_code=503, detail='Unable to retrieve the verification document.') from exc
    if row is None:
        raise HTTPException(status_code=404, detail='Verification document not found.')
    filename, content_type, content = row
    return Response(
        content=bytes(content),
        media_type=content_type,
        headers={'Content-Disposition': f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@app.post('/api/agent/verifications/{user_id}/review')
def review_verification(
    user_id: int,
    request: VerificationReviewRequest,
    user: dict[str, Any] = Depends(_get_authenticated_user),
):
    _require_agent(user)
    if user_id == user['user_id']:
        raise HTTPException(status_code=400, detail='An agent cannot review their own account.')
    try:
        with get_connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """UPDATE users
                   SET verification_status = %s, verification_notes = %s,
                       reviewed_at = NOW(), reviewed_by = %s
                   WHERE user_id = %s AND user_type IN ('COMPANY', 'BANK')
                     AND verification_status = 'PENDING'
                   RETURNING user_id""",
                (request.status, request.notes, user['user_id'], user_id),
            )
            if cursor.fetchone() is None:
                raise HTTPException(status_code=404, detail='Pending organization signup not found.')
    except HTTPException:
        raise
    except (RuntimeError, psycopg2.Error) as exc:
        logger.exception('Unable to review organization signup for user %s.', user_id)
        raise HTTPException(status_code=503, detail='Unable to save the verification decision.') from exc
    return {'user_id': user_id, 'verification_status': request.status}


def _date_text(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _amount_text(value: Any) -> str:
    return f'{Decimal(value):,.2f}' if value is not None else 'No data available'


def _facility_ids(connection, role: str, user_id: int) -> list[int]:
    with connection.cursor() as cursor:
        if role == 'COMPANY':
            cursor.execute('SELECT facility_id FROM facilities WHERE company_user_id = %s', (user_id,))
        elif role == 'AGENT':
            cursor.execute('SELECT facility_id FROM facilities WHERE agent_user_id = %s', (user_id,))
        elif role == 'BANK':
            cursor.execute(
                'SELECT facility_id FROM facility_participants WHERE bank_user_id = %s',
                (user_id,),
            )
        else:
            raise HTTPException(status_code=403, detail='Unsupported account type.')
        return [row[0] for row in cursor.fetchall()]


def _dashboard_facilities(connection, role: str, user_id: int) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        if role == 'COMPANY':
            cursor.execute(
                """
                SELECT f.facility_id, f.facility_name, f.facility_amount, f.currency,
                       f.start_date, f.maturity_date, f.status, NULL::text
                FROM facilities f
                WHERE f.company_user_id = %s
                ORDER BY f.created_at DESC
                """,
                (user_id,),
            )
        elif role == 'AGENT':
            cursor.execute(
                """
                SELECT f.facility_id, f.facility_name, f.facility_amount, f.currency,
                       f.start_date, f.maturity_date, f.status, u.organization_name
                FROM facilities f
                JOIN users u ON u.user_id = f.company_user_id
                WHERE f.agent_user_id = %s
                ORDER BY f.created_at DESC
                """,
                (user_id,),
            )
        elif role == 'BANK':
            cursor.execute(
                """
                SELECT f.facility_id, f.facility_name, f.facility_amount, f.currency,
                       f.start_date, f.maturity_date, f.status, u.organization_name
                FROM facilities f
                JOIN facility_participants fp ON fp.facility_id = f.facility_id
                JOIN users u ON u.user_id = f.company_user_id
                WHERE fp.bank_user_id = %s
                ORDER BY f.created_at DESC
                """,
                (user_id,),
            )
        else:
            raise HTTPException(status_code=403, detail='Unsupported account type.')

        return [
            {
                'facility_id': row[0],
                'facility_name': row[1],
                'facility_amount': _amount_text(row[2]),
                'currency': row[3],
                'start_date': _date_text(row[4]),
                'maturity_date': _date_text(row[5]),
                'status': row[6],
                'company_name': row[7],
            }
            for row in cursor.fetchall()
        ]


def _dashboard_payments(connection, role: str, user_id: int, facility_ids: list[int]) -> list[dict[str, Any]]:
    if role == 'COMPANY':
        condition = '(p.payer_user_id = %s OR p.facility_id = ANY(%s))'
        parameters = (user_id, facility_ids)
    elif role == 'BANK':
        condition = '(p.receiver_user_id = %s OR p.facility_id = ANY(%s))'
        parameters = (user_id, facility_ids)
    else:
        condition = 'p.facility_id = ANY(%s)'
        parameters = (facility_ids,)

    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT p.payment_id, p.facility_id, p.payment_amount, p.currency,
                   p.payment_date, p.payment_reference, p.status
            FROM payments p
            WHERE {condition}
            ORDER BY p.payment_date DESC NULLS LAST, p.payment_id DESC
            LIMIT 50
            """,
            parameters,
        )
        return [
            {
                'payment_id': row[0],
                'facility_id': row[1],
                'payment_amount': _amount_text(row[2]),
                'currency': row[3],
                'payment_date': _date_text(row[4]),
                'payment_reference': row[5],
                'status': row[6],
            }
            for row in cursor.fetchall()
        ]


def _dashboard_repayments(connection, facility_ids: list[int]) -> list[dict[str, Any]]:
    if not facility_ids:
        return []
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT schedule_id, facility_id, due_date, principal_due, interest_due,
                   total_due, status
            FROM repayment_schedules
            WHERE facility_id = ANY(%s)
            ORDER BY due_date
            LIMIT 50
            """,
            (facility_ids,),
        )
        return [
            {
                'schedule_id': row[0],
                'facility_id': row[1],
                'due_date': _date_text(row[2]),
                'principal_due': _amount_text(row[3]),
                'interest_due': _amount_text(row[4]),
                'total_due': _amount_text(row[5]),
                'status': row[6],
            }
            for row in cursor.fetchall()
        ]


def _dashboard_cards(connection, role: str, user_id: int, facility_ids: list[int]) -> list[dict[str, str]]:
    def amount_card(label: str, value: Any, note: str, has_records: bool) -> dict[str, str]:
        return {
            'label': label,
            'value': _amount_text(value) if has_records else 'No data available',
            'note': note,
        }

    with connection.cursor() as cursor:
        if role == 'COMPANY':
            cursor.execute(
                'SELECT COUNT(*), COALESCE(SUM(facility_amount), 0) FROM facilities WHERE company_user_id = %s',
                (user_id,),
            )
            facility_count, total_facility = cursor.fetchone()
            if facility_ids:
                cursor.execute(
                    'SELECT COUNT(*), COALESCE(SUM(drawdown_amount), 0) FROM drawdowns WHERE facility_id = ANY(%s)',
                    (facility_ids,),
                )
                drawdown_count, total_drawn = cursor.fetchone()
                cursor.execute(
                    'SELECT COUNT(*), COALESCE(SUM(interest_amount), 0) FROM interest_accruals WHERE facility_id = ANY(%s)',
                    (facility_ids,),
                )
                accrual_count, accrued_interest = cursor.fetchone()
                cursor.execute(
                    """
                    SELECT due_date, total_due
                    FROM repayment_schedules
                    WHERE facility_id = ANY(%s) AND due_date >= CURRENT_DATE
                      AND status NOT IN ('PAID', 'CANCELLED')
                    ORDER BY due_date
                    LIMIT 1
                    """,
                    (facility_ids,),
                )
                next_repayment = cursor.fetchone()
                cursor.execute(
                    """
                    SELECT COUNT(*), COALESCE(SUM(total_due), 0)
                    FROM repayment_schedules
                    WHERE facility_id = ANY(%s) AND status NOT IN ('PAID', 'CANCELLED')
                    """,
                    (facility_ids,),
                )
                outstanding_count, outstanding = cursor.fetchone()
            else:
                drawdown_count = accrual_count = outstanding_count = 0
                total_drawn = accrued_interest = outstanding = 0
                next_repayment = None

            return [
                {'label': 'Active facilities', 'value': str(facility_count) if facility_count else 'No data available', 'note': 'Facilities belonging to your account'},
                amount_card('Total facility', total_facility, 'Approved facility amount', facility_count > 0),
                amount_card('Total drawn', total_drawn, 'Recorded facility drawdowns', drawdown_count > 0),
                amount_card('Available facility', max(total_facility - total_drawn, 0), 'Facility amount less recorded drawdowns', facility_count > 0),
                amount_card('Accrued interest', accrued_interest, 'Interest accruals on your facilities', accrual_count > 0),
                {'label': 'Next repayment', 'value': _date_text(next_repayment[0]) if next_repayment else 'No data available', 'note': _amount_text(next_repayment[1]) if next_repayment else 'No upcoming repayment'},
                amount_card('Outstanding amount', outstanding, 'Unpaid scheduled repayments', outstanding_count > 0),
            ]

        if role == 'BANK':
            cursor.execute(
                """
                SELECT COUNT(*), COALESCE(SUM(commitment_amount), 0)
                FROM facility_participants
                WHERE bank_user_id = %s
                """,
                (user_id,),
            )
            participation_count, commitment_total = cursor.fetchone()
            if facility_ids:
                cursor.execute(
                    'SELECT COUNT(*), COALESCE(SUM(interest_amount), 0) FROM interest_accruals WHERE facility_id = ANY(%s)',
                    (facility_ids,),
                )
                accrual_count, accrued_interest = cursor.fetchone()
            else:
                accrual_count, accrued_interest = 0, 0
            return [
                {'label': 'Participations', 'value': str(participation_count) if participation_count else 'No data available', 'note': 'Facilities in which your bank participates'},
                amount_card('Commitments', commitment_total, 'Your facility commitments', participation_count > 0),
                amount_card('Interest accruals', accrued_interest, 'Accrued interest in your participating facilities', accrual_count > 0),
            ]

        cursor.execute('SELECT COUNT(*) FROM facilities WHERE agent_user_id = %s', (user_id,))
        managed_count = cursor.fetchone()[0]
        if facility_ids:
            cursor.execute('SELECT COUNT(*) FROM drawdowns WHERE facility_id = ANY(%s)', (facility_ids,))
            drawdown_count = cursor.fetchone()[0]
            cursor.execute(
                """
                SELECT due_date FROM repayment_schedules
                WHERE facility_id = ANY(%s) AND due_date >= CURRENT_DATE
                  AND status NOT IN ('PAID', 'CANCELLED')
                ORDER BY due_date LIMIT 1
                """,
                (facility_ids,),
            )
            next_event = cursor.fetchone()
        else:
            drawdown_count, next_event = 0, None
        return [
            {'label': 'Managed facilities', 'value': str(managed_count) if managed_count else 'No data available', 'note': 'Facilities assigned to your account'},
            {'label': 'Drawdowns', 'value': str(drawdown_count) if drawdown_count else 'No data available', 'note': 'Drawdowns in managed facilities'},
            {'label': 'Upcoming events', 'value': _date_text(next_event[0]) if next_event else 'No data available', 'note': 'Next repayment date'},
        ]


@app.get('/api/dashboard/{user_id}')
def get_dashboard(user_id: int, user: dict[str, Any] = Depends(_get_authenticated_user)):
    if user_id != user['user_id']:
        raise HTTPException(status_code=403, detail='You can only view your own dashboard.')

    role = user['user_type']
    try:
        with get_connection() as connection:
            facility_ids = _facility_ids(connection, role, user_id)
            facilities = _dashboard_facilities(connection, role, user_id)
            payments = _dashboard_payments(connection, role, user_id, facility_ids)
            repayments = _dashboard_repayments(connection, facility_ids)
            cards = _dashboard_cards(connection, role, user_id, facility_ids)
    except (RuntimeError, psycopg2.Error) as exc:
        raise HTTPException(status_code=503, detail='Dashboard data is temporarily unavailable.') from exc

    return {
        'role': role,
        'user': {
            'user_id': user['user_id'],
            'username': user['username'],
            'organization_name': user['organization_name'],
            'user_type': role,
        },
        'cards': cards,
        'facilities': facilities,
        'payments': payments,
        'repayments': repayments,
    }


def _get_rate_history(benchmark: str, start_date: date, end_date: date) -> dict:
    if start_date > end_date:
        raise HTTPException(status_code=400, detail='Start date must be on or before end date.')

    fetch_rates = fetch_sofr_rates if benchmark == 'SOFR' else fetch_sonia_rates
    try:
        rate_history = fetch_rates(start_date, end_date)
    except (ValueError, requests.RequestException) as exc:
        raise HTTPException(
            status_code=502,
            detail=f'{benchmark} rates could not be retrieved from the official rate source: {exc}',
        ) from exc

    return {
        'benchmark': benchmark,
        'start_date': start_date.isoformat(),
        'end_date': end_date.isoformat(),
        'rate_history': rate_history,
    }


@app.get('/api/rates/sofr')
def get_sofr_rates(start_date: date, end_date: date) -> dict:
    return _get_rate_history('SOFR', start_date, end_date)


@app.get('/api/rates/sonia')
def get_sonia_rates(start_date: date, end_date: date) -> dict:
    return _get_rate_history('SONIA', start_date, end_date)


@app.post('/api/historical-loan')
def calculate_historical(request: LoanRequest):
    if not request.end_date:
        raise HTTPException(status_code=400, detail='Historical calculations require an end date.')
    try:
        return calculate_historical_loan(
            principal=request.principal,
            reference_rate=request.reference_rate,
            spread=request.spread,
            start_date=request.start_date,
            end_date=request.end_date,
            day_count=request.day_count,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover - broad fallback for frontend diagnostics
        raise HTTPException(status_code=500, detail=f'Unexpected historical calculation error: {exc}') from exc


@app.post('/api/active-loan')
def calculate_active(request: LoanRequest):
    try:
        return calculate_active_loan(
            principal=request.principal,
            reference_rate=request.reference_rate,
            spread=request.spread,
            start_date=request.start_date,
            as_of_date=request.as_of_date,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover - broad fallback for frontend diagnostics
        raise HTTPException(status_code=500, detail=f'Unexpected active calculation error: {exc}') from exc


if __name__ == '__main__':
    import uvicorn

    uvicorn.run('main:app', host='0.0.0.0', port=8000, reload=True)
