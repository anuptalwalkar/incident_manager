"""Incident memory on Polign Recall.

Each incident is one Recall subject. Facts are typed by predicates.json, so a
correction to a single-valued fact replaces the old value, and nothing is
deleted: history and as_of reads still see every earlier belief.

Recall events do not carry the message that produced them, so this module
keeps a small sidecar file mapping event id to author, text and Slack
coordinates. The postmortem uses it to say who said what.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from polign_recall import Client, RecallError

PREDICATES = Path(__file__).resolve().parent / "predicates.json"


@dataclass
class Change:
    """One effect of a message on the incident's facts."""

    predicate: str
    kind: str  # "set", "changed" or "removed"
    value: Any
    old: Any = None
    author: str = ""

    def describe(self) -> str:
        label = self.predicate.replace("_", " ")
        if self.kind == "changed":
            return f"{label}: {self.old} -> {self.value}"
        if self.kind == "removed":
            return f"{label}: {self.value} removed"
        return f"{label}: {self.value}"


@dataclass
class TimelineEntry:
    at: datetime
    predicate: str
    value: Any
    retraction: bool
    replaced: Any
    source: dict


class IncidentMemory:
    def __init__(self, data_dir: str | Path = "data"):
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.client = Client(local_dir=str(self.data_dir / "recall"),
                             env={"POLIGN_PREDICATES": str(PREDICATES)})
        # "note" is added by Recall itself; incidents only use the typed facts.
        self.registry = {p["predicate"]: p for p in self.client.predicates()
                         if p["predicate"] != "note"}
        self._sources_path = self.data_dir / "sources.jsonl"
        self._sources = self._load_sources()
        self._lock = threading.Lock()

    # ---- reads -------------------------------------------------------------

    def beliefs(self, subject: str, as_of: datetime | None = None) -> list:
        """The raw Recall rows believed now (or as_of), incident predicates only."""
        return [b for b in self.client.recall(subject, as_of=as_of, limit=100)
                if b.predicate in self.registry]

    def facts(self, subject: str, as_of: datetime | None = None) -> dict[str, list[Any]]:
        """Current (or as_of) facts, grouped by predicate."""
        grouped: dict[str, list[Any]] = {}
        for belief in self.beliefs(subject, as_of):
            grouped.setdefault(belief.predicate, []).append(belief.value)
        return grouped

    def timeline(self, subject: str) -> list[TimelineEntry]:
        """Every fact event for the incident, oldest first, with its source."""
        entries: list[TimelineEntry] = []
        for predicate, spec in self.registry.items():
            previous = None
            for event in self.client.history(subject, predicate):
                replaced = previous if spec["cardinality"] == "single" and not event.retraction else None
                entries.append(TimelineEntry(
                    at=parse_time(event.observed_at),
                    predicate=predicate,
                    value=event.value,
                    retraction=event.retraction,
                    replaced=replaced,
                    source=self._sources.get(event.id, {}),
                ))
                if not event.retraction:
                    previous = event.value
        entries.sort(key=lambda e: e.at)
        return entries

    # ---- writes ------------------------------------------------------------

    def apply(self, subject: str, author: str, text: str, proposal: dict,
              source: dict | None = None) -> list[Change]:
        """File the model's proposed statements and removals for one message."""
        full_text = f"{author}: {text}"
        source = {"author": author, "text": text, **(source or {})}
        with self._lock:
            current = self.facts(subject)
            statements = self._valid_statements(subject, full_text, proposal.get("statements") or [])
            changes = self._remember(subject, full_text, statements, source, author)
            changes += self._forget(subject, current, proposal.get("removals") or [], source, author)
        return changes

    def _valid_statements(self, subject: str, full_text: str, proposed: list) -> list[dict]:
        valid = []
        for s in proposed:
            predicate = s.get("predicate")
            evidence = s.get("evidence") or ""
            spec = self.registry.get(predicate)
            if spec is None or not evidence or evidence not in full_text:
                continue
            value = _coerce(s.get("value"), spec["value_type"])
            if value is None:
                continue
            valid.append({"subject": subject, "predicate": predicate, "value": value, "evidence": evidence})
        return valid

    def _remember(self, subject, full_text, statements, source, author) -> list[Change]:
        if not statements:
            return []
        try:
            results = self.client.remember(text=full_text, statements=statements).results
        except RecallError:
            # One bad proposal fails the batch; file the rest one by one.
            results = []
            for s in statements:
                try:
                    results += self.client.remember(text=full_text, statements=[s]).results
                except RecallError:
                    pass
        changes = []
        for r in results:
            if r.already_known or r.stored is None:
                continue
            self._record_source(r.stored.event_id, source)
            old = [b.value for b in r.superseded]
            if old:
                changes.append(Change(r.stored.predicate, "changed", r.stored.value, ", ".join(map(str, old)), author))
            else:
                changes.append(Change(r.stored.predicate, "set", r.stored.value, author=author))
        return changes

    def _forget(self, subject, current, removals, source, author) -> list[Change]:
        changes = []
        for r in removals:
            predicate = r.get("predicate")
            spec = self.registry.get(predicate)
            if spec is None or spec["cardinality"] != "multi":
                continue
            wanted = str(r.get("value", "")).strip().lower()
            match = next((v for v in current.get(predicate, []) if str(v).lower() == wanted), None)
            if match is None:
                continue
            if self.client.forget(subject, predicate, match):
                retraction = next((e for e in reversed(self.client.history(subject, predicate))
                                   if e.retraction and str(e.value).lower() == wanted), None)
                if retraction is not None:
                    self._record_source(retraction.id, source)
                changes.append(Change(predicate, "removed", match, author=author))
        return changes

    # ---- sidecar -----------------------------------------------------------

    def _load_sources(self) -> dict[str, dict]:
        sources = {}
        if self._sources_path.exists():
            for line in self._sources_path.read_text().splitlines():
                if line.strip():
                    row = json.loads(line)
                    sources[row.pop("event_id")] = row
        return sources

    def _record_source(self, event_id: str, source: dict) -> None:
        self._sources[event_id] = source
        with self._sources_path.open("a") as f:
            f.write(json.dumps({"event_id": event_id, **source}) + "\n")

    def close(self) -> None:
        self.client.close()


def _coerce(value: Any, value_type: str) -> Any:
    if value is None:
        return None
    if value_type == "number":
        try:
            return float(value) if "." in str(value) else int(value)
        except ValueError:
            return None
    if value_type == "boolean":
        return value if isinstance(value, bool) else str(value).lower() in {"true", "yes"}
    text = str(value).strip()
    return text or None


def parse_time(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone()
