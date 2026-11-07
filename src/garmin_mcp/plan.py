"""Training plan model: loads the YAML plan, parses session strings, resolves days/weeks."""

from __future__ import annotations

import datetime as dt
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_NAMES = {
    "mon": "Monday",
    "tue": "Tuesday",
    "wed": "Wednesday",
    "thu": "Thursday",
    "fri": "Friday",
    "sat": "Saturday",
    "sun": "Sunday",
}
ZONES_WITHOUT_HR = {"hmp", "tt", "race", "strides"}


# --------------------------------------------------------------------------- #
# Session mini-language
# --------------------------------------------------------------------------- #

@dataclass
class Step:
    """One executable step: an amount of distance/time in a zone."""

    zone: str
    km: float | None = None
    seconds: float | None = None

    def describe(self) -> str:
        if self.km is not None:
            amt = f"{self.km:g} km"
        else:
            s = self.seconds or 0
            amt = f"{s / 60:g} min" if s >= 60 and s % 60 == 0 else f"{s:g} s"
        return f"{amt} {self.zone}"


@dataclass
class Repeat:
    """A repeat block: `times` × (work, recovery); the final recovery is skipped."""

    times: int
    work: Step
    recovery: Step | None = None

    def describe(self) -> str:
        rec = f" / {self.recovery.describe()}" if self.recovery else ""
        return f"{self.times}x({self.work.describe()}{rec})"


Segment = Step | Repeat

_STEP_RE = re.compile(
    r"^\s*(?P<amt>\d+(?:[.,]\d+)?)\s*(?P<unit>km|k|min|m|'|′|s|sec)?\s+(?P<zone>[a-z_]+)\s*$",
    re.IGNORECASE,
)
_REPEAT_RE = re.compile(r"^\s*(?P<n>\d+)\s*[x×]\s*\((?P<body>.+)\)\s*$", re.IGNORECASE)


def parse_step(text: str) -> Step:
    m = _STEP_RE.match(text)
    if not m:
        raise ValueError(f"Cannot parse step: {text!r} (expected e.g. '4km easy', '40min easy', '20s strides')")
    amt = float(m["amt"].replace(",", "."))
    unit = (m["unit"] or "km").lower()
    zone = m["zone"].lower()
    if unit in ("km", "k"):
        return Step(zone=zone, km=amt)
    if unit in ("min", "m", "'", "′"):
        return Step(zone=zone, seconds=amt * 60)
    return Step(zone=zone, seconds=amt)


def parse_session(text: str | None) -> list[Segment]:
    """Parse a session string like '4km easy · 2x(3km tempo / 1km steady) · 4km easy'."""
    if not text or text.strip().lower() == "rest":
        return []
    segments: list[Segment] = []
    for part in re.split(r"\s*[·+]\s*", text.strip()):
        if not part:
            continue
        rm = _REPEAT_RE.match(part)
        if rm:
            pieces = [p for p in rm["body"].split("/")]
            work = parse_step(pieces[0])
            rec = parse_step(pieces[1]) if len(pieces) > 1 else None
            segments.append(Repeat(times=int(rm["n"]), work=work, recovery=rec))
        else:
            segments.append(parse_step(part))
    return segments


def flatten(segments: list[Segment]) -> list[Step]:
    out: list[Step] = []
    for seg in segments:
        if isinstance(seg, Repeat):
            for i in range(seg.times):
                out.append(seg.work)
                if seg.recovery and i < seg.times - 1:
                    out.append(seg.recovery)
        else:
            out.append(seg)
    return out


def describe_session(segments: list[Segment]) -> str:
    return " · ".join(s.describe() for s in segments) if segments else "rest"


# --------------------------------------------------------------------------- #
# Plan
# --------------------------------------------------------------------------- #

def _to_date(v: Any) -> dt.date:
    if isinstance(v, dt.date):
        return v
    return dt.date.fromisoformat(str(v))


def pace_to_seconds(p: str | None) -> float | None:
    """'6:15' -> 375.0 seconds per km."""
    if not p:
        return None
    parts = [float(x) for x in str(p).split(":")]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return parts[0]


def seconds_to_pace(s: float | None) -> str | None:
    if s is None or s <= 0:
        return None
    s = round(s)
    return f"{s // 60}:{s % 60:02d}"


def seconds_to_hms(s: float | None) -> str | None:
    if s is None:
        return None
    s = round(s)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


