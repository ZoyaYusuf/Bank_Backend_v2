ALTER TABLE users
    ADD COLUMN IF NOT EXISTS verification_status TEXT NOT NULL DEFAULT 'APPROVED',
    ADD COLUMN IF NOT EXISTS verification_notes TEXT,
    ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS reviewed_by BIGINT REFERENCES users(user_id);

ALTER TABLE users
    ALTER COLUMN verification_status SET DEFAULT 'PENDING';

ALTER TABLE users
    DROP CONSTRAINT IF EXISTS users_verification_status_check;
ALTER TABLE users
    ADD CONSTRAINT users_verification_status_check
    CHECK (verification_status IN ('PENDING', 'APPROVED', 'REJECTED'));

CREATE TABLE IF NOT EXISTS company_profiles (
    user_id BIGINT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    legal_name TEXT NOT NULL,
    registration_number TEXT NOT NULL,
    country TEXT NOT NULL,
    industry TEXT NOT NULL,
    incorporated_on DATE NOT NULL,
    business_address TEXT NOT NULL,
    contact_email TEXT NOT NULL,
    contact_phone TEXT NOT NULL,
    website TEXT,
    annual_revenue NUMERIC(20, 2),
    revenue_currency CHAR(3) NOT NULL DEFAULT 'USD',
    employee_count INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (annual_revenue IS NULL OR annual_revenue >= 0),
    CHECK (employee_count IS NULL OR employee_count >= 0)
);

CREATE TABLE IF NOT EXISTS bank_profiles (
    user_id BIGINT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    legal_name TEXT NOT NULL,
    registration_number TEXT NOT NULL,
    banking_license_number TEXT NOT NULL,
    country TEXT NOT NULL,
    regulator TEXT NOT NULL,
    bank_type TEXT NOT NULL,
    swift_bic TEXT NOT NULL,
    business_address TEXT NOT NULL,
    contact_email TEXT NOT NULL,
    contact_phone TEXT NOT NULL,
    website TEXT,
    total_assets NUMERIC(20, 2),
    assets_currency CHAR(3) NOT NULL DEFAULT 'USD',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (total_assets IS NULL OR total_assets >= 0)
);

CREATE TABLE IF NOT EXISTS verification_documents (
    document_id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    original_filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    file_size BIGINT NOT NULL CHECK (file_size > 0),
    sha256 TEXT NOT NULL,
    file_content BYTEA NOT NULL,
    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS verification_documents_user_id_idx
    ON verification_documents(user_id);

CREATE INDEX IF NOT EXISTS users_verification_status_idx
    ON users(verification_status, created_at);
