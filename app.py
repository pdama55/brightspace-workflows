#!/usr/bin/env python3
"""Brightspace -> SMS digest.

Primary data source is your personal Brightspace iCal subscription, which needs
no admin involvement. See README.md.
"""
from __future__ import annotations

import argparse
import re
import sys
import time as time_module
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional

from brightspace_sms.config import Config, ConfigError, load_config
from brightspace_sms.digest import build_digest
from brightspace_sms.feed import KIND_ASSIGNMENT, KIND_CLASS, collect_events
from brightspace_sms.notify import send_sms
from brightspace_sms.state import load_state, save_state


def emit_json(payload) -> int:
    """Machine-readable output for agent runtimes. Nothing else goes to stdout."""
    import json

    def default(value):
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if hasattr(value, "__dict__"):
            return {k: v for k, v in vars(value).items() if not k.startswith("_")}
        return str(value)

    print(json.dumps(payload, indent=2, default=default, ensure_ascii=False))
    return 0


def _window(config: Config, now: datetime, days: Optional[int] = None):
    span = config.lookahead_days if days is None else days
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=span + 1)


def _require_feeds(config: Config) -> None:
    if not config.feeds:
        raise ConfigError(
            "No calendar feeds configured. In Brightspace: Calendar -> Settings -> tick "
            "'Enable Calendar Feeds' -> Save -> Subscribe, then put the URL(s) in "
            "BRIGHTSPACE_ICAL_URLS in your .env."
        )


def _gather(config: Config, now: datetime, days: Optional[int] = None):
    _require_feeds(config)
    start, end = _window(config, now, days)
    return collect_events(config.feeds, config.tz, start, end)


def cmd_preview(config: Config, args) -> int:
    now = datetime.now(config.tz)
    events, errors = _gather(config, now)
    for error in errors:
        print(f"[feed error] {error}", file=sys.stderr)
    statuses, note = _fetch_statuses(config, now)
    digest = build_digest(events, now, config.lookahead_days, errors, statuses, note)
    if args.json:
        return emit_json({"header": digest.header, "text": digest.text, **digest.data})
    print(digest.text)
    return 0


def cmd_dump_feed(config: Config, args) -> int:
    now = datetime.now(config.tz)
    events, errors = _gather(config, now, days=args.days)
    for error in errors:
        print(f"[feed error] {error}", file=sys.stderr)
    if not events:
        print("No events in window.")
        return 0
    print(f"{len(events)} event(s) over the next {args.days} days:\n")
    print(f"{'KIND':<11} {'WHEN':<17} {'COURSE':<24} SUMMARY")
    print("-" * 100)
    for event in events:
        when = event.start.strftime("%a %m-%d") if event.all_day else event.start.strftime("%a %m-%d %H:%M")
        course = (event.course or "-")[:23]
        print(f"{event.kind:<11} {when:<17} {course:<24} {event.summary[:44]}")
    print(
        "\nIf a row is classified wrong, adjust ASSIGNMENT_WORDS / CLASS_WORDS "
        "in brightspace_sms/feed.py."
    )
    return 0


