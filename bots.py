"""@recall-bot and @naive-bot in one process, over Slack Socket Mode.

Every channel message goes through one ordered queue: it is ingested into both
memories before the next message or question is handled, so a correction can
never be processed ahead of the claim it corrects, and neither bot answers
from a memory the other has not seen yet.

    python bots.py

Mention recall-bot with "reset" to start a fresh incident in the same channel.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
from pathlib import Path

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from desk import Desk

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("incident_manager")

IGNORED_SUBTYPES = {"message_changed", "message_deleted", "channel_join", "channel_leave",
                    "channel_topic", "channel_purpose", "pinned_item"}
STATE = Path("data/state.json")
SHOW_RETRIEVAL = os.environ.get("SHOW_RETRIEVAL", "1") != "0"

recall_app = App(token=os.environ["RECALL_BOT_TOKEN"])
naive_app = App(token=os.environ["NAIVE_BOT_TOKEN"])
recall_auth = recall_app.client.auth_test()
naive_auth = naive_app.client.auth_test()
RECALL_USER, NAIVE_USER = recall_auth["user_id"], naive_auth["user_id"]
OUR_BOTS = {recall_auth["bot_id"], naive_auth["bot_id"]}

desk = Desk()
work: queue.Queue = queue.Queue()
names: dict[str, str] = {}


def load_runs() -> dict[str, int]:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


runs = load_runs()


def subject_for(channel: str) -> str:
    n = runs.get(channel, 0)
    return channel if n == 0 else f"{channel}-run{n}"


def author_of(event: dict) -> str:
    if event.get("username"):
        return event["username"]
    user = event.get("user")
    if not user:
        return "someone"
    if user not in names:
        try:
            profile = recall_app.client.users_info(user=user)["user"]["profile"]
            names[user] = profile.get("display_name") or profile.get("real_name") or user
        except Exception:
            names[user] = user
    return names[user]


def strip_mentions(text: str) -> str:
    return re.sub(r"<@[A-Z0-9]+>", "", text).strip()


def worker() -> None:
    while True:
        kind, event = work.get()
        try:
            handle(kind, event)
        except Exception:
            log.exception("failed to handle %s", kind)


def handle(kind: str, event: dict) -> None:
    channel, text = event["channel"], event.get("text") or ""
    subject = subject_for(channel)
    question = strip_mentions(text)

    if kind == "naive":
        naive_app.client.chat_postMessage(channel=channel, text=with_retrieval(*desk.naive_reply(subject, question)))
        return

    mentioned = f"<@{RECALL_USER}>" in text
    if mentioned and question.lower() in {"reset", "new incident"}:
        runs[channel] = runs.get(channel, 0) + 1
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(runs))
        recall_app.client.chat_postMessage(channel=channel, text="Started a fresh incident memory for this channel.")
        return

    author = author_of(event)
    changes = desk.ingest(subject, author, question or text,
                          {"channel": channel, "ts": event.get("ts")})
    for c in changes:
        log.info("[%s] %s", subject, c.describe())

    if mentioned:
        if question.lower().startswith(("update", "correction")) and changes:
            reply = "Got it:\n" + "\n".join(f"• {c.describe()}" for c in changes)
        else:
            reply = with_retrieval(*desk.recall_reply(subject, question))
        recall_app.client.chat_postMessage(channel=channel, text=reply)


def with_retrieval(retrieval: str | None, answer: str) -> str:
    """Show what came out of memory above the answer, when SHOW_RETRIEVAL is on."""
    if not retrieval or not SHOW_RETRIEVAL:
        return answer
    return f"*Retrieved from memory* (exactly what the model saw):\n```{retrieval}```\n{answer}"


def relevant(event: dict) -> bool:
    return (event.get("subtype") not in IGNORED_SUBTYPES
            and event.get("bot_id") not in OUR_BOTS
            and bool(event.get("text")))


@recall_app.event("message")
def recall_on_message(event):
    if relevant(event):
        work.put(("recall", event))


@naive_app.event("message")
def naive_on_message(event):
    if relevant(event) and f"<@{NAIVE_USER}>" in (event.get("text") or ""):
        work.put(("naive", event))


# Mentions also arrive as message events, which the handlers above cover.
@recall_app.event("app_mention")
def recall_on_mention():
    pass


@naive_app.event("app_mention")
def naive_on_mention():
    pass


if __name__ == "__main__":
    threading.Thread(target=worker, daemon=True).start()
    SocketModeHandler(naive_app, os.environ["NAIVE_APP_TOKEN"]).connect()
    log.info("recall-bot %s and naive-bot %s connected", RECALL_USER, NAIVE_USER)
    SocketModeHandler(recall_app, os.environ["RECALL_APP_TOKEN"]).start()
