from __future__ import annotations
import pandas as pd

def parse_utc(value: str | pd.Timestamp) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert("UTC")

def session_label(ts, trading_dates: set | None = None) -> str:
    t = parse_utc(ts)
    kr_date = t.tz_convert("Asia/Seoul").date()
    if trading_dates is not None:
        trading = kr_date in trading_dates
    else:
        trading = t.weekday() < 5
    if not trading:
        return "KRX_HOLIDAY_OR_WEEKEND"
    minute = t.hour * 60 + t.minute
    if minute < 350: return "KRX_REGULAR_EARLIER"
    if minute < 390: return "PRE_CLOSE_BASELINE"
    if minute < 400: return "POST_CLOSE_TRANSITION"
    if minute < 540: return "KRX_OFFICIAL_AFTER_HOURS"
    return "KRX_FULLY_CLOSED"

def trading_dates(start, end):
    try:
        import exchange_calendars as xcals
        cal = xcals.get_calendar("XKRX")
        sessions = cal.sessions_in_range(parse_utc(start).date(), parse_utc(end).date())
        # Sessions are labelled by local trading date in recent exchange_calendars.
        return {x.date() for x in sessions}, "exchange_calendars:XKRX"
    except Exception:
        rng = pd.date_range(parse_utc(start).date(), parse_utc(end).date(), freq="B")
        return {x.date() for x in rng}, "weekday_fallback（节假日可能误标）"

