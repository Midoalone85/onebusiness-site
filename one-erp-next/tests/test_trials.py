import unittest
from datetime import datetime, timedelta, timezone
from one_erp_core.trials import PlanState, TrialError, create_trial

NOW = datetime(2026, 10, 15, 0, 0, tzinfo=timezone.utc)


class TrialTests(unittest.TestCase):
    def trial(self):
        return create_trial(
            company_name="  Test Company ", country_code="sa", sector_code="Medical",
            administrator_email="Owner@Example.com", verified_email="owner@example.com",
            now=NOW)

    def test_seven_days_exactly(self):
        trial = self.trial()
        self.assertEqual(trial.expires_at, NOW + timedelta(days=7))
        self.assertEqual(trial.country_code, "SA")
        self.assertEqual(trial.sector_code, "medical")
        self.assertEqual(trial.company_name, "Test Company")

    def test_expires_on_boundary(self):
        trial = self.trial()
        self.assertEqual(trial.state_at(trial.expires_at - timedelta(microseconds=1)), PlanState.TRIAL)
        self.assertEqual(trial.state_at(trial.expires_at), PlanState.TRIAL_EXPIRED)
        self.assertEqual(trial.remaining_seconds_at(trial.expires_at), 0)

    def test_email_mismatch_is_rejected(self):
        with self.assertRaises(TrialError):
            create_trial(company_name="A", country_code="SA", sector_code="trade",
                         administrator_email="wrong@example.com", verified_email="right@example.com", now=NOW)

    def test_naive_datetime_rejected(self):
        with self.assertRaises(TrialError):
            create_trial(company_name="A", country_code="SA", sector_code="trade",
                         administrator_email="a@example.com", verified_email="a@example.com",
                         now=datetime(2026, 10, 15))

    def test_different_timezone_is_normalized(self):
        ksa = timezone(timedelta(hours=3))
        trial = create_trial(company_name="A", country_code="SA", sector_code="trade",
                             administrator_email="a@example.com", verified_email="a@example.com",
                             now=NOW.astimezone(ksa))
        self.assertEqual(trial.started_at, NOW)
