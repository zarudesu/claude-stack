# Architecture

- The ledger rejects negative amounts; `add()` raises `ValueError` when the
  amount is negative.
- Export supports both CSV and JSON output formats.
- Notify delivers messages by email through SMTP, with retries on failure.
- `app/scheduler.py` runs the export job every night on a cron schedule.
- Web state colours: green means yes, red means no, gray means unknown.
