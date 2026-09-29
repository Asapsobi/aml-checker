# Re-screening on a schedule

`amlcheck watch run` re-screens every address on the watchlist and reports the verdicts that
changed (PRD U4). This page sets it to run by itself once a day.

A change is reported in three ways (Q12):

- **Printed**, in a table of every watched address with its verdict before and now.
- **Kept in the audit log.** Every re-screen is a normal check, with the note `watchlist re-screen`.
- **Signalled.** The command exits with status 6, and on macOS it shows a notification.

Status 0 means no verdict changed, and 1 means the run could not start.

## Before you schedule it

1. Add addresses to the watchlist:

   ```bash
   uv run amlcheck watch add TJwwz9NR37hjXdAV5gowj7src4avMuZZNW --client "ACME Ltd" --note "approved 2026-09"
   uv run amlcheck watch list
   ```

   An address starts from its latest check in the audit log. One never checked gets its first verdict
   on the next run, and a first verdict is not a change.
2. Run it once by hand: `uv run amlcheck watch run`.
3. Keep the sanctions list fresh. A list older than 48 hours makes every check INCOMPLETE, and that
   shows up as a change. The jobs below run `amlcheck sync sanctions` first.

## macOS: launchd

launchd is the macOS scheduler. Unlike cron, a run missed while the Mac was asleep happens as soon as
it wakes.

Save this as `~/Library/LaunchAgents/com.amlcheck.watch.plist`. Replace `/Users/you/aml-checker`
with the folder of this project, which holds `.env` with your keys:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.amlcheck.watch</string>
    <key>WorkingDirectory</key>
    <string>/Users/you/aml-checker</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/sh</string>
        <string>-c</string>
        <string>.venv/bin/amlcheck sync sanctions; .venv/bin/amlcheck watch run</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>9</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>/Users/you/.amlcheck/logs/watch.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/you/.amlcheck/logs/watch.log</string>
</dict>
</plist>
```

Then load it, and run it once now to test it:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.amlcheck.watch.plist
launchctl kickstart gui/$(id -u)/com.amlcheck.watch
tail -n 30 ~/.amlcheck/logs/watch.log
```

To stop the schedule:

```bash
launchctl bootout gui/$(id -u)/com.amlcheck.watch
```

**Notifications.** They come from AppleScript, so macOS lists them under Script Editor. If none
appears, allow notifications for Script Editor in System Settings → Notifications. The log file
always has the full table.

## Linux: cron

Run `crontab -e` and add a line like this one, with the path of this project:

```cron
0 9 * * * cd /home/you/aml-checker && .venv/bin/amlcheck sync sanctions; .venv/bin/amlcheck watch run >> ~/.amlcheck/logs/watch.log 2>&1
```

cron skips a run while the machine is off or asleep. There are no desktop notifications on Linux,
so check the log, or act on the exit status 6 in a script of your own.
