# GitHub reuse survey

Reviewed 2026-10-17 before extending the existing Hermes deployment.

## Relevant projects and interfaces

- [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) provides the host agent, messaging integrations, scheduling, and memory options. This companion integrates through the existing SessionDB and Hermes CLI, with an explicit account allowlist. Companion-owned SQLite records provide source links, follow-up lifecycle, social decisions, and deletion invalidation.
- [ad/hermes-digital-creature](https://github.com/ad/hermes-digital-creature) is the closest lightweight companion pattern: a Hermes skill for consistent character, curated memory, user feedback, and optional check-ins. Its single-memory / native-Hermes model and Telegram-oriented use are useful for persona and feedback ideas, but do not supply the persistent event, social scoring, delivery audit, and Weixin controls required here.
- [vcostin/proactive-agent](https://github.com/vcostin/proactive-agent) demonstrates persistent local memory and scheduled follow-ups. It is a voice-first local-inference assistant, so its runtime and hardware assumptions do not fit the existing MiMo + Hermes server.
- [Zazza/svoi-oss](https://github.com/Zazza/svoi-oss) combines self-hosted memory, reminders, and a proactive scheduler. It is a separate Go/Vue/PostgreSQL product stack without a drop-in Hermes Weixin adapter, so adopting it would duplicate the current gateway and require a migration.
- Hermes's [memory provider documentation](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/memory-providers.md) lists external providers such as Mem0, OpenViking, and Honcho. These are useful future retrieval options; they do not replace the current typed event and action ledger.

## Decision

Reuse the installed Hermes CLI as the transport boundary and borrow persona/feedback patterns from the digital-creature skill. Keep memory, event timing, social policy, approvals, and delivery audit in the companion's private SQLite database. The live deployment remains in shadow mode with `outbound_delivery_enabled = false`.
