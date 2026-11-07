"""Bounded timeline coordinates; no view identity, source proof or authorization.

Match the client TimelineRange codec. Calendar/historical endpoints denote whole
periods; instant/ordinal endpoints denote points. Never use pixels, local timezone
defaults, binary floats for decimals, or datetime for astronomical years.
"""

import json
import re
from datetime import datetime, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BeforeValidator


def require(condition):
    if not condition:
        raise ValueError("Invalid timeline range")


def fields(value, keys):
    require(isinstance(value, dict) and set(value) == set(keys))


def integer(value, minimum, maximum):
    return type(value) in (int, float) and minimum <= value <= maximum and value == int(value)


def days(year, month):
    if month == 2:
        return 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28
    return 30 if month in (4, 6, 9, 11) else 31


def next_day(value):
    year, month, day = value
    if day < days(year, month):
        return year, month, day + 1
    return (year, month + 1, 1) if month < 12 else (year + 1, 1, 1)


def date(value):
    require(isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value))
    year, month, day = map(int, value.split("-"))
    require(year >= 1 and 1 <= month <= 12 and 1 <= day <= days(year, month))
    return year, month, day


def zone(value):
    require(
        isinstance(value, str)
        and len(value) <= 128
        and re.fullmatch(r"[A-Za-z][A-Za-z0-9_+-]*(?:/[A-Za-z0-9_+-]+)*", value)
    )
    # A saved target must survive reading on a host with older timezone data.
    # Capture/projection validates local support separately, without guessing.
    return value


def validate_timeline_time_zone(value):
    value = zone(value)
    try:
        ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError):
        raise ValueError("Unsupported timeline time zone") from None
    return value


def stamp(value):
    return (
        f"{value.year:04d}-{value.month:02d}-{value.day:02d}T"
        f"{value.hour:02d}:{value.minute:02d}:{value.second:02d}.{value.microsecond // 1000:03d}"
    )


def instant(value):
    require(isinstance(value, str) and len(value) <= 29)
    match = re.fullmatch(
        r"([0-9]{4}-[0-9]{2}-[0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})"
        r"(?:\.([0-9]{1,3}))?(Z|[+-][0-9]{2}:[0-9]{2})",
        value,
    )
    require(match)
    year, month, day = date(match[1])
    hour, minute, second = map(int, match.group(2, 3, 4))
    millis = int((match[5] or "").ljust(3, "0"))
    suffix = match[6]
    require(hour < 24 and minute < 60 and second < 60 and suffix != "-00:00")
    offset_hour, offset_minute = (0, 0) if suffix == "Z" else (int(suffix[1:3]), int(suffix[4:]))
    require(offset_hour < 24 and offset_minute < 60)
    offset = (offset_hour * 60 + offset_minute) * (-1 if suffix.startswith("-") else 1)
    civil = datetime(year, month, day, hour, minute, second, millis * 1000)
    try:
        utc = civil - timedelta(minutes=offset)
    except OverflowError:
        raise ValueError("Invalid timeline range") from None
    return stamp(civil) + ("Z" if offset == 0 else suffix), stamp(utc) + "Z", utc


def historical(value):
    require(isinstance(value, dict) and integer(value.get("year"), -1_000_000_000, 1_000_000_000))
    precision, year = value.get("precision"), int(value["year"])
    if precision == "year":
        fields(value, ("precision", "year"))
        return dict(precision=precision, year=year)
    month = value.get("month")
    require(integer(month, 1, 12))
    month = int(month)
    if precision == "month":
        fields(value, ("precision", "year", "month"))
        return dict(precision=precision, year=year, month=month)
    day = value.get("day")
    require(precision == "day" and integer(day, 1, days(year, month)))
    day = int(day)
    fields(value, ("precision", "year", "month", "day"))
    return dict(precision=precision, year=year, month=month, day=day)


