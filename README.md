# incident_manager

Incident memory for SRE channels. During an outage the suspected cause,
affected regions and mitigation status change every few minutes. A bot that
remembers by similarity search keeps retrieving the early wrong guess next to
the correction, so it tells the VP "database issue" and drafts a status page
with the wrong cause. Afterwards, the postmortem timeline is rebuilt by hand
from scrollback.

`@recall-bot` keeps the incident's facts current as responders correct each
other, and writes the postmortem timeline from what the channel believed at
each moment. `@naive-bot` sits next to it in the same channel with
append-only similarity memory, the same extractor and the same model, so you
can see the difference on the same messages.

Built at The AI Conference Hack Day, September 29, 2026, on
[Polign Recall](https://github.com/Polign/recall).

## How it works

- Every message in the channel goes through one ordered queue. A model call
  proposes typed facts from it (`predicates.json`: suspected cause, severity,
  incident commander, mitigation status, customer impact, affected regions and
  services), each with an exact quote as evidence.
- Recall files them. A fact that allows one value (suspected cause) is
  replaced when someone corrects it; a fact that allows a set (regions) drops a
  value when someone says it recovered. Nothing is deleted.
- `@recall-bot <question>` answers from current facts plus recent corrections.
- `@recall-bot postmortem` posts every belief in order: time, what changed,
  who said it, in their words.
- `@recall-bot what did we believe at 4:12pm` answers as of that moment.
- `@recall-bot draft a status page update` writes one from current facts.
- `@recall-bot reset` starts a fresh incident in the same channel.

Recall stores what the extractor proposes; it does not check that a root
cause is right. What it adds is explicit correction rules and queryable
history.

## Run it

Python 3.10+.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in the tokens and a model key
```

Without Slack, the whole story runs in the terminal:

```bash
python simulate.py
```

In Slack:

1. Create three apps from the manifests in `slack/` (api.slack.com/apps,
   Create New App, From a manifest). Install each and copy its bot token
   (`xoxb-`). For recall-bot and naive-bot, also create an app-level token
   with the `connections:write` scope (`xapp-`).
2. Create `#inc-checkout-latency` and invite all three apps.
3. Start the bots, then play the incident:

```bash
python bots.py                 # terminal 1
python replay.py               # terminal 2, Enter posts the next message
```

Anyone in the channel can post updates or ask the bots questions; live
messages take the same path as the replay.

## naive-bot on Mem0

By default naive-bot keeps its notes in process (or in Neo4j when `NEO4J_URI`
is set). To run it on [Mem0](https://github.com/mem0ai/mem0) instead, set
`NAIVE_BACKEND=mem0` in `.env`. Mem0 runs locally and stores under
`data/mem0`. It reads each raw channel message and decides for itself what to
add, update or delete, so this is naive-bot with a real memory product behind
it, not append-only notes. `MEM0_INFER=0` turns that off and stores the
extractor's notes as they are.
