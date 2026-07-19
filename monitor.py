#!/usr/bin/env python3
"""
Bethpage Black/Red tee-time availability monitor.

Read-only: polls ForeUp's public availability JSON and alerts on new openings.
Never logs in, never books, never holds. You book by hand from the alert link.

Modes:
  python monitor.py probe        # one-off: print discovered course map, exit
  python monitor.py watch        # single pass (cancellation watch, cron every 15 min)
  python monitor.py droprace     # sleep until 6:59:30 pm ET, then poll every 5 s until 7:06
"""

import json
import os
import re
import smtplib
import ssl
import sys
import time
from datetime import datetime, timedelta, date
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

import requests
import yaml

VERSION = "v3.0-course-map"
ET = ZoneInfo("America/New_York")
FACILITY_ID = "19765"
BASE = "https://foreupsoftware.com/index.php"
BOOKING_PAGE = f"{BASE}/booking/{FACILITY_ID}/{{sched}}#/teetimes"
TIMES_API = f"{BASE}/api/booking/times"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": f"{BASE}/booking/{FACILITY_ID}",
    "X-Requested-With": "XMLHttpRequest",
    "Api-Key": "no_limits",
    "X-Fu-Golfer-Location": "foreup",
}

# Per-course primary schedule + resident booking class, captured from real
# logged-in browser requests (one per course).
COURSE_MAP = {
    "black": {"schedule_id": "2431", "booking_class": "2136"},
    "red":   {"schedule_id": "2432", "booking_class": "2138"},
    "blue":  {"schedule_id": "2433", "booking_class": "2140"},
}
ALL_SCHEDULES = ["2517", "2431", "2433", "2539", "2538", "2434", "2432", "2435"]

# Optional authenticated session: paste your browser's ForeUp cookie into the
# FOREUP_COOKIE repo secret and availability requests run with your login.
_cookie = os.environ.get("FOREUP_COOKIE", "").strip()
if _cookie:
    HEADERS["Cookie"] = _cookie
_jwt = os.environ.get("FOREUP_JWT", "").strip()
if _jwt:
    if not _jwt.lower().startswith("bearer"):
        _jwt = "Bearer " + _jwt
    HEADERS["X-Authorization"] = _jwt

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(ROOT, "state.json")
CONFIG_PATH = os.path.join(ROOT, "config.yaml")


# ----------------------------------------------------------------------------
# config / state
# ----------------------------------------------------------------------------

def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return {"seen": {}, "discovery": None}


def save_state(state):
    # prune seen entries for dates in the past so the file never grows forever
    today = datetime.now(ET).strftime("%Y-%m-%d")
    state["seen"] = {k: v for k, v in state["seen"].items() if k[:10] >= today}
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)


# ----------------------------------------------------------------------------
# discovery: schedule IDs + booking classes, parsed from the booking page HTML
# ----------------------------------------------------------------------------

COURSE_WORD = re.compile("\\b(black|red|blue|green|yellow)\\b", re.I)
TRACKED = ("black", "red", "blue")
RX_BCLASS = re.compile("\"booking_class_id\"\\s*:\\s*\"?(\\d+)\"?")
RX_SCHEDID = re.compile("\"schedule_id\"\\s*:\\s*\"?(\\d+)\"?")
RX_TITLE_COURSE = re.compile(
    "(?:<title>[^<]*?|\"schedule_name\"\\s*:\\s*\"[^\"]*?|<h\\d[^>]*>[^<]*?)"
    "\\b(Black|Red|Blue|Green|Yellow)\\b", re.I)


def _course_key(name):
    m = COURSE_WORD.search(str(name or ""))
    return m.group(1).lower() if m else None


def _named_courses_from(entries):
    out = {}
    for e in entries:
        name = str(e.get("schedule_name") or e.get("name") or e.get("title")
                   or e.get("course_name") or "")
        sid = str(e.get("schedule_id") or e.get("id") or e.get("teesheet_id") or "")
        key = _course_key(name)
        if sid and key:
            out.setdefault(key, {"schedule_id": sid, "name": name})
    return out


def _obj_around(html, pos, span=400):
    left = html.rfind("{", max(0, pos - span), pos)
    right = html.find("}", pos, pos + span)
    return html[left:right] if left != -1 and right != -1 else ""


def _name_in_obj(obj):
    m = re.search('"(?:name|title|label)"\\s*:\\s*"([^"]{2,80})"', obj)
    return m.group(1) if m else ""


