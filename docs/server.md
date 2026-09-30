# Installing on a server

The corridor system runs on its own server, so amlcheck is installed there too, and the corridor
calls its API on that server's 127.0.0.1 (Q18). Nothing is opened to the network. This page sets it
up on Linux with systemd. macOS works the same way with launchd ([scheduling.md](scheduling.md)).

amlcheck is installed as a tool, straight from GitHub, with [uv](https://docs.astral.sh/uv/) or
pipx (Q20). Either one puts it in an environment of its own. uv also fetches a Python 3.12 or newer
when the server has none.

## 1. A user and the tool

Run amlcheck as a user of its own, so its keys and audit log belong to that user only:

```bash
sudo useradd --system --create-home --home-dir /var/lib/amlcheck --shell /usr/sbin/nologin amlcheck
sudo -u amlcheck -H sh -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
```

Install a fixed commit or tag, so the server runs the version you reviewed. Every check records its
`tool_version`:

```bash
sudo -u amlcheck -H /var/lib/amlcheck/.local/bin/uv tool install "git+https://github.com/Asapsobi/aml-checker@<commit or tag>"
sudo -u amlcheck -H /var/lib/amlcheck/.local/bin/amlcheck --version
```

With pipx instead: `pipx install "git+https://github.com/Asapsobi/aml-checker@<commit or tag>"`.
Without `@…`, either one installs the latest `main`.

## 2. Keys and settings

amlcheck keeps its database, logs, `config.toml` and `.env` in `~/.amlcheck/`, which for this user
is `/var/lib/amlcheck/.amlcheck/`. Create the `.env` file readable by that user only. In it, set
the keys from `.env.example` and a new `AMLCHECK_API_TOKEN`
([api.md](api.md#running-it)):

```bash
sudo -u amlcheck -H mkdir -p /var/lib/amlcheck/.amlcheck
sudo -u amlcheck -H sh -c 'umask 077; cat > /var/lib/amlcheck/.amlcheck/.env' <<'EOF'
EAGLE_VIRTUAL_API_KEY=ev_live_...
TRONGRID_API_KEY=...
HYPERSYNC_API_TOKEN=...
AMLCHECK_API_TOKEN=...
EOF
```

Settings go in `/var/lib/amlcheck/.amlcheck/config.toml` (see `config.example.toml`). Without it,
the PRD's defaults apply. The first sync takes a few minutes. Then `status` shows each source:

```bash
sudo -u amlcheck -H /var/lib/amlcheck/.local/bin/amlcheck sync
sudo -u amlcheck -H /var/lib/amlcheck/.local/bin/amlcheck status
```

## 3. The API as a service

Save this as `/etc/systemd/system/amlcheck-api.service`:

```ini
[Unit]
Description=amlcheck local API (127.0.0.1:8766)
After=network-online.target
Wants=network-online.target

[Service]
User=amlcheck
Group=amlcheck
WorkingDirectory=/var/lib/amlcheck
ExecStart=/var/lib/amlcheck/.local/bin/amlcheck api --port 8766
Restart=on-failure
RestartSec=5
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ReadWritePaths=/var/lib/amlcheck

[Install]
WantedBy=multi-user.target
```

## 4. Keeping the data fresh

A sanctions list older than 48 hours makes every check INCOMPLETE. Each check brings the TRON
index up to date by itself. Sync every 6 hours, so a failed download is retried long before then.
Save this as `/etc/systemd/system/amlcheck-sync.service`:

```ini
[Unit]
Description=amlcheck sync (OFAC list and TRON USDT index)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=amlcheck
Group=amlcheck
WorkingDirectory=/var/lib/amlcheck
ExecStart=/var/lib/amlcheck/.local/bin/amlcheck sync
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ReadWritePaths=/var/lib/amlcheck
```

and this as `/etc/systemd/system/amlcheck-sync.timer`:

```ini
[Unit]
Description=Run amlcheck sync every 6 hours

[Timer]
OnBootSec=5min
OnUnitActiveSec=6h

[Install]
WantedBy=timers.target
```

Then start both, and check the API answers:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now amlcheck-api.service amlcheck-sync.timer
systemctl status amlcheck-api.service
sudo -u amlcheck -H sh -c '. /var/lib/amlcheck/.amlcheck/.env; curl -sS -H "Authorization: Bearer $AMLCHECK_API_TOKEN" http://127.0.0.1:8766/v1/health'
```

To re-screen a watchlist every day as well, add a service and timer for `amlcheck watch run` in the
same way ([scheduling.md](scheduling.md)).

## Where the corridor runs

The corridor system must reach 127.0.0.1 of this server:

- **A process on the server:** it calls `http://127.0.0.1:8766/v1/check`.
- **A container:** 127.0.0.1 inside a container is the container itself. Run the container with
  host networking (`docker run --network host`), or install amlcheck in the container.

## Upgrading

Install the new commit or tag over the old one, then restart the API:

```bash
sudo -u amlcheck -H /var/lib/amlcheck/.local/bin/uv tool install --force "git+https://github.com/Asapsobi/aml-checker@<new commit or tag>"
sudo systemctl restart amlcheck-api.service
```

The database is migrated on first use. A migration only adds to the schema, and an older amlcheck
refuses a newer database rather than misread it.

## Backups and logs

- **Database.** Copy it with SQLite's backup, which is safe while the API runs:
  `sqlite3 /var/lib/amlcheck/.amlcheck/amlcheck.db ".backup /path/to/backup.db"`.
- **Latest hash.** Keep it too, from `amlcheck audit verify`: records cut off the end of a log can
  only be noticed against a hash kept elsewhere.
- **Logs.** amlcheck writes JSON logs to `/var/lib/amlcheck/.amlcheck/logs/`, rotated at 1 MB with 5
  kept. systemd keeps the service's own output: `journalctl -u amlcheck-api`.
