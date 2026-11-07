from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SECRET = "s" * 8 + "Q3p9_kM2vX7rT1yLwZ4nB6cD8fH0jA5eG"  # 41 chars
TOKENS_V1 = json.dumps({"di_token": "access-1", "di_refresh_token": "refresh-1", "di_client_id": "cid"})


class FakeTokens:
    def __init__(self) -> None:
        self.di_token = self.di_refresh_token = self.di_client_id = None

    def loads(self, s: str) -> None:
        d = json.loads(s)
        self.di_token, self.di_refresh_token, self.di_client_id = d["di_token"], d["di_refresh_token"], d["di_client_id"]

    def dumps(self) -> str:
        return json.dumps({"di_token": self.di_token, "di_refresh_token": self.di_refresh_token,
                           "di_client_id": self.di_client_id})


class GarminConnectAuthenticationError(Exception):
    pass


class FakeGarmin:
    """Stand-in for garminconnect.Garmin with canned responses."""

    instances = 0

    def __init__(self, rotate_on_call: bool = False, auth_fail: bool = False) -> None:
        FakeGarmin.instances += 1
        self.client = FakeTokens()
        self.rotate_on_call = rotate_on_call
        self.auth_fail = auth_fail
        self.calls: list[str] = []

    def login(self, tokenstore: str) -> tuple[None, None]:
        self.client.loads(tokenstore)
        return None, None

    def _touch(self, name: str) -> None:
        self.calls.append(name)
        if self.auth_fail:
            raise GarminConnectAuthenticationError("401 Unauthorized")
        if self.rotate_on_call:
            n = int(self.client.di_refresh_token.split("-")[1]) + 1
            self.client.di_token = f"access-{n}"
            self.client.di_refresh_token = f"refresh-{n}"

    def get_activities_by_date(self, start: str, end: str | None = None, activitytype: str | None = None,
                               sortorder: str | None = None) -> list[dict[str, Any]]:
        self._touch("get_activities_by_date")
        return [
            {"activityId": 101, "activityName": "Morning Run", "activityType": {"typeKey": "running"},
             "startTimeLocal": "2026-11-05 05:12:00", "distance": 7012.4, "duration": 2700.2,
             "movingDuration": 2680.0, "averageSpeed": 2.597, "averageHR": 146.3, "maxHR": 158.0,
             "elevationGain": 21.0, "averageRunningCadenceInStepsPerMinute": 168.4,
             "aerobicTrainingEffect": 2.8, "anaerobicTrainingEffect": 0.2, "vO2MaxValue": 44.0,
             "summarizedDiveInfo": {"huge": "junk"}, "splitSummaries": [1, 2, 3]},
            {"activityId": 102, "activityName": "Long Run", "activityType": {"typeKey": "running"},
             "startTimeLocal": "2026-11-07 05:00:00", "distance": 15000.0, "duration": 6100.0,
             "averageSpeed": 2.459, "averageHR": 151.0, "maxHR": 162.0},
        ]

    def get_activity(self, aid: str) -> dict[str, Any]:
        self._touch("get_activity")
        return {"activityId": int(aid), "activityName": "Long Run", "activityTypeDTO": {"typeKey": "running"},
                "summaryDTO": {"startTimeLocal": "2026-11-07T05:00:00.0", "distance": 15000.0,
                               "duration": 6100.0, "movingDuration": 6050.0, "averageSpeed": 2.459,
                               "averageHR": 151.0, "maxHR": 162.0, "averageRunCadence": 166.0,
                               "elevationGain": 40.0, "trainingEffect": 3.4}}

    def get_activity_splits(self, aid: str) -> dict[str, Any]:
        self._touch("get_activity_splits")
        return {"lapDTOs": [{"lapIndex": i, "distance": 1000.0, "duration": 400.0 + i, "averageSpeed": 1000 / (400 + i),
                             "averageHR": 145 + i, "maxHR": 150 + i, "averageRunCadence": 166, "elevationGain": 2}
                            for i in range(1, 4)]}

    def get_activity_hr_in_timezones(self, aid: str) -> list[dict[str, Any]]:
        self._touch("get_activity_hr_in_timezones")
        return [{"zoneNumber": z, "secsInZone": 600 * z, "zoneLowBoundary": 100 + 15 * z} for z in range(1, 6)]

    def get_user_summary(self, d: str) -> dict[str, Any]:
        self._touch("get_user_summary")
        return {"totalSteps": 12034, "restingHeartRate": 52, "averageStressLevel": 31,
                "bodyBatteryHighestValue": 88, "bodyBatteryLowestValue": 21, "totalKilocalories": 2510.0,
                "moderateIntensityMinutes": 20, "vigorousIntensityMinutes": 45, "someHugeList": list(range(100))}

    def get_sleep_data(self, d: str) -> dict[str, Any]:
        self._touch("get_sleep_data")
        return {"dailySleepDTO": {"sleepTimeSeconds": 25200, "deepSleepSeconds": 5400, "lightSleepSeconds": 14400,
                                  "remSleepSeconds": 5400, "awakeSleepSeconds": 600,
                                  "sleepStartTimestampLocal": 1794002400000, "sleepEndTimestampLocal": 1794028200000,
                                  "sleepScores": {"overall": {"value": 81, "qualifierKey": "GOOD"}}},
                "avgOvernightHrv": 61.0, "restingHeartRate": 51, "sleepMovement": list(range(500))}

    def get_hrv_data(self, d: str) -> dict[str, Any]:
        self._touch("get_hrv_data")
        return {"hrvSummary": {"calendarDate": d, "lastNightAvg": 61, "weeklyAvg": 58, "status": "BALANCED",
                               "baseline": {"balancedLow": 52, "balancedUpper": 66}},
                "hrvReadings": list(range(300))}

    def get_training_readiness(self, d: str) -> list[dict[str, Any]]:
        self._touch("get_training_readiness")
        return [{"score": 72, "level": "HIGH", "feedbackShort": "READY", "recoveryTime": 360,
                 "hrvFactorPercent": 90, "sleepScoreFactorPercent": 80}]

    def get_training_status(self, d: str) -> dict[str, Any]:
        self._touch("get_training_status")
        return {"mostRecentVO2Max": {"generic": {"vo2MaxPreciseValue": 44.6, "calendarDate": d}},
                "mostRecentTrainingStatus": {"latestTrainingStatusData": {"123": {
                    "trainingStatusFeedbackPhrase": "PRODUCTIVE_1",
                    "acuteTrainingLoadDTO": {"dailyTrainingLoadAcute": 420, "dailyTrainingLoadChronic": 380,
                                             "dailyAcuteChronicWorkloadRatio": 1.1, "acwrStatus": "OPTIMAL"}}}}}

    def get_max_metrics(self, d: str) -> list[dict[str, Any]]:
        self._touch("get_max_metrics")
        return [{"generic": {"calendarDate": d, "vo2MaxPreciseValue": 44.6, "fitnessAge": 27}, "cycling": None}]

    def get_race_predictions(self) -> dict[str, Any]:
        self._touch("get_race_predictions")
        return {"calendarDate": "2026-11-07", "time5K": 1620, "time10K": 3420, "timeHalfMarathon": 7620,
                "timeMarathon": 16200}

    def get_personal_record(self) -> list[dict[str, Any]]:
        self._touch("get_personal_record")
        return [{"typeId": 4, "value": 3601.0, "activityId": 9, "actStartDateTimeInGMTFormatted": "2026-01-30T22:00:00.0"},
                {"typeId": 7, "value": 21100.0, "activityId": 8},
                {"typeId": 3, "value": 1650.0, "activityId": 7},
                {"typeId": 12, "value": 30000.0}]

    def get_endurance_score(self, start: str, end: str | None = None) -> dict[str, Any]:
        self._touch("get_endurance_score")
        return {"calendarDate": start, "overallScore": 5421, "classification": 3,
                "contributors": [{"activityTypeId": 1, "contribution": 87.2}]}

    def get_lactate_threshold(self) -> dict[str, Any]:
        self._touch("get_lactate_threshold")
        return {"speed_and_heart_rate": {"calendarDate": "2026-10-01", "speed": 0.30555, "heartRate": 168},
                "power": {"functionalThresholdPower": 290, "powerToWeight": 4.31}}

    def get_body_battery(self, start: str, end: str | None = None) -> list[dict[str, Any]]:
        self._touch("get_body_battery")
        return [{"date": start, "charged": 60, "drained": 55,
                 "bodyBatteryValueDescriptorDTOList": [
                     {"bodyBatteryValueDescriptorIndex": 0, "bodyBatteryValueDescriptorKey": "timestamp"},
                     {"bodyBatteryValueDescriptorIndex": 1, "bodyBatteryValueDescriptorKey": "bodyBatteryStatus"},
                     {"bodyBatteryValueDescriptorIndex": 2, "bodyBatteryValueDescriptorKey": "bodyBatteryLevel"},
                     {"bodyBatteryValueDescriptorIndex": 3, "bodyBatteryValueDescriptorKey": "bodyBatteryVersion"}],
                 "bodyBatteryValuesArray": [[1, "MEASURED", 30, 2.0], [2, "MEASURED", 85, 2.0], [3, "MEASURED", 40, 2.0]]}]

    def get_heart_rates(self, d: str) -> dict[str, Any]:
        self._touch("get_heart_rates")
        base = 1793984400000  # 2026-11-07 00:00 Asia/Jakarta (17:00 UTC prev day)
        vals = [[base + i * 120_000, 60 + (i % 30)] for i in range(60)] + [[base + 3_600_000 * 5, None]]
        return {"restingHeartRate": 52, "minHeartRate": 48, "maxHeartRate": 165, "heartRateValues": vals}