def discover(session, config):
    """Map Bethpage courses to booking classes on shared schedule 2431.
    Never hard-fails if the page is reachable: falls back to deferred mode
    where slots are attributed by per-item course names at runtime."""
    override = config.get("schedule_overrides") or {}
    if override.get("black", {}).get("schedule_id") and override.get("red", {}).get("schedule_id"):
        return {"courses": override, "ts": datetime.now(ET).isoformat(), "source": "config"}

    def note(msg):
        print(f"[probe] {msg}", flush=True)

    note(f"auth: cookie={'present(%d chars)' % len(_cookie) if _cookie else 'NOT SET'}, "
         f"jwt={'present(%d chars)' % len(_jwt) if _jwt else 'NOT SET'}")

    # ---- fetch booking page, harvest booking_class id -> name table --------
    try:
        r = session.get(BOOKING_PAGE.format(sched="2431"), headers=HEADERS, timeout=20)
        note(f"A: booking page HTTP {r.status_code}, {len(r.text)} bytes")
        html = r.text if r.ok else ""
    except requests.RequestException as e:
        raise RuntimeError(f"booking page unreachable: {e}")
    if not html:
        raise RuntimeError("booking page returned no HTML")

    cls_names = {}
    for m in RX_BCLASS.finditer(html):
        cid = m.group(1)
        if cid not in cls_names:
            cls_names[cid] = _name_in_obj(_obj_around(html, m.start()))
    note(f"A: {len(cls_names)} booking classes found; name table:")
    for cid, nm in cls_names.items():
        tag = _course_key(nm) or "-"
        note(f"A:   class {cid:>6} -> {nm!r} [{tag}]")

    # ---- test which classes the anonymous API accepts -----------------------
    day3 = (datetime.now(ET).date() + timedelta(days=3)).strftime("%m-%d-%Y")
    accessible = []
    for cid in cls_names:
        params = [("time", "all"), ("date", day3), ("holes", "all"),
                  ("players", "0"), ("schedule_id", ALL_SCHEDULES[0])]
        params += [("schedule_ids[]", s) for s in ALL_SCHEDULES]
        params += [("specials_only", "0"), ("api_key", ""),
                   ("booking_class", cid)]
        try:
            r = session.get(TIMES_API, params=params, headers=HEADERS, timeout=15)
            if r.ok:
                accessible.append(cid)
        except requests.RequestException:
            pass
        time.sleep(0.15)
    accessible = [c for c in PREFERRED_CLASSES if c in accessible] + \
                 [c for c in accessible if c not in PREFERRED_CLASSES]
    note(f"B: accessible classes (HTTP 200, preferred first): {accessible}")

    # ---- build course mapping ----------------------------------------------
    found = {}
    for key in TRACKED:
        named = [c for c in accessible if _course_key(cls_names.get(c)) == key]
        if named:
            found[key] = {"schedule_id": "2431", "booking_classes": named,
                          "name": cls_names[named[0]], "mode": "class"}
    if len(found) < len(TRACKED):
        # deferred mode: poll every accessible class; item course names decide
        note("C: class names don't identify courses -> deferred per-item mode")
        for key in TRACKED:
            found.setdefault(key, {"schedule_id": "2431",
                                   "booking_classes": accessible or [None],
                                   "name": f"deferred:{key}", "mode": "item"})

    note(f"RESULT: {json.dumps(found)}")
    if not accessible:
        note("WARNING: no anonymously accessible booking class; if the watch "
             "workflow never alerts even when the site shows times, we will "
             "need an authenticated session cookie.")
    return {"courses": found, "ts": datetime.now(ET).isoformat(), "source": "live"}


def get_discovery(session, config, state):
    d = state.get("discovery")
    if d:
        age_days = (datetime.now(ET) - datetime.fromisoformat(d["ts"])).days
        have = set((d.get("courses") or {}).keys())
        if age_days < 7 and set(TRACKED) <= have:
            return d
    d = discover(session, config)
    state["discovery"] = d
    return d


# ----------------------------------------------------------------------------
# availability
# ----------------------------------------------------------------------------

