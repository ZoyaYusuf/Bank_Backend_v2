import unittest
from datetime import date

from fastapi import HTTPException
from pydantic import ValidationError

from main import (
    BankSignupProfile,
    CompanySignupProfile,
    VerificationReviewRequest,
    _document_content,
)


class SignupVerificationTests(unittest.TestCase):
    def test_company_profile_accepts_risk_analysis_fields(self):
        profile = CompanySignupProfile.model_validate({
            'legal_name': 'Example Trading Ltd',
            'registration_number': 'REG-123',
            'country': 'United Kingdom',
            'business_address': '1 Example Street, London',
            'contact_email': 'finance@example.com',
            'contact_phone': '+44 20 1234 5678',
            'industry': 'Manufacturing',
            'incorporated_on': '2018-04-12',
            'revenue_currency': 'GBP',
            'annual_revenue': '1250000.00',
            'employee_count': '42',
        })

        self.assertEqual(profile.incorporated_on, date(2018, 4, 12))
        self.assertEqual(profile.employee_count, 42)
        self.assertEqual(str(profile.annual_revenue), '1250000.00')

    def test_bank_profile_rejects_invalid_swift_code(self):
        with self.assertRaises(ValidationError):
            BankSignupProfile.model_validate({
                'legal_name': 'Example Bank',
                'registration_number': 'REG-456',
                'country': 'United States',
                'business_address': '1 Example Street, New York',
                'contact_email': 'finance@example.com',
                'contact_phone': '+1 212 555 0100',
                'banking_license_number': 'LIC-456',
                'regulator': 'Example regulator',
                'bank_type': 'Commercial',
                'swift_bic': 'INVALID',
                'assets_currency': 'USD',
            })

    def test_document_type_and_signature_must_match(self):
        self.assertEqual(
            _document_content('registration.pdf', 'application/pdf', b'%PDF-1.7 content'),
            'application/pdf',
        )
        with self.assertRaises(HTTPException):
            _document_content('registration.pdf', 'application/pdf', b'not a PDF')
        with self.assertRaises(HTTPException):
            _document_content('registration.pdf', 'image/png', b'%PDF-1.7 content')

    def test_rejection_requires_agent_notes(self):
        with self.assertRaises(ValidationError):
            VerificationReviewRequest(status='REJECTED')
        self.assertEqual(
            VerificationReviewRequest(status='REJECTED', notes='Could not verify licence.').status,
            'REJECTED',
        )


if __name__ == '__main__':
    unittest.main()