def cmd_check(config: Config, args) -> int:
    ok = True
    print(f"Timezone:       {config.tz_name}")
    print(f"Lookahead:      {config.lookahead_days} days")
    print(f"Daily send at:  {config.send_at.strftime('%H:%M')}")
    print(f"Poll interval:  {config.poll_interval}s")
    print(f"Dry run:        {config.dry_run}")

    if not config.feeds:
        print("\nFeeds:          NONE CONFIGURED (set BRIGHTSPACE_ICAL_URLS)")
        ok = False
    else:
        print(f"\nFeeds:          {len(config.feeds)} configured")
        now = datetime.now(config.tz)
        events, errors = _gather(config, now)
        for label, url in config.feeds:
            print(f"  - {label or '(unlabeled)'}: {url[:70]}")
        for error in errors:
            print(f"  ! {error}")
            ok = False
        classes = sum(1 for e in events if e.kind == KIND_CLASS)
        due = sum(1 for e in events if e.kind == KIND_ASSIGNMENT)
        print(f"  {len(events)} events in window ({classes} class, {due} due-style)")
        if events and not due:
            print("  ! No due-style events found. Run `dump-feed` to see what the feed returns.")

    from brightspace_sms.syllabus import load_schedules

    schedules = load_schedules()
    if schedules:
        print(f"\nSyllabi:        {len(schedules)} cached")
        for course, meetings in schedules.items():
            print(f"  - {course}: {len(meetings)} meetings")
    else:
        print("\nSyllabi:        none cached (run `parse-syllabus` to add class topics)")

    if config.dry_run:
        print("\nTwilio:         skipped (DRY_RUN=true)")
    else:
        try:
            config.require_twilio()
            print("\nTwilio:         configured")
        except ConfigError as exc:
            print(f"\nTwilio:         {exc}")
            ok = False

    if config.has_cookie:
        st = load_state()
        if st.get("cookie_fingerprint") == _cookie_fingerprint(config.bs_cookie) and st.get("cookie_first_ok"):
            first = datetime.fromisoformat(st["cookie_first_ok"])
            last = datetime.fromisoformat(st.get("cookie_last_ok", st["cookie_first_ok"]))
            alive = (last - first).total_seconds() / 3600
            died = st.get("cookie_died_at")
            print(f"\nCookie:         {'DEAD' if died else 'alive'}, "
                  f"{alive:.1f}h since first successful use")
        else:
            print("\nCookie:         configured, not yet exercised (run `probe`)")
    else:
        print("\nCookie:         not configured (no grades/submission status)")

    print("\nValence API:    " + ("credentials present" if config.has_valence else "not configured (optional)"))
    print("\n" + ("All good." if ok else "Problems found — see above."))
    return 0 if ok else 1


def _send_digest(config: Config, digest, state, now: datetime, reason: str) -> None:
    count = send_sms(config, digest.text)
    state["last_sent_date"] = now.date().isoformat()
    state["last_fingerprint"] = digest.fingerprint
    state["last_sent_at"] = now.isoformat()
    save_state(state)
    where = "printed" if config.dry_run else f"sent ({count} msg)"
    print(f"[{now:%Y-%m-%d %H:%M}] digest {where} — {reason}")


def run_once(config: Config, force: bool = False) -> None:
    now = datetime.now(config.tz)
    events, errors = _gather(config, now)
    for error in errors:
        print(f"[feed error] {error}", file=sys.stderr)

    statuses, note = _fetch_statuses(config, now, track=True)
    digest = build_digest(events, now, config.lookahead_days, errors, statuses, note)
    state = load_state()
    today = now.date().isoformat()
    already_sent_today = state.get("last_sent_date") == today

    if force:
        _send_digest(config, digest, state, now, "forced")
        return

    if digest.is_empty and not config.send_when_empty:
        print(f"[{now:%Y-%m-%d %H:%M}] nothing to report; SEND_WHEN_EMPTY is false.")
        return

    if not already_sent_today:
        if now.time() < config.send_at:
            print(f"[{now:%Y-%m-%d %H:%M}] waiting for {config.send_at:%H:%M}.")
            return
        _send_digest(config, digest, state, now, "daily digest")
        return

    if config.alert_on_change and digest.fingerprint != state.get("last_fingerprint"):
        _send_digest(config, digest, state, now, "changed since this morning")
        return

    print(f"[{now:%Y-%m-%d %H:%M}] already sent today, nothing changed.")


def cmd_once(config: Config, args) -> int:
    run_once(config, force=args.force)
    return 0


