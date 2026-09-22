# Warneford School calendar feed

Mirrors the Highworth Warneford School website calendar into a subscribable
`.ics` feed, updated twice a day by GitHub Actions.

## How it works

The school website renders its calendar client-side from a JSON endpoint:

```
https://www.warnefordschool.org/calendar/api.asp
    ?pid=9&viewid=1&calid=1&bgedit=false
    &start=YYYY-MM-DD&end=YYYY-MM-DD&_=<epoch_ms>
```

No cookies or authentication. `school_calendar.py` walks a rolling window of
months (1 back, 13 ahead by default), dedupes by the school's own event id,
and writes `calendar.ics`.

`state.json` tracks a content hash per event so `SEQUENCE` and `LAST-MODIFIED`
only change when the school actually changes something. That keeps the output
byte-identical between runs, so git only records a commit on a real change.

## Setup

1. Create a **public** repo (see note below) and add these files.
2. Run the workflow once by hand: **Actions → Update school calendar → Run workflow**.
   It should commit `calendar.ics` and `state.json`.
3. **Settings → Pages → Source: Deploy from a branch → `main` / root.**
4. Your feed URL is:
   `https://<username>.github.io/<repo>/calendar.ics`
5. Subscribe to it (see below).

### Why public

Subscribed calendars are fetched anonymously — Outlook and Google can't present
a token — so the `.ics` must be reachable without authentication. On a private
repo, `raw.githubusercontent.com` URLs require a token and Pages needs a paid
plan. The school's calendar is already public information, so there is nothing
sensitive here. Don't add anything personal (child's name, class, etc.) to the
repo.

### Keeping the schedule alive

GitHub disables scheduled workflows after 60 days of repository inactivity, and
commits made by the built-in `GITHUB_TOKEN` may not count as activity. To be
safe, create a fine-grained PAT with **Contents: read and write** on this repo
only, and save it as the repository secret `CALENDAR_PAT`. The workflow picks it
up automatically. Without it, expect to re-enable the schedule occasionally —
GitHub emails you before it does this.

## Subscribing

Use the *subscribe to a URL* option, not *import file*. Import is a one-off
snapshot; subscribe keeps updating.

- **iPhone/iPad:** Settings → Apps → Calendar → Calendar Accounts → Add Account
  → Other → Add Subscribed Calendar. iOS lets you choose the refresh interval —
  set it to hourly. This is the best-behaved client by some distance.
- **Outlook.com / Microsoft 365 web:** Calendar → Add calendar → Subscribe from web.
- **Google Calendar (desktop web only):** Other calendars → + → From URL.
- **Thunderbird:** New Calendar → On the Network → iCalendar (ICS).

Refresh frequency is the client's decision, not yours. Google and Outlook can
take 24 hours or more to pick up a change. For school dates that's fine, but it
means a subscription alone isn't a same-day alerting mechanism.

## Getting alerted to changes

Because every update is a commit, watch your own repo (**Watch → All Activity**)
and GitHub emails you on each change, with a diff showing exactly which date was
added or moved. That is more immediate and more informative than the calendar
refresh.

Workflow failures also email you by default, so a scrape that breaks because the
school changed their website won't fail silently.

## Local testing

```bash
python school_calendar.py --out calendar.ics --state state.json
python school_calendar.py --json-file sample.json --out test.ics --state /tmp/s.json
```

`--min-events` (default 1) aborts without writing if the fetch returns
suspiciously little, so a broken scrape can't wipe a working feed.

## Notes and known rough edges

- **Times are converted to UTC** using Europe/London, which sidesteps having to
  embed a `VTIMEZONE` block. BST/GMT transitions are handled by `zoneinfo`.
- **All-day `DTEND` is exclusive**, matching both the API and RFC 5545. A
  three-day event ending on the 16th is written as `DTEND:20260917`.
- **Multi-day timed events become daily recurrences.** The API returns the
  careers evening as start 18/09 17:00, end 23/09 19:30 — which read literally
  is one continuous 126-hour block. It isn't: the API's own `recurrence` text
  says "between 5:00pm and 7:30pm on 18/09/2026 until 23/09/2026", i.e. six
  evening sessions. Any timed event whose end date is later than its start date,
  and whose end time-of-day is later than its start time-of-day, is written as
  `FREQ=DAILY` with the end clamped to day one. Pass `--no-daily-expand` to
  disable this if a genuinely continuous multi-day event ever appears — the API
  gives no structured field distinguishing the two cases, only the human-readable
  `recurrence` string.
- **Timed events use `TZID=Europe/London`, not UTC.** This is why there is a
  `VTIMEZONE` block. With a UTC `DTSTART`, a `FREQ=DAILY` rule spanning a clock
  change would hold the UTC time fixed and drift the local time by an hour.
  Verified: a daily 17:00 run across the October transition stays at 17:00 local.
- **`desc` was empty on every captured event.** Descriptions are stripped of
  HTML tags and entity-decoded, but that path is untested against real content.
- **The date window is a guess.** The site requests roughly a month at a time;
  whether `api.asp` caps very wide ranges is untested, which is why this fetches
  month by month rather than one big range.
- If the school ever publishes an official iCal feed, use that instead and
  delete this.
