# Brightspace SMS digest

Texts you, once a morning:

```
Wed Sep 2

CLASS TODAY
9:30am CS 201 Algorithms: Lecture (Klaus 1443)
   Dynamic programming: memoization vs tabulation · read CLRS ch. 15.1-15.3

DUE (next 7d)
Today 11:59pm — CS 201 Algorithms: Problem Set 3 is due
Sat — BIOL 110: Quiz 2
Mon 5pm — HIST 101: Reading response 4
```

## How it gets the data

**Assignments and class times come from your Brightspace iCal subscription.**
Brightspace lets any student generate a personal calendar feed URL — no admin
involvement, no OAuth app, no scraping, and no session cookie that expires every
few days. That URL serves plain `.ics` over HTTPS forever, and it updates when
instructors change due dates.

**Class topics come from your syllabus, parsed once per term.** Brightspace has
no structured record of what a class is *doing* on a given day — "Week 6:
Dynamic Programming" lives in a PDF a human wrote. `parse-syllabus` extracts that
into a `date -> topic` table once; the daily digest just looks today's date up.
This is the one part that is a static artifact you extracted, not a live feed.

**Submission status and grades come from the REST API, authenticated with your
browser session cookie.** Brightspace's Valence API is real and documented, but
getting an OAuth client ID needs the *Can Manage API Applications* permission,
which schools don't grant to students. The web UI itself calls those endpoints
with your session cookies, so `brightspace_sms/session.py` does the same — you,
your credentials, your data. The catch is that SSO (BoilerKey/Duo) means the
session can't be renewed unattended, so you re-paste the cookie when it expires.
`brightspace_sms/valence.py` holds the OAuth path for the day an admin says yes.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

### 1. Get your calendar feed URL

In Brightspace: **Calendar → Settings → tick "Enable Calendar Feeds" → Save.**
A **Subscribe** button then appears on the Calendar nav bar; it gives you a
`webcal://` URL. Use the "all calendars" option for a single URL, or grab one per
course.

Put it in `.env`:

```
BRIGHTSPACE_ICAL_URLS=webcal://school.brightspace.com/d2l/le/calendar/feed/user/feed.ics?token=abc123
```

For multiple feeds, **wrap the value in double quotes** — `.env` silently drops
unquoted continuation lines. An optional `LABEL=` prefix names the course for
events that don't identify their own:

```
BRIGHTSPACE_ICAL_URLS="CS 201=webcal://.../feed1.ics
BIOL 110=webcal://.../feed2.ics"
```

Comma-separated on one line works too.

> **If "Enable Calendar Feeds" isn't in Calendar → Settings**, your institution
> disabled feeds org-wide. There is no student-side workaround; you'd need an
> admin to either turn feeds on or register an OAuth app.

Treat the feed URL like a password — anyone with it can read your calendar.

### 2. Check it

```bash
python app.py check        # validates config, fetches feeds, counts events
python app.py dump-feed    # every parsed event + how it was classified
python app.py preview      # the digest, printed, never sent
```

`dump-feed` is the tuning tool. Brightspace's iCal output varies by school and
D2L version, so if something is filed as a class when it's an assignment (or
vice versa), adjust `ASSIGNMENT_WORDS` / `CLASS_WORDS` in
[brightspace_sms/feed.py](brightspace_sms/feed.py).

### 3. Add class topics (optional)

Most instructors attach the syllabus to the Content **Overview** page, which sits
outside the module tree — so pull those first:

```bash
python app.py fetch-syllabi                                   # downloads to syllabi/
python app.py parse-syllabus --all --term-start 2026-08-24
```

`--term-start` is the first day of classes; it lets "Week 3, Tuesday" resolve to a
real date. Without it, syllabi that use week numbers extract nothing usable.

For a course whose syllabus isn't in Brightspace, point at a file yourself:

```bash
python app.py parse-syllabus --course "CS 201" --file ~/Downloads/cs201.pdf
```

Accepts `.pdf`, `.docx`, `.txt`, and `.md`. Results land in
`schedules/<course>.json`, which you can hand-edit. Needs `ANTHROPIC_API_KEY`;
nothing else in the project does. Re-run once a term.

Use `python app.py materials` to browse any course's content tree — several
instructors encode the schedule in the module structure (`Week 5 / Wednesday`)
rather than in a syllabus document.

### 4. Grades and submission status

This is what turns "here's what's due" into "here's what's *left*". Without it the
digest lists everything due; with it, submitted work moves to a `DONE` section.

Get your session cookie from the browser:

1. Log into Brightspace, open DevTools (**Cmd+Opt+I**) → **Network** tab
2. Reload the page
3. Click the first request to your Brightspace host
4. Under **Request Headers**, right-click the `cookie:` line → **Copy value**
5. Paste into `.env`, in double quotes:

