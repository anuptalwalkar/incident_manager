"""Post the scripted incident into Slack as the responders, one step per Enter.

    python replay.py                 # channel from story.json
    python replay.py my-channel      # another channel

Needs REPLAY_BOT_TOKEN (chat:write, chat:write.customize, channels:read, channels:join) and
the two bot tokens, only to look up the bots' user ids for @mentions.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError


def channel_id(client: WebClient, name: str) -> str:
    cursor = None
    while True:
        page = client.conversations_list(types="public_channel", limit=200, cursor=cursor)
        for c in page["channels"]:
            if c["name"] == name:
                return c["id"]
        cursor = page.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            sys.exit(f"channel #{name} not found; create it and invite the three apps")


def main() -> None:
    load_dotenv()
    story = json.loads((Path(__file__).parent / "story.json").read_text())
    replay = WebClient(token=os.environ["REPLAY_BOT_TOKEN"])
    reader = WebClient(token=os.environ["RECALL_BOT_TOKEN"])
    recall_auth = reader.auth_test()
    naive_auth = WebClient(token=os.environ["NAIVE_BOT_TOKEN"]).auth_test()
    recall_user, naive_user = recall_auth["user_id"], naive_auth["user_id"]
    name = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("SLACK_CHANNEL", story["channel"])
    channel = channel_id(replay, name.lstrip("#"))
    try:
        replay.conversations_join(channel=channel)
    except SlackApiError as e:
        if e.response["error"] != "already_in_channel":
            sys.exit(f"incident-replay is not in #{name} and cannot join ({e.response['error']}); "
                     "add the channels:join scope to the app and reinstall it")

    steps = story["steps"]
    for i, step in enumerate(steps, 1):
        person = story["people"][step["who"]]
        text = step["text"].replace("{recall}", f"<@{recall_user}>").replace("{naive}", f"<@{naive_user}>")
        preview = step["text"].replace("{recall}", "@recall-bot").replace("{naive}", "@naive-bot")
        if step.get("mark") == "before_correction":
            now = datetime.now().astimezone().strftime("%-I:%M:%S %p")
            print(f"\n  >> Before the correction it is {now}. Later ask: @recall-bot what did we believe at {now}?")
        answer = input(f"\n[{i}/{len(steps)}] {person['name']}: {preview}\n  Enter to post, s to skip, q to quit: ")
        if answer.strip().lower() == "q":
            return
        if answer.strip().lower() == "s":
            continue
        posted = replay.chat_postMessage(channel=channel, text=text, username=person["name"], icon_emoji=person["icon"])
        if step.get("ask"):
            both = "{naive}" in step["text"]
            expected = {recall_auth["bot_id"]} | ({naive_auth["bot_id"]} if both else set())
            if both and os.environ.get("JUDGE", "1") != "0":
                expected.add("judge")
            wait_for_replies(reader, channel, posted["ts"], expected)


def wait_for_replies(reader: WebClient, channel: str, after: str, bots: set[str], timeout: float = 180) -> None:
    """Hold the next step until each asked bot has answered, so answers stay in order."""
    print("  waiting for the bots to answer...", end="", flush=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        history = reader.conversations_history(channel=channel, oldest=after, limit=50)["messages"]
        seen = {m.get("bot_id") for m in history} | {"judge" for m in history if m.get("username") == "Judge"}
        if bots <= seen:
            print(" done")
            return
        time.sleep(1)
    print(" timed out; check that python bots.py is running")


if __name__ == "__main__":
    main()
