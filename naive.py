"""The comparison: append-only similarity memory.

This is how many agent memories work: every extracted fact is kept as a
sentence with an embedding, and a question retrieves the most similar
sentences by cosine similarity. Nothing is replaced or removed and results are
ordered by similarity, not time, so the model sees an early wrong guess next to
its correction with no way to tell which came last. It gets the same extractor
output and the same answer model as the Recall side.

NAIVE_K sets how many notes a question retrieves (default 10).
"""

from __future__ import annotations

import math
import os
from collections import defaultdict
from typing import Callable

Embed = Callable[[list[str]], list[list[float]]]

LABELS = {
    "suspected_cause": "suspected root cause",
    "affected_region": "affected region",
    "affected_service": "affected service",
    "mitigation_status": "mitigation status",
    "customer_impact": "customer impact",
    "incident_commander": "incident commander",
    "severity": "severity",
}


def _notes(proposal: dict) -> list[dict]:
    """One sentence per proposed statement or removal."""
    notes = []
    for s in proposal.get("statements") or []:
        label = LABELS.get(s.get("predicate"), str(s.get("predicate")).replace("_", " "))
        notes.append({"predicate": s.get("predicate"), "text": f"{label}: {s.get('value')}"})
    for r in proposal.get("removals") or []:
        label = LABELS.get(r.get("predicate"), str(r.get("predicate")).replace("_", " "))
        notes.append({"predicate": r.get("predicate"), "text": f"{label} {r.get('value')} no longer applies"})
    return notes


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


class NaiveMemory:
    def __init__(self, embed: Embed, k: int = 10):
        self.embed = embed
        self.k = k
        self._notes: dict[str, list[tuple[str, list[float]]]] = defaultdict(list)

    def add(self, subject: str, proposal: dict) -> None:
        texts = [n["text"] for n in _notes(proposal)]
        self._notes[subject].extend(zip(texts, self.embed(texts)))

    def search(self, subject: str, query: str) -> list[str]:
        return [note for note, _ in self.search_scored(subject, query)]

    def search_scored(self, subject: str, query: str) -> list[tuple[str, float]]:
        notes = self._notes.get(subject, [])
        if not notes or not query.strip():
            return []
        q = self.embed([query])[0]
        scored = sorted(((text, _cosine(q, vec)) for text, vec in notes), key=lambda x: -x[1])
        return scored[: self.k]


class Neo4jNaiveMemory:
    """The same append-only similarity memory, stored in Neo4j.

    Each extracted fact becomes a (:Note) node with an embedding, linked to its
    (:Incident), and retrieval ranks an incident's notes by cosine similarity.
    The design is what makes it naive, not the database: notes are only ever
    added, and a question gets the most similar ones regardless of which came
    last.
    """

    def __init__(self, uri: str, user: str, password: str, embed: Embed, k: int = 10):
        from neo4j import GraphDatabase

        self.embed = embed
        self.k = k
        self.driver = GraphDatabase.driver(uri, auth=(user, password))
        self.driver.verify_connectivity()
        self._backfill()

    def _backfill(self) -> None:
        """Embed notes written before retrieval used embeddings."""
        records, _, _ = self.driver.execute_query(
            "MATCH (n:Note) WHERE n.embedding IS NULL RETURN elementId(n) AS id, n.text AS text")
        for i in range(0, len(records), 100):
            batch = records[i:i + 100]
            vectors = self.embed([r["text"] for r in batch])
            self.driver.execute_query(
                """UNWIND $rows AS row
                   MATCH (n:Note) WHERE elementId(n) = row.id
                   SET n.embedding = row.embedding""",
                rows=[{"id": r["id"], "embedding": v} for r, v in zip(batch, vectors)])

    def add(self, subject: str, proposal: dict) -> None:
        notes = _notes(proposal)
        if not notes:
            return
        for note, vector in zip(notes, self.embed([n["text"] for n in notes])):
            note["embedding"] = vector
        self.driver.execute_query(
            """MERGE (i:Incident {id: $subject})
               WITH i UNWIND $notes AS note
               CREATE (i)-[:HAS_NOTE]->(:Note {subject: $subject, predicate: note.predicate,
                                               text: note.text, embedding: note.embedding,
                                               created: datetime()})""",
            subject=subject, notes=notes)

    def search(self, subject: str, query: str) -> list[str]:
        return [note for note, _ in self.search_scored(subject, query)]

    def search_scored(self, subject: str, query: str) -> list[tuple[str, float]]:
        if not query.strip():
            return []
        records, _, _ = self.driver.execute_query(
            """MATCH (n:Note {subject: $subject}) WHERE n.embedding IS NOT NULL
               WITH n, vector.similarity.cosine(n.embedding, $q) AS score
               RETURN n.text AS text, score ORDER BY score DESC LIMIT $k""",
            subject=subject, q=self.embed([query])[0], k=self.k)
        return [(r["text"], r["score"]) for r in records]

    def close(self) -> None:
        self.driver.close()


def open_naive_memory(embed: Embed):
    """Neo4j when NEO4J_URI is set, otherwise in process."""
    k = int(os.environ.get("NAIVE_K", "10"))
    uri = os.environ.get("NEO4J_URI")
    if not uri:
        return NaiveMemory(embed, k)
    return Neo4jNaiveMemory(uri, os.environ.get("NEO4J_USER", "neo4j"), os.environ["NEO4J_PASSWORD"], embed, k)