@dataclass
class Plan:
    raw: dict[str, Any]
    path: str
    race_pace_s_per_km: float | None = field(default=None)

    # --- basics ----------------------------------------------------------- #
    @property
    def name(self) -> str:
        return self.raw["name"]

    @property
    def timezone(self) -> str:
        return self.raw.get("timezone", "UTC")

    @property
    def start_date(self) -> dt.date:
        return _to_date(self.raw["start_date"])

    @property
    def race_date(self) -> dt.date:
        return _to_date(self.raw["goal"]["date"])

    @property
    def zones(self) -> dict[str, dict[str, Any]]:
        return self.raw["zones"]

    @property
    def rules(self) -> dict[str, Any]:
        return self.raw.get("rules", {})

    @property
    def weeks(self) -> list[dict[str, Any]]:
        return self.raw["weeks"]

    def hr_range(self, zone: str) -> tuple[int | None, int | None]:
        z = self.zones.get(zone, {})
        return z.get("min"), z.get("max")

    # --- calendar --------------------------------------------------------- #
    def week_index(self, day: dt.date) -> int | None:
        delta = (day - self.start_date).days
        if delta < 0:
            return None
        idx = delta // 7
        return idx if idx < len(self.weeks) else None

    def week_start(self, idx: int) -> dt.date:
        return self.start_date + dt.timedelta(days=7 * idx)

    def phase_for(self, day: dt.date) -> dict[str, Any] | None:
        for ph in self.raw.get("phases", []):
            if _to_date(ph["start"]) <= day <= _to_date(ph["end"]):
                return ph
        return None

    def milestone_for(self, day: dt.date) -> dict[str, Any] | None:
        key = f"{day.year:04d}-{day.month:02d}"
        for m in self.raw.get("milestones", []):
            if str(m["month"]) == key:
                return m
        return None

    def tests(self) -> list[dict[str, Any]]:
        return [dict(t, date=_to_date(t["date"])) for t in self.raw.get("tests", [])]

    def test_on(self, day: dt.date) -> dict[str, Any] | None:
        for t in self.tests():
            if t["date"] == day:
                return t
        return None

    def test_by_id(self, test_id: str) -> dict[str, Any] | None:
        for t in self.tests():
            if t["id"].lower() == test_id.lower():
                return t
        return None

    # --- week resolution -------------------------------------------------- #
    def _weekday_template(self, wk: dict[str, Any]) -> dict[str, str]:
        wd = wk.get("weekday", {})
        if isinstance(wd, str):
            wd = self.raw.get("templates", {}).get(wd, {})
        return dict(wd)

    def resolve_week(self, idx: int) -> dict[str, Any]:
        """Return a week with a concrete session (or rest) for every day."""
        wk = self.weeks[idx]
        start = self.week_start(idx)
        weekday = self._weekday_template(wk)
        key_day = wk.get("key_day", self.rules.get("key_day_default", "sat"))
        second_day = "sun" if key_day == "sat" else "sat"
        strength = wk.get("strength_days", self.raw.get("strength_days", {}))
        strength_text = self.raw.get("strength", {})

        days = []
        for i, d in enumerate(DAYS):
            date = start + dt.timedelta(days=i)
            entry: dict[str, Any] = {
                "date": date.isoformat(),
                "day": DAY_NAMES[d],
                "role": "rest",
                "session": "rest",
            }
            if d in weekday:
                entry.update(role="weekday_easy", session=weekday[d])
            if d == key_day:
                key = wk["key"]
                entry.update(role="key_run", key_type=key["type"], session=key["session"])
                if key.get("note"):
                    entry["note"] = key["note"]
            elif d == second_day:
                if str(wk.get("second", "rest")).lower() != "rest":
                    entry.update(role="weekend_easy", session=wk["second"])
            test = self.test_on(date)
            if test:
                entry["test"] = {k: (v.isoformat() if isinstance(v, dt.date) else v) for k, v in test.items()}
                entry["session"] = test["session"]
                if entry["role"] == "rest":
                    entry["role"] = "test"
            if d in strength:
                code = strength[d]
                entry["strength"] = {"code": code, "detail": strength_text.get(code, code)}
            segs = parse_session(entry["session"])
            entry["structure"] = describe_session(segs)
            entry["planned_km"] = round(self.estimate_km(segs, date), 1) if segs else 0.0
            days.append(entry)

        return {
            "week": wk["week"],
            "start": start.isoformat(),
            "end": (start + dt.timedelta(days=6)).isoformat(),
            "phase": wk.get("phase"),
            "planned_total_km": wk.get("total_km"),
            "weekday_km_estimate": wk.get("weekday_km"),
            "key_day": DAY_NAMES[key_day],
            "note": wk.get("note"),
            "days": days,
        }

    def day_plan(self, day: dt.date) -> dict[str, Any] | None:
        idx = self.week_index(day)
        if idx is None:
            return None
        week = self.resolve_week(idx)
        for d in week["days"]:
            if d["date"] == day.isoformat():
                return {"week": week["week"], "phase": week["phase"], **d}
        return None

    # --- estimates -------------------------------------------------------- #
    def easy_pace_s(self, day: dt.date) -> float:
        """Expected easy pace (s/km) for a date, from the milestone table (fallback: baseline)."""
        m = self.milestone_for(day)
        if m and m.get("easy_pace"):
            return pace_to_seconds(m["easy_pace"]) or 420.0
        # walk back to the most recent milestone with an easy pace
        best = pace_to_seconds(self.raw.get("baseline", {}).get("easy_pace_at_143_147")) or 420.0
        for ms in self.raw.get("milestones", []):
            y, mo = (int(x) for x in str(ms["month"]).split("-"))
            if dt.date(y, mo, 1) <= day and ms.get("easy_pace"):
                best = pace_to_seconds(ms["easy_pace"]) or best
        return best

    def zone_pace_s(self, zone: str, day: dt.date) -> float:
        easy = self.easy_pace_s(day)
        if zone == "hmp" and self.race_pace_s_per_km:
            return self.race_pace_s_per_km
        offsets = {
            "recovery": 20,
            "easy": 0,
            "aerobic_test": 0,
            "steady": -35,
            "tempo": -55,
            "tempo_test": -60,
            "hmp": -75,
            "tt": -85,
            "race": -75,
            "strides": -150,
        }
        return max(easy + offsets.get(zone, 0), 240)

    def estimate_km(self, segments: list[Segment], day: dt.date) -> float:
        total = 0.0
        for st in flatten(segments):
            if st.km is not None:
                total += st.km
            elif st.seconds:
                total += st.seconds / self.zone_pace_s(st.zone, day)
        return total

    def estimate_seconds(self, segments: list[Segment], day: dt.date) -> float:
        total = 0.0
        for st in flatten(segments):
            if st.seconds is not None:
                total += st.seconds
            elif st.km is not None:
                total += st.km * self.zone_pace_s(st.zone, day)
        return total

    def overview(self) -> dict[str, Any]:
        r = self.raw
        return {
            "name": r["name"],
            "athlete": r.get("athlete"),
            "goal": {k: (v.isoformat() if isinstance(v, dt.date) else v) for k, v in r["goal"].items()},
            "baseline": r.get("baseline"),
            "end_targets": r.get("end_targets"),
            "start_date": self.start_date.isoformat(),
            "weeks": len(self.weeks),
            "zones": r["zones"],
            "rules": r.get("rules"),
            "strength": r.get("strength"),
            "weekly_checklist": r.get("weekly_checklist"),
            "phases": [
                {k: (v.isoformat() if isinstance(v, dt.date) else v) for k, v in ph.items()}
                for ph in r.get("phases", [])
            ],
            "milestones": r.get("milestones"),
            "tests": [
                {k: (v.isoformat() if isinstance(v, dt.date) else v) for k, v in t.items()}
                for t in self.tests()
            ],
            "race_pace_override": seconds_to_pace(self.race_pace_s_per_km),
            "plan_file": self.path,
        }


