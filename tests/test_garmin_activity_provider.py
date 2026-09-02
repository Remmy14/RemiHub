from __future__ import annotations

import os
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from backend.services import garmin_activity_provider as provider


class FakeGarminApi:
    constructed_args = None
    login_calls = []
    date_calls = []
    prohibited_calls = []
    activities = []
    activity_details = {}
    login_error = None
    activities_error = None

    def __init__(self, *args, **kwargs):
        self.__class__.constructed_args = (args, kwargs)

    @classmethod
    def reset(cls):
        cls.constructed_args = None
        cls.login_calls = []
        cls.date_calls = []
        cls.prohibited_calls = []
        cls.activities = []
        cls.activity_details = {}
        cls.login_error = None
        cls.activities_error = None

    def login(self, tokenstore):
        self.__class__.login_calls.append(tokenstore)
        if self.__class__.login_error:
            raise self.__class__.login_error

    def get_activities_by_date(self, start, end, *, activitytype=None, sortorder=None):
        self.__class__.date_calls.append((start, end, activitytype, sortorder))
        if self.__class__.activities_error:
            raise self.__class__.activities_error
        return list(self.__class__.activities)

    def get_activity(self, *_args, **_kwargs):
        self.__class__.prohibited_calls.append("get_activity")
        raise AssertionError("get_activity must not be called")

    def get_activity_splits(self, *_args, **_kwargs):
        self.__class__.prohibited_calls.append("get_activity_splits")
        raise AssertionError("get_activity_splits must not be called")

    def get_activity_details(self, *_args, **_kwargs):
        activity_id = str(_args[0])
        if activity_id in self.__class__.activity_details:
            return self.__class__.activity_details[activity_id]
        self.__class__.prohibited_calls.append("get_activity_details")
        raise AssertionError("get_activity_details must not be called")

    def get_activity_hr_in_timezones(self, *_args, **_kwargs):
        self.__class__.prohibited_calls.append("get_activity_hr_in_timezones")
        raise AssertionError("get_activity_hr_in_timezones must not be called")


class RateLimited(Exception):
    status_code = 429


class ConnectionFailure(Exception):
    pass


class AuthenticationFailure(Exception):
    pass


