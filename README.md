# SourceAlert

Monitor public web sources for meaningful updates and send alerts to your phone.

SourceAlert is a lightweight, open-source personal monitor. Add public URLs to
`sources.yaml`; it checks them on a schedule, stores version history in SQLite,
and notifies you through ntfy or Telegram when content is new or updated.

The project began with a practical problem: campus community members may want
public safety updates even when they cannot use a university's account-based
alert system. UW Seattle Alert is included as the first example, but SourceAlert
is not limited to campuses or emergency information.

## Supported sources

- RSS and Atom feeds
- WordPress sites with discoverable feeds
- Ordinary public HTML pages
- A specific page region selected with CSS

SourceAlert does not bypass authentication, CAPTCHAs, paywalls, or access
controls. Highly dynamic JavaScript applications may require a future adapter.

## Quick start

```bash
git clone https://github.com/sherryxia404/source-alert.git
cd source-alert
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Install the **ntfy** mobile app, subscribe to a long random topic, and place that
topic in `.env`:

```text
NTFY_TOPIC=your-long-private-random-topic
```

Test the phone connection:

```bash
python source_alert.py --test-notification
```

Start monitoring:

```bash
python source_alert.py
```

The first check for each source establishes a baseline silently. Later new or
edited content triggers a notification.

## Add a source

Edit `sources.yaml`:

```yaml
sources:
  - name: UW Seattle Alert
    url: https://emergency.uw.edu/
    type: auto
    enabled: true

  - name: A public RSS feed
    url: https://example.com/feed.xml
    type: rss
    enabled: true

  - name: One section of a webpage
    url: https://example.com/status
    type: webpage
    selector: "#current-status"
    enabled: true
```

`type: auto` first looks for RSS/Atom discovery metadata. If no feed is found,
it monitors the page's main content.

## Timing

The default interval is five minutes. Change it in `.env`:

```text
CHECK_INTERVAL_SECONDS=600
```

Intervals below 60 seconds are rejected to avoid unnecessary load on source
websites. For cron or a cloud scheduler, run one check with:

```bash
python source_alert.py --once
```

The SQLite database must persist between runs.

## Docker

```bash
docker build -t source-alert .
docker run --env-file .env -v source-alert-data:/app/data source-alert
```

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

## Privacy and safety

- `.env`, notification credentials, topics, and databases are gitignored.
- Public ntfy topics act like passwords; use a long random topic.
- SourceAlert is supplemental. It is not a replacement for 911, official
  emergency systems, or instructions from public safety officials.

## How change detection works

RSS items use their GUID or link as identity. Static webpages use their URL.
SourceAlert hashes normalized title and body content:

- unknown identity: new item;
- known identity with a different hash: updated item;
- same identity and hash: no notification.

Every distinct version is saved in SQLite for later inspection.
