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

## Keeping the data fresh

Run `amlcheck sync` every 6 hours, even without a watchlist. A sanctions list older than 48 hours
makes every check INCOMPLETE. The OFAC file is about 28 MiB, and on a slow line a download can fail
(V22). Four tries a day leave plenty of room before the list goes stale. Each check refreshes the
TRON index by itself.

**macOS.** Save this as `~/Library/LaunchAgents/com.amlcheck.sync.plist`, with your user name in the
paths. It runs the installed `amlcheck` at 03:00, 09:00, 15:00 and 21:00, and at each login. A time
missed while the Mac sleeps runs when it wakes:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.amlcheck.sync</string>
    <key>WorkingDirectory</key>
    <string>/Users/you</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/you/.local/bin/amlcheck</string>
        <string>sync</string>
    </array>
    <key>StartCalendarInterval</key>
    <array>
        <dict><key>Hour</key><integer>3</integer><key>Minute</key><integer>0</integer></dict>
        <dict><key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer></dict>
        <dict><key>Hour</key><integer>15</integer><key>Minute</key><integer>0</integer></dict>
        <dict><key>Hour</key><integer>21</integer><key>Minute</key><integer>0</integer></dict>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/Users/you/.amlcheck/logs/sync.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/you/.amlcheck/logs/sync.log</string>
</dict>
</plist>
```

Load it with `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.amlcheck.sync.plist`,
and stop it with `launchctl bootout gui/$(id -u)/com.amlcheck.sync`. Don't use launchd's
`StartInterval` for this: it skips a run that falls while the Mac sleeps.

**Linux.** With cron, use `0 */6 * * * $HOME/.local/bin/amlcheck sync >> $HOME/.amlcheck/logs/sync.log 2>&1`.
On a server, use the systemd timer in [server.md](server.md).

`amlcheck status` warns once the list is more than 24 hours old, while checks still work. If the
warning stays, read `~/.amlcheck/logs/sync.log`: each run ends with the list it got or the reason it
failed.