class MemoryStore:
    def __init__(self, tokens: str = TOKENS_V1, fail_save: bool = False) -> None:
        self.tokens = tokens
        self.saved: list[str] = []
        self.fail_save = fail_save

    def load(self) -> str:
        return self.tokens

    def save(self, tokens_json: str) -> None:
        if self.fail_save:
            raise RuntimeError("network down")
        self.saved.append(tokens_json)
        self.tokens = tokens_json


class FakeGist:
    """httpx.MockTransport handler emulating the GitHub gist API."""

    def __init__(self, key: str, content: str | None = None) -> None:
        self.key = key
        self.content = content
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("authorization") != "Bearer ghp_test":
            return httpx.Response(401, json={"message": "Bad credentials"})
        if request.method == "GET" and request.url.path == "/gists/g123":
            return httpx.Response(200, json={"id": "g123", "files": {"garmin_tokens.enc": {
                "filename": "garmin_tokens.enc", "content": self.content, "truncated": False}}})
        if request.method == "PATCH" and request.url.path == "/gists/g123":
            body = json.loads(request.content)
            self.content = body["files"]["garmin_tokens.enc"]["content"]
            return httpx.Response(200, json={"id": "g123"})
        if request.method == "POST" and request.url.path == "/gists":
            body = json.loads(request.content)
            self.content = body["files"]["garmin_tokens.enc"]["content"]
            return httpx.Response(201, json={"id": "g123", "public": body["public"]})
        return httpx.Response(404, json={"message": "Not Found"})

    def plaintext(self) -> str:
        return Fernet(self.key.encode()).decrypt(self.content.encode()).decode()


@pytest.fixture
def key() -> str:
    return Fernet.generate_key().decode()


@pytest.fixture
def fake_gist(key: str) -> FakeGist:
    return FakeGist(key, Fernet(key.encode()).encrypt(TOKENS_V1.encode()).decode())


@pytest.fixture
def gist_http(fake_gist: FakeGist) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(fake_gist))
