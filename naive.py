"""The comparison: append-only similarity memory.

This is how many agent memories work: every extracted fact is kept as a
sentence, and a question retrieves the most similar sentences. Nothing is
replaced or removed and results are ordered by similarity, not time, so the
model sees an early wrong guess next to its correction with no way to tell
which came last. It gets the same extractor output and the same answer model
as the Recall side.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict

LABELS = {
    "suspected_cause": "suspected root cause",
    "affected_region": "affected region",
    "affected_service": "affected service",
    "mitigation_status": "mitigation status",
    "customer_impact": "customer impact",
    "incident_commander": "incident commander",
    "severity": "severity",
}


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9][a-z0-9.-]*", text.lower())
    return {w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words}


class NaiveMemory:
    def __init__(self, k: int = 5):
        self.k = k
        self._notes: dict[str, list[str]] = defaultdict(list)

    def add(self, subject: str, proposal: dict) -> None:
        for s in proposal.get("statements") or []:
            label = LABELS.get(s.get("predicate"), str(s.get("predicate")).replace("_", " "))
            self._notes[subject].append(f"{label}: {s.get('value')}")
        for r in proposal.get("removals") or []:
            label = LABELS.get(r.get("predicate"), str(r.get("predicate")).replace("_", " "))
            self._notes[subject].append(f"{label} {r.get('value')} no longer applies")

    def search(self, subject: str, query: str) -> list[str]:
        notes = self._notes.get(subject, [])
        if not notes:
            return []
        docs = [_tokens(n) for n in notes]
        df: dict[str, int] = defaultdict(int)
        for d in docs:
            for t in d:
                df[t] += 1
        q = _tokens(query)
        scored = []
        for i, d in enumerate(docs):
            score = sum(math.log(1 + len(docs) / df[t]) for t in q & d)
            if score > 0:
                scored.append((score, i))
        scored.sort(key=lambda x: -x[0])
        return [notes[i] for _, i in scored[: self.k]]