def cmd_run(config: Config, args) -> int:
    interval = args.poll_interval or config.poll_interval
    print(
        f"Polling every {interval}s. Daily digest at {config.send_at:%H:%M} {config.tz_name}. "
        f"{'DRY RUN — nothing will be texted.' if config.dry_run else ''}"
    )
    while True:
        try:
            run_once(config)
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001 - the loop must survive transient failures
            print(f"[{datetime.now(config.tz):%Y-%m-%d %H:%M}] poll failed: {exc}", file=sys.stderr)
        try:
            time_module.sleep(interval)
        except KeyboardInterrupt:
            print("\nStopped.")
            return 0


def _parse_all_syllabi(config: Config, args) -> int:
    """Parse every syllabus already downloaded into syllabi/."""
    from brightspace_sms.config import ROOT
    from brightspace_sms.syllabus import parse_syllabus, save_schedule

    term_start = None
    if args.term_start:
        try:
            term_start = date.fromisoformat(args.term_start)
        except ValueError:
            print("--term-start must be YYYY-MM-DD", file=sys.stderr)
            return 1

    files = sorted((ROOT / "syllabi").glob("*"))
    files = [f for f in files if f.suffix.lower() in {".pdf", ".docx", ".txt", ".md"}]
    if not files:
        print("No syllabi downloaded yet. Run: python app.py fetch-syllabi", file=sys.stderr)
        return 1

    failures = 0
    for path in files:
        course = path.name.split("-", 1)[0]
        course = re.sub(r"([A-Z]+)(\d+)", r"\1 \2", course)
        print(f"\n{course}: parsing {path.name} ...")
        try:
            data = parse_syllabus(path, course, config.anthropic_model, term_start)
        except Exception as exc:  # noqa: BLE001 - keep going through the batch
            print(f"  FAILED: {str(exc)[:140]}", file=sys.stderr)
            failures += 1
            continue
        meetings = data.get("meetings", [])
        dated = [m for m in meetings if m.get("date")]
        out = save_schedule(course, data)
        print(f"  {len(meetings)} meetings, {len(dated)} dated -> {out.name}")
    return 1 if failures == len(files) else 0


def cmd_parse_syllabus(config: Config, args) -> int:
    from brightspace_sms.syllabus import parse_syllabus, save_schedule

    if args.all:
        return _parse_all_syllabi(config, args)

    if not (args.course and args.file):
        print("Pass --course and --file, or use --all.", file=sys.stderr)
        return 1
    path = Path(args.file).expanduser()
    if not path.exists():
        print(f"No such file: {path}", file=sys.stderr)
        return 1

    term_start = None
    if args.term_start:
        try:
            term_start = date.fromisoformat(args.term_start)
        except ValueError:
            print("--term-start must be YYYY-MM-DD", file=sys.stderr)
            return 1

    print(f"Reading {path.name} with {config.anthropic_model}...")
    try:
        data = parse_syllabus(path, args.course, config.anthropic_model, term_start)
    except Exception as exc:  # noqa: BLE001 - a one-off command; report and exit
        print(f"Syllabus parse failed: {exc}", file=sys.stderr)
        return 1
    meetings = data.get("meetings", [])
    dated = [m for m in meetings if m.get("date")]
    out = save_schedule(args.course, data)

    print(f"Extracted {len(meetings)} meeting(s), {len(dated)} with resolvable dates -> {out}")
    if meetings and not dated:
        print(
            "None resolved to real dates. Re-run with --term-start YYYY-MM-DD so week "
            "numbers can be converted."
        )
    for meeting in dated[:5]:
        print(f"  {meeting['date']}  {meeting.get('topic', '')[:60]}")
    if len(dated) > 5:
        print(f"  ... and {len(dated) - 5} more")
    return 0


def _cookie_fingerprint(cookie: str) -> str:
    """Identify a pasted cookie without storing it, so re-pasting resets the clock."""
    import hashlib

    return hashlib.sha256(cookie.encode("utf-8")).hexdigest()[:12]


