"""Run the incident story through both memories in the terminal, no Slack.

    python simulate.py            # fresh incident each run
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from desk import Desk


def main() -> None:
    load_dotenv()
    story = json.loads((Path(__file__).parent / "story.json").read_text())
    subject = f"sim-{int(time.time())}"
    desk = Desk()
    marks: dict[str, datetime] = {}
    try:
        for step in story["steps"]:
            person = story["people"][step["who"]]["name"]
            if step.get("mark"):
                time.sleep(1.1)
                marks[step["mark"]] = datetime.now().astimezone()
                time.sleep(1.1)
            text = step["text"].replace("{recall}", "@recall-bot").replace("{naive}", "@naive-bot")
            print(f"\n\033[1m{person}:\033[0m {text}")
            if step.get("ask"):
                question = text.replace("@recall-bot", "").replace("@naive-bot", "").strip()
                retrieval, reply = desk.recall_reply(subject, question)
                _show("recall-bot", "32", retrieval, reply)
                if "{naive}" in step["text"]:
                    _show("naive-bot ", "33", *desk.naive_reply(subject, question))
                continue
            for change in desk.ingest(subject, person, text):
                print(f"  \033[2m[recall] {change.describe()}\033[0m")
        if "before_correction" in marks:
            when = marks["before_correction"]
            print(f"\n\033[1mYou:\033[0m @recall-bot what did we believe at {when.strftime('%H:%M:%S')}?")
            print(f"  \033[32mrecall-bot:\033[0m {_indent(desk.believed_at(subject, when))}")
    finally:
        desk.close()


def _show(name: str, color: str, retrieval: str | None, reply: str) -> None:
    if retrieval:
        print(f"  \033[{color}m{name}\033[0m \033[2mretrieved:\n              {_indent(retrieval)}\033[0m")
    print(f"  \033[{color}m{name}:\033[0m {_indent(reply)}")


def _indent(text: str) -> str:
    return text.replace("\n", "\n              ")


if __name__ == "__main__":
    main()
