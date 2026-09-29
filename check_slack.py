"""Check the Slack setup in .env without printing any token.

    python check_slack.py
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

BOTS = [("recall-bot", "RECALL_BOT_TOKEN", "RECALL_APP_TOKEN"),
        ("naive-bot", "NAIVE_BOT_TOKEN", "NAIVE_APP_TOKEN"),
        ("incident-replay", "REPLAY_BOT_TOKEN", None)]


def main() -> int:
    load_dotenv()
    channel = os.environ.get("SLACK_CHANNEL", "inc-checkout-latency").lstrip("#")
    ok = True
    for label, bot_var, app_var in BOTS:
        token = os.environ.get(bot_var, "")
        if not token.startswith("xoxb-"):
            print(f"x {label}: {bot_var} is {'missing' if not token else 'not a bot token (want xoxb-)'}")
            ok = False
            continue
        client = WebClient(token=token)
        try:
            auth = client.auth_test()
        except SlackApiError as e:
            print(f"x {label}: {bot_var} rejected by Slack ({e.response['error']})")
            ok = False
            continue
        where = f"{auth['user']} in {auth['team']}"
        member = None
        try:
            cursor = None
            while member is None:
                page = client.conversations_list(types="public_channel", limit=200, cursor=cursor)
                member = next((c.get("is_member", False) for c in page["channels"] if c["name"] == channel), None)
                cursor = page.get("response_metadata", {}).get("next_cursor")
                if not cursor:
                    break
        except SlackApiError as e:
            print(f"x {label}: cannot list channels ({e.response['error']})")
            ok = False
            continue
        if member is None:
            print(f"x {label} ({where}): channel #{channel} not found")
            ok = False
        elif not member:
            print(f"x {label} ({where}): not in #{channel}; run /invite @{label} there")
            ok = False
        else:
            print(f"ok {label} ({where}) is in #{channel}")
        if app_var:
            app_token = os.environ.get(app_var, "")
            if not app_token.startswith("xapp-"):
                print(f"x {label}: {app_var} is {'missing' if not app_token else 'not an app-level token (want xapp-)'}")
                ok = False
            else:
                try:
                    WebClient().apps_connections_open(app_token=app_token)
                    print(f"ok {label}: Socket Mode token works")
                except SlackApiError as e:
                    print(f"x {label}: {app_var} rejected ({e.response['error']}); it needs the connections:write scope")
                    ok = False
    if not (os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY")):
        print("x no model key: set OPENROUTER_API_KEY or OPENAI_API_KEY")
        ok = False
    print("\nAll set: run python bots.py, then python replay.py" if ok else "\nFix the lines marked x and rerun.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