def fetch_times(session, course_key, day: date, cmap=None):
    """One request for one course, using its own primary schedule and resident
    booking class — an exact mirror of the browser request for that course."""
    m = (cmap or COURSE_MAP).get(course_key)
    if not m:
        return []
    datestr = day.strftime("%m-%d-%Y")
    params = [
        ("time", "all"), ("date", datestr), ("holes", "all"), ("players", "0"),
        ("booking_class", m["booking_class"]),
        ("schedule_id", m["schedule_id"]),
    ]
    params += [("schedule_ids[]", s) for s in ALL_SCHEDULES]
    params += [("specials_only", "0"), ("api_key", "")]
    try:
        r = session.get(TIMES_API, params=params, headers=HEADERS, timeout=15)
        data = r.json() if r.ok else None
    except (requests.RequestException, ValueError):
        data = None
    if isinstance(data, list):
        return [it for it in data if isinstance(it, dict)]
    return []


def parse_slot(raw, course_key):
    t = raw.get("time") or ""  # "2026-07-10 15:30"
    try:
        dt = datetime.strptime(t, "%Y-%m-%d %H:%M").replace(tzinfo=ET)
    except ValueError:
        return None
    return {
        "course": course_key,
        "dt": dt,
        "date": dt.strftime("%Y-%m-%d"),
        "time_label": dt.strftime("%-I:%M%p").lower(),
        "weekday": dt.strftime("%a"),
        "spots": int(raw.get("available_spots") or raw.get("available_spots_18") or 0),
        "holes": raw.get("holes"),
    }


def in_window(slot, windows):
    w = windows.get(slot["dt"].strftime("%A").lower())
    if not w or not w.get("enabled", True):
        return False
    hhmm = slot["dt"].strftime("%H:%M")
    return w["start"] <= hhmm <= w["end"]


def slot_key(slot):
    return f'{slot["date"]} {slot["dt"].strftime("%H:%M")} {slot["course"]}'


def scan(session, config, state, days_ahead):
    """One pass: for each configured course, query its own schedule+class for
    each in-window day. Slots belong to the course we asked for; the
    schedule_name census stays as a diagnostic."""
    ccfg = config["courses"]
    cmap = {k: {**COURSE_MAP.get(k, {}), **{kk: vv for kk, vv in (ccfg[k] or {}).items()
                if kk in ("schedule_id", "booking_class") and vv}}
            for k in ccfg}
    tracked = [k for k in ccfg if cmap.get(k, {}).get("schedule_id")]
    today = datetime.now(ET).date()
    new_slots = []

    n_raw, n_window = 0, 0
    n_named = {k: 0 for k in tracked}
    sched_names = {}
    sample_logged = False
    print(f"[scan] monitor {VERSION}", flush=True)

    for ckey in tracked:
        for offset in days_ahead:
            day = today + timedelta(days=offset)
            if day.strftime("%A").lower() not in ccfg[ckey]["windows"]:
                continue
            for raw in fetch_times(session, ckey, day, cmap):
                n_raw += 1
                sn = str(raw.get("schedule_name") or "")
                sched_names[sn] = sched_names.get(sn, 0) + 1
                if not sample_logged:
                    print("[scan] sample raw item: " + json.dumps(raw)[:400], flush=True)
                    sample_logged = True
                # only count slots that are actually this course's product
                # (the API sometimes returns sibling-schedule rows)
                item_key = _course_key(sn)
                if item_key is not None and item_key != ckey:
                    continue
                if "9 hole" in sn.lower() and not config.get("include_nine_hole_products"):
                    continue
                slot = parse_slot(raw, ckey)
                if not slot or slot["spots"] < 1:
                    continue
                n_named[ckey] += 1
                if not in_window(slot, ccfg[ckey]["windows"]):
                    continue
                n_window += 1
                k = slot_key(slot)
                prev_spots = state["seen"].get(k, 0)
                if slot["spots"] > prev_spots:
                    slot["new_group"] = slot["spots"] >= 2 and prev_spots < 2
                    new_slots.append(slot)
                state["seen"][k] = slot["spots"]
            time.sleep(0.2)

    if sched_names:
        top = sorted(sched_names.items(), key=lambda x: -x[1])[:15]
        print("[scan] products seen: " +
              "; ".join(f"{n or '(blank)'} x{c}" for n, c in top), flush=True)
    counts = ", ".join(f"{k} {v}" for k, v in n_named.items())
    print(f"[scan] raw rows: {n_raw} | usable per course: ({counts}) | "
          f"in your windows: {n_window} | new since last pass: {len(new_slots)}", flush=True)
    return new_slots


# ----------------------------------------------------------------------------
# alerts
# ----------------------------------------------------------------------------

