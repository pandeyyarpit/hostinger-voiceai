-- ══════════════════════════════════════════════════════════════════════════════
-- MANUAL APPOINTMENTS FOR CONFIRMATION AGENT — Run once in Supabase SQL Editor
-- Adds manually entered appointments and an optional exact confirmation call time.
-- ══════════════════════════════════════════════════════════════════════════════

ALTER TABLE appointments
    ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'cal'
        CHECK (source IN ('cal', 'manual')),
    ADD COLUMN IF NOT EXISTS confirmation_call_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS notes TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_appointments_manual_confirmation_time
    ON appointments (confirmation_call_at)
    WHERE booking_status = 'booked' AND confirmation_call_at IS NOT NULL;

-- Manual rows have a generated cal_booking_id beginning with `manual:`. They
-- are never sent to Cal.com unless a future operator explicitly creates one.
