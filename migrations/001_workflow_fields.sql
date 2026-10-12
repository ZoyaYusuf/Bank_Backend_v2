ALTER TABLE loan_requests
    ADD COLUMN IF NOT EXISTS tenor_months INTEGER,
    ADD COLUMN IF NOT EXISTS review_notes TEXT;

ALTER TABLE loan_requests
    DROP CONSTRAINT IF EXISTS loan_requests_tenor_months_check;
ALTER TABLE loan_requests
    ADD CONSTRAINT loan_requests_tenor_months_check
    CHECK (tenor_months IS NULL OR tenor_months > 0);

ALTER TABLE facility_participants
    ADD COLUMN IF NOT EXISTS offered_spread NUMERIC(8, 5) NOT NULL DEFAULT 0;

ALTER TABLE facility_participants
    DROP CONSTRAINT IF EXISTS facility_participants_status_check;
ALTER TABLE facility_participants
    ADD CONSTRAINT facility_participants_status_check
    CHECK (status IN ('PROPOSED', 'ACTIVE', 'REJECTED', 'INACTIVE'));

ALTER TABLE payments
    ADD COLUMN IF NOT EXISTS repayment_schedule_id BIGINT
        REFERENCES repayment_schedules(schedule_id);