```
BRIGHTSPACE_COOKIE="d2lSessionVal=...; d2lSecureSessionVal=..."
```

Then confirm it works:

```bash
python app.py probe     # discovers API versions, courses, and which endpoints respond
python app.py grades    # your current grades
```

**Treat this cookie exactly like your password** — it is full access to your
Brightspace account, not a read-only token. It lives only in `.env`, which is
gitignored. Never paste it anywhere else.

**Refreshing the cookie.** Sessions expire. When they do, the digest keeps
working from the calendar feed and adds a line telling you to re-paste; `probe`
says so outright. Repeat the steps above — it takes about 20 seconds.

### 5. Turn on texting

Set your Twilio credentials in `.env` and flip `DRY_RUN=false`. Twilio runs about
$1/month for the number plus a fraction of a cent per message. Carrier
email-to-SMS gateways are free but drop messages silently, so they're a bad fit
for something you're meant to rely on.

### 6. Run it

```bash
python app.py run          # the always-running loop
```

Each poll it decides:

- haven't texted today and it's past `SEND_AT` → send the daily digest
- already texted today and the list changed → send once more (`ALERT_ON_CHANGE`)
- otherwise → do nothing

State lives in `.state.json`, so restarting the loop doesn't re-text you.

To keep it running across reboots, a `launchd` job or `tmux` session is enough —
it's a single long-lived process with no external state.

## Commands

| Command | What it does |
|---|---|
| `run` | The polling loop (default if you pass no command) |
| `once` | Evaluate one cycle and exit. `--force` sends regardless of state |
| `preview` | Print the digest; never sends, never writes state |
| `dump-feed` | Every parsed event with its classification. `--days N` |
| `check` | Validate config, fetch feeds, report what was found |
| `parse-syllabus` | Extract a date→topic table from a syllabus |
| `grades` | Your current grades, per course |
| `fetch-syllabi` | Download each course's Content Overview attachment |
| `materials` | Browse a course's content tree. `--match REGEX`, `--limit N` |
| `probe` | Discover which API endpoints your school exposes, and test the cookie |
| `fetch-schedules` | Extract schedules from pages instructors link in Content |
| `valence` | Test the optional OAuth REST path |

## Using it from an agent runtime

`preview`, `grades`, and `materials` take `--json`, which prints a single JSON
document to stdout and nothing else — warnings go to stderr. That makes each one
usable as a tool for a self-hosted agent (Hermes, OpenClaw, or your own):

```bash
python app.py preview --json     # {date, in_class_today, todo[], done[], notes[], errors[]}
python app.py grades --json      # [{course, org_unit_id, items[]}]
python app.py materials --json   # [{course, total_topics, topics[]}]
```

If you go that route, the agent's own scheduler replaces `app.py run` and its
messaging gateway replaces `notify.py` — delete both and keep `brightspace_sms/`
as a pure data layer.

**Treat these outputs as untrusted input.** Assignment titles, instructor
feedback, syllabus text, and linked schedule pages are all written by other
people. This script only formats them into a text message, so injected
instructions are inert here — an agent that can take actions is a different
story. Give it read-only Brightspace access and nothing adjacent.

## Layout

```
app.py                      CLI
brightspace_sms/
  config.py                 .env loading
  feed.py                   iCal fetch, recurrence expansion, classification
  digest.py                 message assembly
  syllabus.py               syllabus -> date/topic table
  notify.py                 Twilio delivery + chunking
  state.py                  dedupe state
  session.py                cookie-authenticated REST client (grades, submissions)
  webschedule.py            parses schedule pages linked from Content
  valence.py                optional OAuth REST client
schedules/                  generated per-course schedules
syllabi/                    syllabi downloaded from Brightspace
```

## Known limits

- **Feed classification is heuristic.** Brightspace doesn't label events as
  "assignment" vs "class meeting"; the split is inferred from duration and
  keywords. Check `dump-feed` at the start of term.
- **The feed only knows what instructors put in Brightspace.** A due date that
  only exists in a syllabus won't appear. The syllabus parser covers class topics
  but does not currently backfill due dates.
- **Submission status only covers Assignments (dropbox folders).** Quizzes and
  discussions have no student-readable submission endpoint, so they always stay
  in `TO DO`. Unknown is deliberately not treated as done.
- **The session cookie expires** and cannot be auto-renewed through SSO. The
  digest degrades to feed-only rather than failing.
- **Feed URLs can be revoked.** Regenerating the subscription in Brightspace
  invalidates the old URL; the script falls back to the last good cached copy in
  `.feed_cache/` and reports the error rather than going silent.
- **Long digests are split** into at most 4 SMS messages, then truncated.