def _note_cookie_alive(config: Config, now: datetime) -> None:
    state = load_state()
    fingerprint = _cookie_fingerprint(config.bs_cookie)
    if state.get("cookie_fingerprint") != fingerprint:
        state["cookie_fingerprint"] = fingerprint
        state["cookie_first_ok"] = now.isoformat()
        state.pop("cookie_died_at", None)
    state["cookie_last_ok"] = now.isoformat()
    save_state(state)


def _note_cookie_dead(config: Config, now: datetime) -> str:
    """Record the death and report how long this cookie actually lasted."""
    state = load_state()
    if state.get("cookie_fingerprint") != _cookie_fingerprint(config.bs_cookie):
        return "[Brightspace cookie expired — re-paste BRIGHTSPACE_COOKIE]"
    if not state.get("cookie_died_at"):
        state["cookie_died_at"] = now.isoformat()
        save_state(state)
    first = state.get("cookie_first_ok")
    if not first:
        return "[Brightspace cookie expired — re-paste BRIGHTSPACE_COOKIE]"
    try:
        lifetime = now - datetime.fromisoformat(first)
    except ValueError:
        return "[Brightspace cookie expired — re-paste BRIGHTSPACE_COOKIE]"
    hours = lifetime.total_seconds() / 3600
    span = f"{hours:.0f}h" if hours < 48 else f"{hours / 24:.1f}d"
    return f"[Brightspace cookie expired after {span} — re-paste BRIGHTSPACE_COOKIE]"


def _fetch_statuses(config: Config, now: datetime, track: bool = False):
    """Submission status per assignment, when a session cookie is configured.

    Returns (statuses, note). Never raises: the digest is more useful late and
    incomplete than not at all, so an expired cookie degrades to feed-only with
    a line in the message telling you to re-paste it.
    """
    if not config.has_cookie:
        return {}, None

    from brightspace_sms.digest import match_key
    from brightspace_sms.session import SessionClient, SessionExpired

    try:
        client = SessionClient(config)
        since = now - timedelta(days=1)
        until = now + timedelta(days=config.lookahead_days + 1)
        statuses = {}
        for course in client.current_courses():
            for assignment in client.assignments(course, since=since, until=until):
                statuses[match_key(course.label, assignment.name)] = assignment
        if track:
            _note_cookie_alive(config, now)
        return statuses, None
    except SessionExpired:
        return {}, (
            _note_cookie_dead(config, now)
            if track
            else "[Brightspace cookie expired — re-paste BRIGHTSPACE_COOKIE]"
        )
    except Exception as exc:  # noqa: BLE001 - never let this sink the digest
        return {}, f"[submission status unavailable: {str(exc)[:60]}]"


def cmd_grades(config: Config, args) -> int:
    from brightspace_sms.session import AccessDenied, SessionClient, SessionExpired

    if not config.has_cookie:
        print("BRIGHTSPACE_COOKIE is not set — run `python app.py probe` for setup.", file=sys.stderr)
        return 1
    payload = []
    try:
        client = SessionClient(config)
        for course in client.current_courses():
            try:
                values = client.grades(course)
            except AccessDenied:
                continue
            if not values:
                continue
            if args.json:
                payload.append(
                    {
                        "course": course.label,
                        "course_name": course.name,
                        "org_unit_id": course.org_unit_id,
                        "items": [vars(v) for v in values],
                    }
                )
                continue
            print(f"\n{course.label} — {course.name}")
            for value in values:
                shown = value.displayed or (f"{value.points:g}" if value.points is not None else "—")
                print(f"  {shown:>10}   {value.name}")
        if args.json:
            return emit_json(payload)
    except SessionExpired as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


