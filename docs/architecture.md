# Architecture decisions

## First increment

Use a small Python modular monolith with SQLite. Keep Hermes as the chat model provider and Weixin transport. The companion owns its structured memory, event queue, social state, and audit trail in a separate private database.

The current data path is:

```text
Hermes SessionDB (read-only)
  -> Weixin allowlist filter
  -> versioned USER_MESSAGE_RECEIVED and metadata-only ASSISTANT_MESSAGE_SENT events
  -> SQLite transactional outbox and cursor
  -> validated Memory Engine extraction and persistence
  -> Social Engine scoring and auditable decision
  -> grounded draft generation and review record
  -> idempotent proactive action state machine
  -> optional allowlisted Hermes CLI delivery (manual approval in shadow mode; automatic dispatch in live mode)
```

The SessionDB poller reads only `user` and `assistant` roles from `source='weixin'` and requires the configured target to match the existing allowlist. User messages carry text for extraction; assistant events carry only the platform message ID, never reply text. Both preserve the source Unix timestamp. Event persistence and cursor advancement are atomic; replay after a crash is deduplicated by the Hermes message row ID.

## Integration boundary

Hermes does not expose an inbound Weixin callback to portable plugins in this installed version. Polling the installed SessionDB in read-only mode avoids changes to the Weixin login or adapter. A schema check fails closed if a future Hermes update changes the tables the reader needs.

The runtime composes read-only SessionDB ingestion, MiMo extraction, daily and monthly budget checks, structured memory persistence, periodic social scoring, and short grounded drafts for selected candidates. Extraction requires exactly the four top-level arrays in the JSON contract; incomplete or unexpected output uses bounded validation retry rather than silently becoming an empty result. Exact acknowledgements and greetings bypass extraction; short substantive messages still go to the model. A persistent companion state is refreshed from open topics, pending follow-up events, explicit interaction preferences, and the latest episode summary. Persona description, language, tone, and style preferences are versioned and passed to draft generation; the prompt treats supplied text as untrusted and keeps the role explicitly fictional. The optional discovery worker checks public GitHub repository metadata and configured HTTPS RSS feeds without sending remote descriptions or feed bodies to the model. Drafts and action states are stored for review. In shadow mode, a human explicitly approves an action. In live mode, selected grounded drafts can be dispatched automatically only when the separate outbound flag is enabled.

## State and privacy

- Hermes SessionDB remains authoritative for raw Hermes chat history.
- Companion SQLite is authoritative for extracted memories, follow-up events, topics, preferences, social state, and action records.
- The companion DB is stored separately with user-only filesystem access. It does not copy Telegram, cron, CLI, or other users' messages.
- Historical memory import includes only inbound messages matched to the sole allowlisted Weixin user. Assistant events are metadata-only and limited to that same target. Source timestamps are preserved, so relative dates are not reinterpreted as current.
- No third-party companion service is used. Model calls use the configured Xiaomi MiMo endpoint.
- `mode` accepts only `shadow` or `live`; live mode requires the separate outbound delivery flag.
- `pause` and `resume` persist a user control flag. Pausing stops social candidate evaluation while memory ingestion continues.
- Daily token use is recorded from provider usage, including failed JSON responses; when usage is missing, the request is charged the full estimated input and maximum output allowance.
- The monthly USD guard estimates cost at Xiaomi's published MiMo-V2.6-Flash overseas rates and prices all input as an uncached input. It blocks new model calls before the estimated cap. The provider response has usage tokens but no final account charge; Token Plan credits, domestic billing, discounts, and adjustments may differ, so reconcile against Xiaomi billing. See [official pricing](https://mimo.mi.com/docs/en-US/pricing).
- Memory operations are exposed through the local CLI (`memories`, `search`, `related`, `correct`, `forget`, `export`). Search covers active memories and associated episodes, follow-up events, and topics by keyword or time range; related-memory lookup follows shared source messages and explicit links. Corrections atomically supersede the old fact and invalidate source-derived events, topics, and drafts. Export includes structured memories, episodes, events, topics, preferences, companion state, decisions, and actions, while excluding raw inbox/outbox message payloads and API credentials.

## Delivery guarantees

The local outbox is durable and ingestion is idempotent. Schema v4 added persisted proactive action states, v5 added delivery and response timestamps, v6 tracks topic provenance, v7 adds discovery records, v8 makes explicit preference evidence idempotent, and v9 adds the bounded Thought Queue. Retry timestamps are normalized to UTC before storage because SQLite compares their ISO-8601 text lexicographically. The queue deduplicates by source key, limits active items and daily creation, records evaluation and execution attempts, and connects candidate cancellation/expiry to action state. Memory correction and deletion invalidate thoughts derived from that source. A generated draft enters `draft_ready`; shadow mode requires explicit `approve-action`, while live mode automatically queues the generated draft for dispatch. A separate delivery switch is required and live configuration is rejected unless it is enabled. The dispatcher rechecks user controls and social timing rules before sending. It passes the body on stdin, targets only the allowlisted Weixin user, records success/failure events without message text, and never retries `delivery_unknown`. A later user message within 24 hours marks the delivery as responded. After 48 hours, no reply adds one weak ignored signal; it does not permanently change preferences. Strict exactly-once delivery has not been established for the underlying Hermes/iLink interface.

Topic updates apply only when their source timestamp is at least as recent as the topic evidence already stored. This lets newer messages lower an interest score or re-enable follow-up without allowing delayed history retries to overwrite current state. Active Thought Queue candidates refresh their score and expiry from current topic evidence while preserving any later user-requested delay. The automated integration suite uses a mock extractor, draft generator, and sender to exercise inbound message persistence, event follow-up selection, reviewable draft creation, explicitly approved delivery, response feedback, and database reopen persistence. Separate tests verify metadata-only assistant events, timezone-normalized retries, and the Thought Queue lifecycle, limits, cancellation, expiry, and restart persistence. An eight-case synthetic evaluation set checks deterministic Social Engine outcomes for reasonable concern, empty greetings, repetition, interruption, topic continuation, verified discoveries, long non-response, and explicit stop. Human-review fields are retained with each sample; no natural-language model judge runs automatically. The 91 tests do not call MiMo, query GitHub, or send a real Weixin message.

## Deployment notes

The deployment-specific environment survey and acceptance audit are intentionally kept out of the source repository. Use the example configuration as a starting point, review privacy and model-cost controls for your environment, and verify `doctor`, `status`, backup, and delivery behavior before enabling live sends.