def period(point):
    year = point["year"]
    if point["precision"] == "year":
        return (year, 1, 1), (year + 1, 1, 1)
    month = point["month"]
    if point["precision"] == "month":
        return (year, month, 1), ((year, month + 1, 1) if month < 12 else (year + 1, 1, 1))
    start = (year, month, point["day"])
    return start, next_day(start)


def decimal(value):
    require(
        isinstance(value, str)
        and len(value) <= 26
        and re.fullmatch(r"-?(?:0|[1-9][0-9]{0,14})(?:\.[0-9]{1,9})?", value)
    )
    negative = value.startswith("-")
    whole, _, fraction = value.lstrip("-").partition(".")
    scaled = int(whole) * 1_000_000_000 + int(fraction.ljust(9, "0"))
    trimmed = fraction.rstrip("0")
    canonical = (
        "0"
        if scaled == 0
        else ("-" if negative else "") + whole + ("." + trimmed if trimmed else "")
    )
    return canonical, -scaled if negative else scaled


def ordered(start, end, bounds):
    require(start < end or start == end and bounds["start_inclusive"] and bounds["end_inclusive"])


def normalize_timeline_range(value):
    require(
        isinstance(value, dict)
        and integer(value.get("version"), 1, 1)
        and type(value.get("start_inclusive")) is bool
        and type(value.get("end_inclusive")) is bool
    )
    bounds = dict(
        version=1, start_inclusive=value["start_inclusive"], end_inclusive=value["end_inclusive"]
    )
    common = ("version", "kind", "start", "end", "start_inclusive", "end_inclusive")
    kind = value.get("kind")
    if kind in ("calendar", "instant"):
        fields(value, (*common, "time_zone"))
        time_zone = zone(value["time_zone"])
        if kind == "calendar":
            start, end = date(value["start"]), date(value["end"])
            lower = start if bounds["start_inclusive"] else next_day(start)
            upper = next_day(end) if bounds["end_inclusive"] else end
            require(start <= end and lower < upper)
            return dict(
                **bounds, kind=kind, start=value["start"], end=value["end"], time_zone=time_zone
            )
        start, end = instant(value["start"]), instant(value["end"])
        ordered(start[2], end[2], bounds)
        return dict(**bounds, kind=kind, start=start[0], end=end[0], time_zone=time_zone)
    if kind == "historical":
        fields(value, (*common, "calendar", "year_numbering"))
        require(
            value["calendar"] == "proleptic-gregorian" and value["year_numbering"] == "astronomical"
        )
        start, end = historical(value["start"]), historical(value["end"])
        a, b = period(start), period(end)
        require(a[0 if bounds["start_inclusive"] else 1] < b[1 if bounds["end_inclusive"] else 0])
        return dict(
            **bounds,
            kind=kind,
            start=start,
            end=end,
            calendar="proleptic-gregorian",
            year_numbering="astronomical",
        )
    require(kind == "ordinal" and value.get("unit") == "property-value")
    fields(value, (*common, "unit"))
    start, end = decimal(value["start"]), decimal(value["end"])
    ordered(start[1], end[1], bounds)
    return dict(**bounds, kind=kind, start=start[0], end=end[0], unit="property-value")


def timeline_range_key(value):
    value = normalize_timeline_range(value)
    if value["kind"] == "instant":
        points = [instant(value[key])[1] for key in ("start", "end")]
    elif value["kind"] == "historical":
        points = [
            [
                value[key]["precision"],
                value[key]["year"],
                value[key].get("month"),
                value[key].get("day"),
            ]
            for key in ("start", "end")
        ]
    else:
        points = [value["start"], value["end"]]
    context = value.get(
        "time_zone", value.get("unit", [value.get("calendar"), value.get("year_numbering")])
    )
    return json.dumps(
        [1, value["kind"], *points, value["start_inclusive"], value["end_inclusive"], context],
        ensure_ascii=False,
        separators=(",", ":"),
    )


TimelineRangeValue = Annotated[dict, BeforeValidator(normalize_timeline_range)]