class GarminActivityProviderTests(unittest.TestCase):
    def setUp(self):
        FakeGarminApi.reset()

    def patch_garmin(self):
        return patch.object(provider, "_garmin_class", return_value=FakeGarminApi)

    def test_runtime_login_uses_tokenstore_without_password_or_mfa(self):
        with self.patch_garmin(), patch.dict(os.environ, {"GARMIN_TOKENSTORE": "/secure/garmin"}, clear=True):
            provider.fetch_running_activity_summaries(date(2026, 8, 21))

        self.assertEqual(FakeGarminApi.constructed_args, ((), {}))
        self.assertEqual(FakeGarminApi.login_calls, ["/secure/garmin"])

    def test_runtime_query_uses_exact_date_and_running_summary_call_only(self):
        FakeGarminApi.activities = [{"activityId": 123, "activityName": "Run"}]

        with self.patch_garmin():
            provider.fetch_running_activity_summaries(
                date(2026, 8, 29),
                tokenstore="/secure/garmin",
            )

        start_date, end_date, activitytype, sortorder = FakeGarminApi.date_calls[0]
        self.assertIsInstance(start_date, str)
        self.assertIsInstance(end_date, str)
        self.assertEqual(
            FakeGarminApi.date_calls,
            [("2026-08-29", "2026-08-29", "running", "asc")],
        )
        self.assertEqual(activitytype, "running")
        self.assertEqual(sortorder, "asc")
        self.assertEqual(FakeGarminApi.prohibited_calls, [])

    def test_bootstrap_helper_constructs_password_login_inside_provider(self):
        def mfa():
            return "123456"

        with self.patch_garmin():
            provider.bootstrap_garmin_tokenstore(
                email="runner@example.com",
                password="secret",
                tokenstore=Path("/secure/garmin"),
                prompt_mfa=mfa,
            )

        args, kwargs = FakeGarminApi.constructed_args
        self.assertEqual(args, ("runner@example.com", "secret"))
        self.assertIs(kwargs["prompt_mfa"], mfa)
        self.assertEqual(FakeGarminApi.login_calls, ["/secure/garmin"])

    def test_structured_auth_rate_limit_and_network_failures(self):
        cases = [
            (AuthenticationFailure("bad token"), provider.GarminAuthError),
            (RateLimited("slow down"), provider.GarminRateLimitError),
            (ConnectionFailure("connection timeout"), provider.GarminNetworkError),
        ]
        for exc, expected in cases:
            with self.subTest(expected=expected.__name__):
                FakeGarminApi.reset()
                FakeGarminApi.activities_error = exc
                with self.patch_garmin():
                    with self.assertRaises(expected):
                        provider.fetch_running_activity_summaries(
                            date(2026, 8, 21),
                            tokenstore="/secure/garmin",
                        )

    def test_normalizes_all_v1_fields_and_rounds_fractional_seconds(self):
        activity = provider.normalize_activity_summary(
            {
                "activityId": 123,
                "activityUUID": "uuid-123",
                "activityName": "Morning Run",
                "activityType": {"typeKey": "running"},
                "startTimeLocal": "2026-08-21 07:00:00",
                "distance": "5000",
                "duration": "1800.5",
                "movingDuration": "1799.4",
                "averageSpeed": "2.777778",
                "averageHR": "150",
                "maxHR": "180",
                "activityTrainingLoad": "52.5",
                "aerobicTrainingEffect": "3.2",
                "anaerobicTrainingEffect": "0.4",
                "trainingEffectLabel": "MAINTAINING",
                "vO2MaxValue": "45",
                "hrTimeInZone_1": "10.5",
                "hrTimeInZone_2": "20.4",
                "hrTimeInZone_3": "30.5",
                "hrTimeInZone_4": "40.1",
                "hrTimeInZone_5": "50.9",
                "averageRunningCadenceInStepsPerMinute": "172",
                "avgPower": "245",
                "avgStrideLength": "88.09",
                "elevationGain": "25.2",
                "elevationLoss": "24.7",
                "calories": "410",
                "steps": "6400",
            }
        )

        self.assertEqual(activity.external_provider, "GARMIN")
        self.assertEqual(activity.external_activity_id, "123")
        self.assertEqual(activity.external_activity_uuid, "uuid-123")
        self.assertEqual(activity.external_activity_name, "Morning Run")
        self.assertEqual(activity.completed_distance_miles, Decimal("5000") / provider.METERS_PER_MILE)
        self.assertEqual(activity.duration_seconds, 1801)
        self.assertEqual(activity.moving_duration_seconds, 1799)
        self.assertEqual(activity.average_speed_meters_per_second, Decimal("2.777778"))
        self.assertEqual(activity.average_hr, Decimal("150"))
        self.assertEqual(activity.max_hr, Decimal("180"))
        self.assertEqual(activity.training_load, Decimal("52.5"))
        self.assertEqual(activity.aerobic_training_effect, Decimal("3.2"))
        self.assertEqual(activity.anaerobic_training_effect, Decimal("0.4"))
        self.assertEqual(activity.training_effect_label, "MAINTAINING")
        self.assertEqual(activity.vo2_max, Decimal("45"))
        self.assertEqual(activity.hr_zone_1_seconds, 11)
        self.assertEqual(activity.hr_zone_2_seconds, 20)
        self.assertEqual(activity.hr_zone_3_seconds, 31)
        self.assertEqual(activity.hr_zone_4_seconds, 40)
        self.assertEqual(activity.hr_zone_5_seconds, 51)
        self.assertEqual(activity.average_cadence_spm, Decimal("172"))
        self.assertEqual(activity.average_power_watts, Decimal("245"))
        self.assertEqual(activity.average_stride_length_meters, Decimal("0.8809"))
        self.assertEqual(activity.elevation_gain_meters, Decimal("25.2"))
        self.assertEqual(activity.elevation_loss_meters, Decimal("24.7"))
        self.assertEqual(activity.calories, Decimal("410"))
        self.assertEqual(activity.steps, 6400)

    def test_missing_optional_metrics_remain_nullable(self):
        activity = provider.normalize_activity_summary(
            {
                "activityId": 123,
                "activityType": {"typeKey": "running"},
                "distance": "1609.344",
                "duration": "600",
            }
        )

        self.assertEqual(activity.completed_distance_miles, Decimal("1"))
        self.assertIsNone(activity.moving_duration_seconds)
        self.assertIsNone(activity.average_stride_length_meters)

    def test_malformed_response_is_structured(self):
        with self.assertRaises(provider.GarminMalformedResponseError):
            provider.normalize_activity_summary({"activityId": 1, "activityType": {"typeKey": "cycling"}})

    def test_cycling_query_uses_verified_indoor_cycling_activity_type(self):
        FakeGarminApi.activities = [{"activityId": 123, "activityName": "Ride"}]

        with self.patch_garmin():
            provider.fetch_cycling_activity_summaries(
                date(2026, 8, 29),
                tokenstore="/secure/garmin",
            )

        self.assertEqual(
            FakeGarminApi.date_calls,
            [("2026-08-29", "2026-08-29", "indoor_cycling", "asc")],
        )
        self.assertEqual(FakeGarminApi.prohibited_calls, [])

    def test_cycling_summary_metrics_and_manufacturer_are_canonical(self):
        activity = provider.normalize_cycling_activity_summary(
            {
                "activityId": 456,
                "activityUUID": "uuid-456",
                "activityName": "18 min Just Ride",
                "activityType": {"typeKey": "indoor_cycling"},
                "manufacturer": "PELOTON",
                "startTimeLocal": "2026-08-29 06:30:00",
                "distance": "8107",
                "duration": "1083",
                "movingDuration": "1081",
                "calories": "260",
                "avgPower": "103",
                "maxPower": "218",
                "normPower": "132",
                "averageBikeCadenceInRevPerMinute": "78",
                "maxBikeCadenceInRevPerMinute": "110",
                "averageHR": "151",
                "maxHR": "163",
                "activityTrainingLoad": "21",
                "aerobicTrainingEffect": "2.4",
                "anaerobicTrainingEffect": "0.1",
            },
            resistance=(Decimal("24"), Decimal("39.49"), Decimal("57")),
        )

        self.assertEqual(activity.external_activity_type_key, "indoor_cycling")
        self.assertEqual(activity.external_manufacturer, "PELOTON")
        self.assertEqual(activity.completed_distance_miles, Decimal("8107") / provider.METERS_PER_MILE)
        self.assertEqual(activity.average_power_watts, Decimal("103"))
        self.assertEqual(activity.max_power_watts, Decimal("218"))
        self.assertEqual(activity.normalized_power_watts, Decimal("132"))
        self.assertEqual(activity.average_cadence_rpm, Decimal("78"))
        self.assertEqual(activity.max_cadence_rpm, Decimal("110"))
        self.assertEqual(activity.resistance_avg, Decimal("39.49"))

    def test_peloton_resistance_aggregates_are_transient_detail_derived(self):
        FakeGarminApi.activities = [
            {
                "activityId": 456,
                "activityType": {"typeKey": "indoor_cycling"},
                "manufacturer": "PELOTON",
                "distance": "1609.344",
                "duration": "600",
            }
        ]
        FakeGarminApi.activity_details = {
            "456": {
                "activityDetailMetrics": [
                    {"directResistance": "24"},
                    {"directResistance": "57"},
                    {"directResistance": None},
                    {"directResistance": "37.5"},
                ]
            }
        }

        with self.patch_garmin():
            resolution = provider.resolve_cycling_activities(
                date(2026, 8, 29),
                tokenstore="/secure/garmin",
            )

        self.assertEqual(len(resolution.activities), 1)
        activity = resolution.activities[0]
        self.assertEqual(activity.resistance_min, Decimal("24"))
        self.assertEqual(activity.resistance_max, Decimal("57"))
        self.assertEqual(activity.resistance_avg, Decimal("39.5"))
        self.assertNotIn("get_activity", FakeGarminApi.prohibited_calls)

    def test_cycling_partial_or_missing_hr_remains_nullable(self):
        activity = provider.normalize_cycling_activity_summary(
            {
                "activityId": 456,
                "activityType": {"typeKey": "indoor_cycling"},
                "distance": "1609.344",
                "duration": "600",
                "maxHR": "163",
            }
        )

        self.assertIsNone(activity.average_hr)
        self.assertEqual(activity.max_hr, Decimal("163"))

    def test_bootstrap_script_does_not_import_garminconnect(self):
        script = Path("backend/scripts/garmin_auth_bootstrap.py").read_text(encoding="utf-8")

        self.assertNotIn("garminconnect", script)
        self.assertNotIn("Garmin(", script)
        self.assertIn("bootstrap_garmin_tokenstore", script)


if __name__ == "__main__":
    unittest.main()
