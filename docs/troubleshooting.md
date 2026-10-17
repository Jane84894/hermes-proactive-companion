# Troubleshooting and recovery

Run commands as the account that owns the Hermes installation. The companion stores its private database and configuration under `~/.hermes/companion`; Hermes chat history remains in Hermes SessionDB.

## Check service health

```sh
systemctl --user status hermes-companion
systemctl --user is-active hermes-gateway hermes-companion-backup.timer
PYTHONPATH=$HOME/hermes-proactive-companion/src \
  $HOME/.hermes/hermes-agent/.venv/bin/python \
  -m hermes_companion.runtime --config $HOME/.hermes/companion/config.toml doctor
PYTHONPATH=$HOME/hermes-proactive-companion/src \
  $HOME/.hermes/hermes-agent/.venv/bin/python \
  -m hermes_companion.runtime --config $HOME/.hermes/companion/config.toml status
```

`doctor` checks configuration, SessionDB readability, the allowlisted Weixin target, key presence (redacted), and the companion database/schema. `status` reports queue counts, model usage, shadow decisions, and the delivery/discovery switches. It never prints the API key or Weixin target.

For recent service errors, inspect only the companion unit:

```sh
journalctl --user -u hermes-companion --since '2 hours ago' --no-pager
```

Do not paste Hermes conversation payloads, configuration files, environment files, or credentials into a support request. The companion intentionally does not log message bodies or model output validation text.

## Queue is pending or the token budget is reached

The daily token guard defers unprocessed events until midnight in the configured timezone (`Asia/Shanghai` on the current server). The service stays active and retries when the new day starts; do not reset the event cursor or delete pending outbox rows. `status` shows `model_tokens_today`, `pending_events`, and `processed_events`.

If events remain pending after the daily reset, run `doctor`, confirm the service is active, and inspect the companion journal for sanitized error types. Check network reachability to the configured MiMo endpoint without printing or copying the key. A model timeout or malformed response may be retried with bounded backoff; the same event remains idempotent.

The separate Thought Queue stores deduplicated candidate intentions and selected shadow actions. Inspect it with `thoughts list` or `thoughts search <text>`. Use `thoughts delay <id> <ISO-8601-time>`, `thoughts complete <id>`, or `thoughts cancel <id>` to manage an item. The queue enforces `max_queue_length`, `max_tasks_per_day`, and `max_attempts` from `[thoughts]`; when a cap is reached, new candidates wait in their source event/topic/discovery records and can be reconsidered later.

## Pause and resume proactive evaluation

```sh
PYTHONPATH=$HOME/hermes-proactive-companion/src \
  $HOME/.hermes/hermes-agent/.venv/bin/python \
  -m hermes_companion.runtime --config $HOME/.hermes/companion/config.toml pause
```

Use `resume` in place of `pause` to resume shadow candidate evaluation. Pausing does not stop memory ingestion. Real Weixin delivery also requires the separate configuration flag and a manually approved action; the current deployment keeps delivery disabled.

## Correct or remove an inaccurate memory

List memories first and copy the ID of the record to change. `search <keyword>` also searches memories, episodes, follow-up events, and topics; optional `--since` / `--until` values accept ISO-8601 timestamps with timezone offsets.

```sh
PYTHONPATH=$HOME/hermes-proactive-companion/src \
  $HOME/.hermes/hermes-agent/.venv/bin/python \
  -m hermes_companion.runtime --config $HOME/.hermes/companion/config.toml memories
```

Correct a record with `correct <memory-id> <corrected text>`. The correction supersedes the old record and invalidates pending follow-up events and drafts derived from its source. Use `forget <memory-id>` when the information should be removed rather than replaced.

## Restore the companion database

The backup timer writes a consistent SQLite snapshot to `$HOME/.hermes/companion/backups` and keeps the newest 14 snapshots. First choose the desired snapshot by filename. Then stop the companion, preserve the current database, restore the snapshot, and verify before relying on it:

```sh
systemctl --user stop hermes-companion
cp -a $HOME/.hermes/companion/data/companion.sqlite3 \
  $HOME/.hermes/companion/data/companion.sqlite3.before-restore
cp -a $HOME/.hermes/companion/backups/<chosen-snapshot>.sqlite3 \
  $HOME/.hermes/companion/data/companion.sqlite3
chmod 0600 $HOME/.hermes/companion/data/companion.sqlite3
systemctl --user start hermes-companion
```

Run `doctor` and `status` after restart. Keep the `.before-restore` copy until the restored database is confirmed. This procedure changes only the companion database; it does not modify Hermes SessionDB or Weixin login state.

## Roll back companion source

Stop the companion, preserve the current source directory, then restore one of the dated `hermes-proactive-companion.pre-*` snapshots under your home directory. Start the service and run `doctor`, `status`, and the test suite from the restored tree. Do not roll back or replace the Hermes checkout to revert companion code. Keep the original source snapshot until the restored service passes its health checks.
