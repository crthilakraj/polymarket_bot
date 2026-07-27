# systemd deployment

Two services, matching the two independent background loops already
validated in dry-run (see `../../HANDOVER.md` for what they do and why):

- `polymarket-bot-loop.service` — runs `scripts/run_live_games_loop.sh`
  (refreshes the live-game watchlist and restarts `main.py` every 15 min so
  it keeps picking up newly-live games as old ones end)
- `polymarket-metadata-refresh.service` — runs
  `scripts/refresh_all_metadata.py` (re-fetches Gamma metadata for every
  market ever collected, every 5 min, so resolutions are detected even after
  a market rotates out of the live watchlist)

Both are independent; either can be started/stopped/restarted without
affecting the other.

## Install

```bash
# 1. Create a dedicated, non-root user (recommended - this process holds
#    trading credentials via .env even in dry-run mode)
sudo useradd -r -m -d /opt/polymarket-bot -s /usr/sbin/nologin polymarket

# 2. Deploy the code
sudo git clone <your-repo-url> /opt/polymarket-bot
sudo chown -R polymarket:polymarket /opt/polymarket-bot

# 3. Install uv and the project's dependencies as the polymarket user
sudo -u polymarket bash -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
sudo -u polymarket bash -c 'cd /opt/polymarket-bot && ~/.local/bin/uv sync'

# 4. Create .env (copy .env.example and fill in real values)
sudo -u polymarket cp /opt/polymarket-bot/.env.example /opt/polymarket-bot/.env
sudo -u polymarket nano /opt/polymarket-bot/.env
# Leave DRY_RUN=true and LIVE_TRADING_CONFIRMED=false unless you have
# explicitly decided to go live - see config.Settings.require_live_trading_confirmation()

# 5. Verify uv's location matches what the unit files expect
which uv   # as the polymarket user - if not on PATH by default for services,
           # either symlink it into /usr/local/bin or edit ExecStart in
           # polymarket-metadata-refresh.service to the absolute path

# 6. Install the unit files
sudo cp deploy/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload

# 7. Enable + start
sudo systemctl enable --now polymarket-bot-loop.service
sudo systemctl enable --now polymarket-metadata-refresh.service
```

## Operate

```bash
# Status / logs (journald replaces the nohup .log files used in dev)
sudo systemctl status polymarket-bot-loop.service
sudo journalctl -u polymarket-bot-loop.service -f
sudo journalctl -u polymarket-metadata-refresh.service -f

# Stop everything cleanly
sudo systemctl stop polymarket-bot-loop.service polymarket-metadata-refresh.service

# Restart (e.g. after a code update)
cd /opt/polymarket-bot && sudo -u polymarket git pull
sudo systemctl restart polymarket-bot-loop.service polymarket-metadata-refresh.service
```

## Notes

- `Restart=always` means both services come back automatically after a
  crash *or* a VPS reboot (since they're `enable`d, not just `start`ed) -
  this is the main advantage over the `nohup`-based setup used during
  development.
- Neither service places real orders regardless of restarts - that's
  gated entirely by `DRY_RUN`/`LIVE_TRADING_CONFIRMED` in `.env`, which
  `EnvironmentFile=` loads the same way for both.
- The database (`polymarket_data.db*`) lives in `WorkingDirectory` and
  grows continuously (~1-2GB/hour observed under active collection) - watch
  disk space, and see `HANDOVER.md` for the open pruning task.
- Adjust `WorkingDirectory`, `User`, and the `uv` path in `ExecStart` if
  your deployment doesn't match the `/opt/polymarket-bot` / `polymarket`
  user convention used above.
