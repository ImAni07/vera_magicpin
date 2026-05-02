# Vera Deterministic Composer

This entry implements Vera as a deterministic rules-first message engine. It does not call an external LLM at runtime; the goal is fast, repeatable, grounded composition from the four challenge contexts.

## Run

```bash
python bot.py
```

The bot listens on:

```text
http://127.0.0.1:8080
```

It exposes the required endpoints:

- `GET /v1/healthz`
- `GET /v1/metadata`
- `POST /v1/context`
- `POST /v1/tick`
- `POST /v1/reply`

It also supports optional `POST /v1/teardown` to wipe in-memory state.

## Approach

The core function is:

```python
compose(category, merchant, trigger, customer=None)
```

The composer routes by `trigger.kind`, then picks one strongest fact from the trigger, one merchant-specific anchor, and one category-appropriate next action. Customer-scoped triggers send as `merchant_on_behalf` only when consent is present. Merchant-scoped triggers send as `vera`.

Key behaviors:

- Uses real context facts: metrics, active offers, digest sources, dates, slots, batches, review themes, and customer relationship data.
- Suppresses repeat sends by `suppression_key`.
- Handles idempotent context updates by `(scope, context_id, version)`.
- Detects WhatsApp Business auto-replies, hard opt-outs, intent transitions, off-topic questions, and simple price questions.
- Avoids fabricated locality facts, fake offers, fake citations, and external API calls.

## Local Smoke Test

Start the bot in one terminal:

```bash
python bot.py
```

Then, in another terminal, run the official simulator after adding your LLM key inside `judge_simulator.py`:

```bash
python judge_simulator.py
```

For deployment, expose the same server publicly and submit the base URL, for example:

```text
https://your-domain.example
```

The judge will call `/v1/context`, `/v1/tick`, `/v1/reply`, `/v1/healthz`, and `/v1/metadata` under that base URL.

## Tradeoffs

This entry favors reliability and determinism over LLM creativity. It should score well on operational stability, grounding, replay behavior, and canonical trigger families. A production version could add a temperature-zero LLM as a final copy editor, but only after validating that every claim remains present in the pushed context.
