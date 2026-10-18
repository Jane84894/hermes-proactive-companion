# Hermes Proactive Companion

A self-hosted companion service that adds context-aware proactive conversation to an existing [Hermes Agent](https://github.com/NousResearch/hermes-agent) installation. Hermes remains responsible for model access and messaging transport; this project adds structured memory, follow-up candidates, social timing rules, and auditable delivery controls.

## Features

- Read-only ingestion from the configured Hermes SessionDB and allowlisted messaging account.
- Private SQLite storage for extracted memories, topics, follow-up events, preferences, and action state.
- MiMo-compatible structured extraction and short, grounded draft generation.
- Social gates for quiet hours, daily caps, cooldowns, busy/pause controls, and ignored messages.
- Shadow mode for reviewing drafts before sending, with optional live delivery through an allowlisted Hermes CLI target.
- Idempotent ingestion and dispatch records, terminal handling for ambiguous delivery, and private database backups.
- Optional GitHub and HTTPS RSS discovery for configured interests.

## Safety defaults

The example configuration uses `mode = "shadow"` and `outbound_delivery_enabled = false`. Keep these defaults while checking the imported data and generated drafts. Live delivery requires both `mode = "live"` and `outbound_delivery_enabled = true`; it sends only to the account allowlisted by the Hermes environment. Configure real credentials outside this repository. Never commit `.env`, `config.toml`, `persona.toml`, or SQLite data files.

## Quick start

1. Install and configure Hermes Agent, including its messaging transport and allowlisted target.
2. Install this package in the Python environment used by Hermes:

   ```sh
   python3 -m pip install -e .
   ```

3. Create private runtime configuration files:

   ```sh
   mkdir -p ~/.hermes/companion/data
   cp config.example.toml ~/.hermes/companion/config.toml
   cp persona.example.toml ~/.hermes/companion/persona.toml
   chmod 700 ~/.hermes/companion ~/.hermes/companion/data
   chmod 600 ~/.hermes/companion/config.toml ~/.hermes/companion/persona.toml
   ```

4. Review the paths in `~/.hermes/companion/config.toml`. By default, the service reads `~/.hermes/state.db`, `~/.hermes/.env`, and Hermes' installed CLI. The `.env` file must contain the model provider credentials expected by your Hermes setup.
   For a long companion style prompt, set `[persona].instructions` in `persona.toml` to a TOML multiline string (up to 20,000 characters). It is injected as trusted local style guidance while the companion's truthfulness and grounding rules remain in force.
5. Check the configuration and database, then run the test suite:

   ```sh
   hermes-companion --config ~/.hermes/companion/config.toml doctor
   PYTHONPATH=src python3 -m unittest discover -s tests -v
   ```

6. Start with shadow mode and review generated drafts with `hermes-companion ... reviews`. Use the systemd units in `deploy/` if you want a persistent service and scheduled backups.

## Proactive frequency

The example policy allows up to eight messages per day, requires at least 45 minutes between proactive messages, applies a 30-minute cooldown after user activity, and allows a light-chat fallback after six hours without user activity when no open event or topic candidate exists. Quiet hours default to 23:30–08:30 in `Asia/Shanghai`. These are limits and eligibility rules, not a fixed sending schedule; candidate quality, user controls, and delivery checks still apply.

The companion also accepts user controls through the configured chat: `少找我聊天`, `先别主动联系我`, `恢复主动联系`, and `我最近很忙`. See the source and operations guide for details.

## Operations

```sh
systemctl --user status hermes-companion
hermes-companion --config ~/.hermes/companion/config.toml status
hermes-companion --config ~/.hermes/companion/config.toml pause
hermes-companion --config ~/.hermes/companion/config.toml resume
```

Use `memories`, `search`, `related`, `correct`, `forget`, and `export` to manage structured memory. Use `thoughts list`, `thoughts search`, `thoughts delay`, `thoughts complete`, and `thoughts cancel` to manage follow-up intentions. `export` excludes raw inbox/outbox payloads and credentials. Operational diagnosis and database recovery steps are in [`docs/troubleshooting.md`](docs/troubleshooting.md).

## Development

The storage layer and tests use the Python standard library. Model calls use the OpenAI-compatible SDK (`openai>=2.24,<3`).

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

The synthetic quality cases in `eval/companion-quality-v1.json` exercise deterministic candidate selection and suppression. Passing those fixtures does not establish natural-language draft quality; use human review before enabling live delivery.
