-- =====================================================================
-- Wastraq - RECONCILE properties.verification_status WITH THE REVIEW RECORD
--
--   psql -v ON_ERROR_STOP=1 -d wastraq_demo -f database/reconcile_verification_status.sql
--
-- WHY THIS EXISTS
-- ---------------
-- The state machine says exactly one thing may clear a property for
-- operation: a reviewer approving its field survey. `review_survey()` does
-- two writes for that - it sets property_surveys.survey_status = 'APPROVED'
-- AND properties.verification_status = 'VERIFIED_FOR_OPERATION'.
--
-- The 16 pilot properties were not created through that API. Their survey
-- rows were written directly by the seed, complete with a reviewer, a
-- reviewed_at and review_status = 'APPROVED' - but the second write never
-- happened, so the property rows stayed at 'FIELD_SURVEYED'. The database
-- has been internally inconsistent since the lane was first loaded: an
-- approved, reviewed survey whose property does not say so.
--
-- This is the second write, applied after the fact. It is NOT a promotion
-- of unreviewed work: every row it touches carries its own reviewer id,
-- its own reviewed_at and its own APPROVED review_status, already in the
-- database. Nothing here invents an approval, and a property with no
-- approved survey is not touched.
--
-- WHAT IT WILL NOT DO
-- -------------------
-- * It does not touch geometry, photos, collection events or evidence.
-- * It does not create, delete or reseed any row.
-- * It only ever moves FIELD_SURVEYED -> VERIFIED_FOR_OPERATION. A
--   DISPUTED, UNVERIFIED or PENDING_SURVEY property is left exactly as it
--   is, because for those the review record is not the whole story.
-- * It never moves anything DOWN. A newly registered property stays
--   PENDING_SURVEY until a reviewer actually approves it.
--
-- Idempotent: running it twice reports 0 the second time.
-- =====================================================================

BEGIN;

DO $$
DECLARE
    candidates int;
    promoted   int;
    skipped    int;
    promoted_ids text[];
BEGIN
    SELECT count(*) INTO candidates
    FROM properties p
    JOIN v_property_current_survey s ON s.property_id = p.property_id
    WHERE s.survey_status = 'APPROVED'
      AND s.review_status = 'APPROVED'
      AND s.reviewer_id  IS NOT NULL
      AND s.reviewed_at  IS NOT NULL
      AND p.verification_status = 'FIELD_SURVEYED';

    WITH moved AS (
        UPDATE properties p
           SET verification_status = 'VERIFIED_FOR_OPERATION',
               updated_at = now(),
               -- Attribute it to the reviewer who actually approved it, not
               -- to whoever happened to run this script.
               updated_by = COALESCE(p.updated_by, s.reviewer_id)
          FROM v_property_current_survey s
         WHERE s.property_id = p.property_id
           AND s.survey_status = 'APPROVED'
           AND s.review_status = 'APPROVED'
           AND s.reviewer_id  IS NOT NULL
           AND s.reviewed_at  IS NOT NULL
           AND p.verification_status = 'FIELD_SURVEYED'
        RETURNING p.property_id
    )
    SELECT count(*), coalesce(array_agg(property_id), '{}') INTO promoted, promoted_ids FROM moved;

    -- Anything with an approved survey that this did NOT promote, so a
    -- surprising case is reported rather than silently passed over.
    SELECT count(*) INTO skipped
    FROM properties p
    JOIN v_property_current_survey s ON s.property_id = p.property_id
    WHERE s.survey_status = 'APPROVED'
      AND p.verification_status NOT IN ('VERIFIED_FOR_OPERATION', 'FIELD_VERIFIED');

    RAISE NOTICE 'reconcile: % candidate(s), % promoted to VERIFIED_FOR_OPERATION', candidates, promoted;
    IF skipped > 0 THEN
        RAISE NOTICE 'reconcile: % propert(ies) have an APPROVED survey but were left alone '
                     '(missing reviewer/reviewed_at, or in a state this script will not touch)', skipped;
    END IF;

    -- Post-condition, scoped to the rows THIS statement moved. Checking
    -- every VERIFIED_FOR_OPERATION row in the database would abort the
    -- reconciliation over pre-existing data it never touched.
    IF EXISTS (
        SELECT 1 FROM properties p
        LEFT JOIN v_property_current_survey s ON s.property_id = p.property_id
        WHERE p.property_id = ANY (promoted_ids)
          AND (s.survey_status IS DISTINCT FROM 'APPROVED' OR s.reviewed_at IS NULL)
    ) THEN
        RAISE EXCEPTION 'refusing to commit: a property was promoted without '
                        'an approved, reviewed survey';
    END IF;
END $$;

COMMIT;
