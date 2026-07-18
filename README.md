# SourceAlert

Monitor public web sources for meaningful updates and send alerts to your phone.

SourceAlert is a lightweight, open-source personal monitor. Add public URLs to
`sources.yaml`; it checks them on a schedule, stores version history in SQLite,
and notifies you through SMS, ntfy, or Telegram when content is new or updated.

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

## Free cloud setup: GitHub Actions + ntfy

This is the recommended setup for personal use. GitHub runs one short check
every ten minutes, and ntfy delivers a normal push notification to your phone.
For a public repository using standard GitHub-hosted runners, this setup does
not need Railway, Twilio, a server, or a credit card.

1. Install **ntfy** from the iOS App Store or Google Play.
2. Make a long random topic name, for example
   `source-alert-7f3b1c9e-your-own-random-words`. Treat it like a password.
3. In ntfy, tap **Subscribe to topic**, use the default `ntfy.sh` server, and
   enter that exact topic name.
4. In this GitHub repository, open **Settings → Secrets and variables →
   Actions → New repository secret**.
5. Name the secret `NTFY_TOPIC`; paste only the topic name as its value.
6. Open **Actions → Monitor sources → Run workflow**. Leave the test box off
   for the first run. This quietly saves the current page as the baseline.
7. Run it a second time with **Send a test notification to the phone** checked.
   A SourceAlert test should appear on the phone.

After that, no computer needs to remain on. The workflow in
`.github/workflows/monitor.yml` runs automatically. Its SQLite state contains
only copies of public source content and is committed when a source changes;
the private ntfy topic remains encrypted in GitHub Secrets.
The workflow also writes one tiny monthly heartbeat commit so GitHub does not
disable the schedule after a long period without repository activity.

GitHub scheduled jobs can occasionally start a few minutes late during busy
periods, so this is useful supplemental monitoring, not a guaranteed emergency
delivery system.

## SMS with Twilio

Create a Twilio account, obtain an SMS-capable Twilio phone number, and verify
your personal recipient number if the account is still in trial mode. Add these
private values to `.env` locally or to your cloud provider's encrypted
environment variables:

```text
TWILIO_ACCOUNT_SID=...
TWILIO_AUTH_TOKEN=...
TWILIO_FROM_NUMBER=+12065550123
SMS_TO_NUMBER=+16085550123
```

Phone numbers must use E.164 format: `+`, country code, and number. Do not put
real credentials or phone numbers in `sources.yaml`, source code, or GitHub.

Test delivery:

```bash
python source_alert.py --test-notification
```

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

The free GitHub Actions workflow has its own schedule in
`.github/workflows/monitor.yml`. Change `*/10 * * * *` to `*/30 * * * *` for
every 30 minutes. Scheduled workflows use UTC, and GitHub's shortest supported
schedule interval is five minutes.

## Docker

```bash
docker build -t source-alert .
docker run --env-file .env -v source-alert-data:/app/data source-alert
```

## Deploy continuously on Railway

1. In Railway, create a project with **Deploy from GitHub repo** and choose
   `sherryxia404/source-alert`.
2. Railway detects the included `Dockerfile`; no public domain is required.
3. Add a persistent Volume mounted at `/app/data`.
4. Add the environment variables from `.env.example` in Railway's Variables
   panel. Set `DATABASE_PATH=/app/data/source_alert.sqlite3`.
5. Add the four Twilio SMS variables above as private Railway variables.
6. Deploy. The Docker process stays running and checks every five minutes.

Every push to the connected GitHub branch can trigger a new deployment. The
Volume preserves change history across deployments.

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