def send_email(cfg, to_addrs, subject, body):
    user = os.environ["GMAIL_ADDRESS"]
    pw = os.environ["GMAIL_APP_PASSWORD"]
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = ", ".join(to_addrs)
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as s:
        s.login(user, pw)
        s.sendmail(user, to_addrs, msg.as_string())


def booking_link(state, course_key):
    c = (state.get("discovery") or {}).get("courses", {}).get(course_key, {})
    return BOOKING_PAGE.format(sched=c.get("schedule_id", "2431"))


def alert(config, state, new_slots):
    if not new_slots:
        return
    email_to = [os.environ["ALERT_EMAIL"]]
    sms_to = os.environ.get("SMS_EMAIL")
    ccfg = config.get("courses", {})

    by_course = {}
    for s in sorted(new_slots, key=lambda x: x["dt"]):
        by_course.setdefault(s["course"], []).append(s)

    for ckey, slots in by_course.items():
        channels = ccfg.get(ckey, {}).get("alerts", ["email"])
        lines = []
        for s in slots:
            star = "*" if s["weekday"] in ("Fri", "Sat", "Sun") else ""
            grp = " (2-4 spot!)" if s["spots"] >= 2 else ""
            lines.append(
                f'{star}{ckey.upper()} {s["weekday"]} {s["dt"].strftime("%-m/%-d")} '
                f'{s["time_label"]} - {s["spots"]} spot{"s" if s["spots"]!=1 else ""}{grp}'
            )
        link = booking_link(state, ckey)
        subject = f"[TEE] {ckey.upper()}: {len(slots)} opening{'s' if len(slots)!=1 else ''}"
        body = "\n".join(lines) + f"\n\nBook: {link}\n"

        if "email" in channels:
            send_email(config, email_to, subject, body)
        if "text" in channels and sms_to:
            sms_body = "\n".join(lines[:4]) + f"\n{link}"
            send_email(config, [sms_to], "", sms_body)
        print(f"alerted {ckey}: {len(slots)} slots via {channels}", flush=True)


# ----------------------------------------------------------------------------
# modes
# ----------------------------------------------------------------------------

def run_watch(config, state, session):
    days = list(range(0, config.get("lookahead_days", 7) + 1))
    new_slots = scan(session, config, state, days)
    alert(config, state, new_slots)
    print(f"watch pass done: {len(new_slots)} new slots")


def run_droprace(config, state, session):
    now = datetime.now(ET)
    drop = now.replace(hour=19, minute=0, second=0, microsecond=0)
    if now > drop + timedelta(minutes=10):
        drop += timedelta(days=1)
    warm = drop - timedelta(seconds=30)
    if warm - now > timedelta(minutes=80):
        print(f"not within drop window (now {now:%H:%M} ET); exiting to save minutes")
        return
    # the day that unlocks at this drop: resident window = 7 days ahead
    target_offset = config.get("resident_advance_days", 7)
    target_day = (drop.date() + timedelta(days=target_offset))
    tday = target_day.strftime("%A").lower()
    if not any(tday in c["windows"] for c in config["courses"].values()):
        # nothing we care about unlocks tonight, but cancellations spike at drop
        # time too, so do a couple of full passes and get out
        print(f"target day {target_day} not in windows; light pass only")
        run_watch(config, state, session)
        return

    wait = (warm - datetime.now(ET)).total_seconds()
    if wait > 0:
        print(f"sleeping {wait:.0f}s until 30s before {drop:%H:%M} ET drop")
        time.sleep(wait)

    deadline = drop + timedelta(minutes=10)
    print(f"drop-race polling for {target_day} until {deadline:%H:%M:%S} ET")
    while datetime.now(ET) < deadline:
        new_slots = scan(session, config, state, [target_offset])
        if new_slots:
            alert(config, state, new_slots)
            save_state(state)  # persist immediately once we hit
        time.sleep(config.get("droprace_poll_seconds", 5))
    print("drop-race window closed")


def run_probe(config, state, session):
    d = discover(session, config)
    print(json.dumps(d, indent=2))
    state["discovery"] = d


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "watch"
    config = load_config()
    state = load_state()
    session = requests.Session()
    try:
        {"watch": run_watch, "droprace": run_droprace, "probe": run_probe}[mode](
            config, state, session
        )
    finally:
        save_state(state)


if __name__ == "__main__":
    main()
