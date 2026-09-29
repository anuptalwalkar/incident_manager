"""Post the scripted incident into Slack as the responders, one step per Enter.

    python replay.py                 # channel from story.json
    python replay.py my-channel      # another channel

Needs REPLAY_BOT_TOKEN (chat:write, chat:write.customize, channels:read) and
the two bot tokens, only to look up the bots' user ids for @mentions.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from slack_sdk import WebClient


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
    recall_user = WebClient(token=os.environ["RECALL_BOT_TOKEN"]).auth_test()["user_id"]
    naive_user = WebClient(token=os.environ["NAIVE_BOT_TOKEN"]).auth_test()["user_id"]
    name = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("SLACK_CHANNEL", story["channel"])
    channel = channel_id(replay, name.lstrip("#"))

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
        replay.chat_postMessage(channel=channel, text=text, username=person["name"], icon_emoji=person["icon"])


if __name__ == "__main__":
    main()