def cmd_materials(config: Config, args) -> int:
    """List each course's content tree — files, links, and module structure."""
    import re as _re

    from brightspace_sms.session import SessionClient, SessionExpired

    if not config.has_cookie:
        print("BRIGHTSPACE_COOKIE is not set — run `python app.py probe`.", file=sys.stderr)
        return 1

    pattern = _re.compile(args.match, _re.I) if args.match else None
    try:
        client = SessionClient(config)
        payload = []
        for course in client.current_courses():
            topics = client.content_tree(course.org_unit_id)
            shown = [t for t in topics if not pattern or pattern.search(t.title) or pattern.search(t.path)]
            if args.json:
                payload.append(
                    {
                        "course": course.label,
                        "org_unit_id": course.org_unit_id,
                        "total_topics": len(topics),
                        "topics": [
                            {"id": t.id, "title": t.title, "path": t.path, "kind": t.kind}
                            for t in shown[: args.limit]
                        ],
                    }
                )
                continue
            print(f"\n===== {course.label}  ({len(shown)}/{len(topics)} topics) =====")
            for topic in shown[: args.limit]:
                print(f"  [{topic.path[:30]:<30}] {topic.title[:52]}")
            if len(shown) > args.limit:
                print(f"  ... and {len(shown) - args.limit} more (raise --limit)")
        if args.json:
            return emit_json(payload)
    except SessionExpired as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


def cmd_fetch_syllabi(config: Config, args) -> int:
    """Download every course's Content Overview attachment (usually the syllabus)."""
    from brightspace_sms.config import ROOT
    from brightspace_sms.session import SessionClient, SessionExpired

    if not config.has_cookie:
        print("BRIGHTSPACE_COOKIE is not set — run `python app.py probe`.", file=sys.stderr)
        return 1

    dest = ROOT / "syllabi"
    try:
        client = SessionClient(config)
        courses = client.current_courses()
    except SessionExpired as exc:
        print(str(exc), file=sys.stderr)
        return 1

    found = 0
    for course in courses:
        path = client.overview_syllabus(course, dest)
        if path:
            found += 1
            print(f"  {course.label:<10} {path.stat().st_size:>8,}B  {path.name}")
        else:
            print(f"  {course.label:<10}        —  no Overview attachment")
    print(f"\n{found} file(s) in {dest}/")
    if found:
        print("Next: python app.py parse-syllabus --all --term-start YYYY-MM-DD")
    return 0


def cmd_fetch_schedules(config: Config, args) -> int:
    """Follow schedule pages instructors link from Content and extract them.

    Cheaper and more reliable than the syllabus path when it applies: no API
    key, and the page is the instructor's live source of truth.
    """
    from brightspace_sms.session import SessionClient, SessionExpired
    from brightspace_sms.syllabus import save_schedule
    from brightspace_sms.webschedule import SCHEDULE_HINT, schedule_from_url

    if not config.has_cookie:
        print("BRIGHTSPACE_COOKIE is not set — run `python app.py probe`.", file=sys.stderr)
        return 1
    try:
        term_start = date.fromisoformat(args.term_start)
    except ValueError:
        print("--term-start must be YYYY-MM-DD", file=sys.stderr)
        return 1

    try:
        client = SessionClient(config)
        courses = client.current_courses()
    except SessionExpired as exc:
        print(str(exc), file=sys.stderr)
        return 1

    written = 0
    for course in courses:
        candidates = [
            t for t in client.content_tree(course.org_unit_id) if SCHEDULE_HINT.search(t.title)
        ]
        for topic in candidates:
            detail = client.topic_detail(course.org_unit_id, topic.id)
            url = str(detail.get("Url") or "")
            # TopicType 3 is an external link; d2l quicklinks point back inside.
            if detail.get("TopicType") != 3 or not url.startswith("http"):
                continue
            try:
                data = schedule_from_url(url, course.label, term_start)
            except Exception as exc:  # noqa: BLE001 - one bad page must not stop the sweep
                print(f"  {course.label:<10} {topic.title[:34]:<36} fetch failed: {str(exc)[:50]}")
                continue
            if not data:
                print(f"  {course.label:<10} {topic.title[:34]:<36} no week grid found")
                continue
            out = save_schedule(course.label, data)
            written += 1
            dates = [m["date"] for m in data["meetings"]]
            print(f"  {course.label:<10} {topic.title[:34]:<36} "
                  f"{len(dates)} meetings, {min(dates)}..{max(dates)} -> {out.name}")
            break
    print(f"\n{written} schedule(s) written. Courses without a linked schedule page need "
          f"`fetch-syllabi` + `parse-syllabus --all`.")
    return 0


