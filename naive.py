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
        return [note for note, _ in self.search_scored(subject, query)]

    def search_scored(self, subject: str, query: str) -> list[tuple[str, float]]:
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
        return [(notes[i], score) for score, i in scored[: self.k]]


class Neo4jNaiveMemory:
    """The same append-only similarity memory, stored in Neo4j.

    Each extracted fact becomes a (:Note) node linked to its (:Incident), and
    retrieval uses Neo4j's full-text index (Lucene scoring). The design is what
    makes it naive, not the database: notes are only ever added, and a question
    gets the most similar ones regardless of which came last.
    """

    def __init__(self, uri: str, user: str, password: str, k: int = 5):
        from neo4j import GraphDatabase

        self.k = k
        self.driver = GraphDatabase.driver(uri, auth=(user, password))
        self.driver.verify_connectivity()
        self.driver.execute_query(
            "CREATE FULLTEXT INDEX note_text IF NOT EXISTS FOR (n:Note) ON EACH [n.text]")
        self.driver.execute_query("CALL db.awaitIndexes(30)")

    def add(self, subject: str, proposal: dict) -> None:
        notes = []
        for s in proposal.get("statements") or []:
            label = LABELS.get(s.get("predicate"), str(s.get("predicate")).replace("_", " "))
            notes.append({"predicate": s.get("predicate"), "text": f"{label}: {s.get('value')}"})
        for r in proposal.get("removals") or []:
            label = LABELS.get(r.get("predicate"), str(r.get("predicate")).replace("_", " "))
            notes.append({"predicate": r.get("predicate"), "text": f"{label} {r.get('value')} no longer applies"})
        if notes:
            self.driver.execute_query(
                """MERGE (i:Incident {id: $subject})
                   WITH i UNWIND $notes AS note
                   CREATE (i)-[:HAS_NOTE]->(:Note {subject: $subject, predicate: note.predicate,
                                                   text: note.text, created: datetime()})""",
                subject=subject, notes=notes)

    def search(self, subject: str, query: str) -> list[str]:
        return [note for note, _ in self.search_scored(subject, query)]

    def search_scored(self, subject: str, query: str) -> list[tuple[str, float]]:
        terms = sorted(_tokens(query))
        if not terms:
            return []
        lucene = " OR ".join('"' + t.replace('"', "") + '"' for t in terms)
        records, _, _ = self.driver.execute_query(
            """CALL db.index.fulltext.queryNodes('note_text', $q) YIELD node, score
               WHERE node.subject = $subject
               RETURN node.text AS text, score ORDER BY score DESC LIMIT $k""",
            q=lucene, subject=subject, k=self.k)
        return [(r["text"], r["score"]) for r in records]

    def close(self) -> None:
        self.driver.close()


def open_naive_memory():
    """Neo4j when NEO4J_URI is set, otherwise in process."""
    import os

    uri = os.environ.get("NEO4J_URI")
    if not uri:
        return NaiveMemory()
    return Neo4jNaiveMemory(uri, os.environ.get("NEO4J_USER", "neo4j"), os.environ["NEO4J_PASSWORD"])