def default_plan_path() -> str:
    env = os.getenv("GARMIN_MCP_PLAN")
    if env:
        return str(Path(env).expanduser())
    return str(resources.files("garmin_mcp").joinpath("plans/jrf2027_sub2.yaml"))


@lru_cache(maxsize=4)
def _load(path: str, mtime: float) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_plan(path: str | None = None) -> Plan:
    p = path or default_plan_path()
    raw = _load(p, os.path.getmtime(p))
    plan = Plan(raw=raw, path=p)
    rp = os.getenv("GARMIN_MCP_RACE_PACE") or raw.get("race_pace")
    if rp:
        plan.race_pace_s_per_km = pace_to_seconds(str(rp))
    validate_plan(plan)
    return plan


def validate_plan(plan: Plan) -> None:
    """Raise ValueError if any session string fails to parse."""
    templates = plan.raw.get("templates", {})
    for name, t in templates.items():
        for d, s in t.items():
            if d not in DAYS:
                raise ValueError(f"template {name}: bad day {d!r}")
            parse_session(s)
    for i, wk in enumerate(plan.weeks):
        if wk.get("week") != i:
            raise ValueError(f"weeks[{i}] has week={wk.get('week')}, expected {i}")
        parse_session(wk["key"]["session"])
        parse_session(wk.get("second"))
        wd = wk.get("weekday", {})
        if isinstance(wd, str) and wd not in templates:
            raise ValueError(f"week {i}: unknown weekday template {wd!r}")
        if isinstance(wd, dict):
            for s in wd.values():
                parse_session(s)
    for t in plan.raw.get("tests", []):
        parse_session(t["session"])