def cmd_probe(config: Config, args) -> int:
    """Discover which Valence endpoints this Brightspace instance exposes."""
    from brightspace_sms.session import (
        AccessDenied,
        SessionClient,
        SessionExpired,
        has_session_cookies,
    )

    if not config.has_cookie:
        print(
            "BRIGHTSPACE_COOKIE is not set. Get it from your browser:\n"
            "  1. Log into Brightspace, open DevTools (Cmd+Opt+I) -> Network\n"
            "  2. Reload the page, click the first request to your Brightspace host\n"
            "  3. Under Request Headers, right-click the `cookie:` line -> Copy value\n"
            "  4. Paste into .env as:  BRIGHTSPACE_COOKIE=\"<paste>\"",
            file=sys.stderr,
        )
        return 1
    if not has_session_cookies(config.bs_cookie):
        print(
            "That cookie string has no `d2lSessionVal` in it. Copy the whole `cookie:` "
            "request header, not just one value.",
            file=sys.stderr,
        )
        return 1

    client = SessionClient(config)
    print(f"Host: {config.bs_base_url}\n")

    try:
        versions = client.versions()
    except SessionExpired as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if versions:
        interesting = {k: v for k, v in versions.items() if k in ("lp", "le")}
        print(f"API versions: {interesting or versions}")
    else:
        print("API versions: could not read /d2l/api/versions/ — falling back to defaults")
    print(f"Using lp={client.lp}, le={client.le}\n")

    try:
        me = client.whoami()
    except SessionExpired as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"Authenticated as: {me.get('FirstName','')} {me.get('LastName','')} "
          f"({me.get('UniqueName','')}, id {me.get('Identifier','?')})\n")

    courses = client.current_courses()
    print(f"Current-term courses: {len(courses)}")
    for course in courses:
        print(f"  [{course.org_unit_id}] {course.label:<12} {course.name}")
    if not courses:
        print("  (none — the enrollments endpoint returned nothing)")
        return 1

    sample = courses[: args.courses]
    print(f"\nProbing endpoints against {len(sample)} course(s):\n")
    results = []
    for course in sample:
        for name, fn in (
            ("dropbox/folders (submission status)", client.assignments),
            ("grades/myGradeValues (grades)", client.grades),
        ):
            try:
                rows = fn(course)
                status = f"OK — {len(rows)} row(s)"
                results.append((course.label, name, True, rows))
            except AccessDenied as exc:
                status = f"no permission — {str(exc)[:60]}"
                results.append((course.label, name, False, []))
                print(f"  {course.label:<12} {name:<38} {status}")
                continue
            except SessionExpired:
                raise
            except Exception as exc:  # noqa: BLE001 - this command exists to report failures
                status = f"FAILED — {str(exc)[:90]}"
                results.append((course.label, name, False, []))
            print(f"  {course.label:<12} {name:<38} {status}")

    print("\nSample rows:")
    for label, name, ok, rows in results:
        if ok and rows:
            print(f"\n  {label} — {name}")
            for row in rows[:3]:
                print(f"    {row}")

    working = {name for _, name, ok, rows in results if ok and rows}
    print("\n" + ("Usable endpoints: " + ", ".join(sorted(working)) if working
                   else "No endpoint returned data. Paste this output back to me."))
    return 0


