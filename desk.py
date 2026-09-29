"""The incident desk: what happens to each channel message and each question.

Slack-free on purpose, so simulate.py and bots.py run the same logic.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from llm import LLM
from memory import Change, IncidentMemory
from naive import NaiveMemory

STATUS_REQUEST = ("Draft a customer-facing status page update: two or three sentences covering "
                  "impact, cause if known, and what is being done. No internal names. Plain text, no bold.")


class Desk:
    def __init__(self, data_dir: str = "data"):
        self.memory = IncidentMemory(data_dir)
        self.naive = NaiveMemory()
        self.llm = LLM()
        self.changes: dict[str, list[Change]] = {}

    # ---- every message -----------------------------------------------------

    def ingest(self, subject: str, author: str, text: str, source: dict | None = None) -> list[Change]:
        """Extract facts from one channel message and file them in both memories."""
        current = self.memory.facts(subject)
        proposal = self.llm.extract(self.memory.registry, current, author, text)
        self.naive.add(subject, proposal)
        changes = self.memory.apply(subject, author, text, proposal, source)
        self.changes.setdefault(subject, []).extend(changes)
        return changes

    # ---- questions ---------------------------------------------------------

    def recall_reply(self, subject: str, text: str) -> str:
        ask = text.strip()
        low = ask.lower()
        if "postmortem" in low or "timeline" in low:
            return self.postmortem(subject)
        when = parse_when(low)
        if when is not None:
            return self.believed_at(subject, when)
        if low in {"facts", "status", "state", "current state"}:
            return self.fact_sheet(subject)
        context = self._recall_context(subject)
        question = STATUS_REQUEST if _wants_status(low) else ask
        return self.llm.answer(context, question)

    def naive_reply(self, subject: str, text: str) -> str:
        ask = text.strip()
        query = "suspected root cause affected region service severity mitigation customer impact" \
            if _wants_status(ask.lower()) else ask
        notes = self.naive.search(subject, query)
        context = "Relevant memories:\n" + "\n".join(f"- {n}" for n in notes) if notes else "(no memories)"
        question = STATUS_REQUEST if _wants_status(ask.lower()) else ask
        return self.llm.answer(context, question)

    # ---- Recall-only views -------------------------------------------------

    def fact_sheet(self, subject: str, as_of: datetime | None = None) -> str:
        facts = self.memory.facts(subject, as_of=as_of)
        if not facts:
            return "No facts recorded for this incident yet."
        return "\n".join(f"• *{p.replace('_', ' ')}:* {', '.join(map(str, vs))}" for p, vs in facts.items())

    def believed_at(self, subject: str, when: datetime) -> str:
        header = f"*What we believed at {when.strftime('%-I:%M:%S %p')}*"
        return header + "\n" + self.fact_sheet(subject, as_of=when)

    def postmortem(self, subject: str) -> str:
        entries = self.memory.timeline(subject)
        if not entries:
            return "No facts recorded for this incident yet."
        lines = ["*Postmortem timeline* (from Recall history; every belief, including the ones we corrected)"]
        for e in entries:
            label = e.predicate.replace("_", " ")
            if e.retraction:
                what = f"{label}: ~{e.value}~ no longer applies"
            elif e.replaced is not None:
                what = f"{label}: ~{e.replaced}~ → *{e.value}*"
            else:
                what = f"{label}: *{e.value}*"
            who = e.source.get("author", "")
            quote = e.source.get("text", "")
            said = f"  _{who}: \"{_clip(quote)}\"_" if who else ""
            lines.append(f"`{e.at.strftime('%H:%M:%S')}` {what}{said}")
        return "\n".join(lines)

    def _recall_context(self, subject: str) -> str:
        sheet = self.fact_sheet(subject).replace("*", "").replace("• ", "- ")
        recent = [c for c in self.changes.get(subject, []) if c.kind in {"changed", "removed"}][-5:]
        context = "Current incident facts (latest values, corrections already applied):\n" + sheet
        if recent:
            context += "\n\nRecent corrections:\n" + "\n".join(
                f"- {c.describe()} (from {c.author})" for c in recent)
        return context

    def close(self) -> None:
        self.memory.close()


def _wants_status(low: str) -> bool:
    return "status page" in low or "status update" in low


def _clip(text: str, n: int = 90) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def parse_when(low: str, now: datetime | None = None) -> datetime | None:
    """Times in questions such as "what did we believe at 4:12pm" or "10 minutes ago"."""
    now = now or datetime.now().astimezone()
    m = re.search(r"(\d+)\s*(min|minute|minutes|mins|m)\s+ago", low)
    if m:
        return now - timedelta(minutes=int(m.group(1)))
    if "believe" not in low and "as of" not in low and " at " not in f" {low} ":
        return None
    m = re.search(r"\b(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(am|pm)?\b", low)
    if not m:
        return None
    hour, minute, second = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    if m.group(4) == "pm" and hour < 12:
        hour += 12
    if m.group(4) == "am" and hour == 12:
        hour = 0
    if m.group(4) is None and hour < 12 and now.hour >= 12 and hour + 12 <= now.hour + 1:
        hour += 12  # "at 4:12" in the afternoon means 16:12
    return now.replace(hour=hour, minute=minute, second=second, microsecond=0)