def cmd_valence(config: Config, args) -> int:
    if not config.has_valence:
        print(
            "No Valence credentials configured. This command needs an OAuth app registered "
            "by a Brightspace admin at your school; the iCal feed path does not.",
            file=sys.stderr,
        )
        return 1
    from brightspace_sms.valence import ValenceClient, upcoming_assignments

    client = ValenceClient(config)
    who = client.whoami()
    print(f"Authenticated as {who.get('FirstName', '')} {who.get('LastName', '')} (id {who.get('Identifier')})")
    assignments = upcoming_assignments(config, config.lookahead_days)
    if not assignments:
        print(f"No assignments due in the next {config.lookahead_days} days.")
        return 0
    for assignment in assignments:
        due = assignment.due.astimezone(config.tz).strftime("%a %b %d %H:%M") if assignment.due else "no due date"
        print(f"  {due}  {assignment.course}: {assignment.title}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    # The loop is long-lived and usually redirected to a log file; without this,
    # block buffering means the log stays empty for hours.
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")

    p_run = sub.add_parser("run", help="run the polling loop (default)")
    p_run.add_argument("--poll-interval", type=int, default=None, help="seconds between polls")
    p_run.set_defaults(func=cmd_run)

    p_once = sub.add_parser("once", help="evaluate once and exit")
    p_once.add_argument("--force", action="store_true", help="send even if already sent today")
    p_once.set_defaults(func=cmd_once)

    p_prev = sub.add_parser("preview", help="print the digest without sending")
    p_prev.add_argument("--json", action="store_true", help="machine-readable output")
    p_prev.set_defaults(func=cmd_preview)

    p_dump = sub.add_parser("dump-feed", help="show every parsed calendar event and its classification")
    p_dump.add_argument("--days", type=int, default=14)
    p_dump.set_defaults(func=cmd_dump_feed)

    sub.add_parser("check", help="validate config and feeds").set_defaults(func=cmd_check)

    p_syl = sub.add_parser("parse-syllabus", help="extract a date->topic table from a syllabus")
    p_syl.add_argument("--all", action="store_true", help="parse everything in syllabi/")
    p_syl.add_argument("--course", help="course label, e.g. 'CS 201'")
    p_syl.add_argument("--file", help="path to a .pdf, .docx, .txt or .md syllabus")
    p_syl.add_argument("--term-start", help="YYYY-MM-DD, so 'Week 3' can be resolved to a date")
    p_syl.set_defaults(func=cmd_parse_syllabus)

    p_grades = sub.add_parser("grades", help="show your current grades")
    p_grades.add_argument("--json", action="store_true", help="machine-readable output")
    p_grades.set_defaults(func=cmd_grades)

    p_sched = sub.add_parser(
        "fetch-schedules", help="extract schedules from pages linked in Content"
    )
    p_sched.add_argument("--term-start", required=True, help="first day of classes, YYYY-MM-DD")
    p_sched.set_defaults(func=cmd_fetch_schedules)

    sub.add_parser(
        "fetch-syllabi", help="download each course's syllabus from Brightspace"
    ).set_defaults(func=cmd_fetch_syllabi)

    p_mat = sub.add_parser("materials", help="list each course's content tree")
    p_mat.add_argument("--match", help="only show topics matching this regex")
    p_mat.add_argument("--limit", type=int, default=25)
    p_mat.add_argument("--json", action="store_true", help="machine-readable output")
    p_mat.set_defaults(func=cmd_materials)

    p_probe = sub.add_parser("probe", help="discover which API endpoints your school exposes")
    p_probe.add_argument("--courses", type=int, default=2, help="how many courses to test against")
    p_probe.set_defaults(func=cmd_probe)

    sub.add_parser("valence", help="test the optional Brightspace REST API path").set_defaults(func=cmd_valence)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args = parser.parse_args((argv or []) + ["run"])

    try:
        config = load_config()
        return args.func(config, args)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
