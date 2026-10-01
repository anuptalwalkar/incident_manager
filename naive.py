"""The comparison: append-only similarity memory.

This is how many agent memories work: every extracted fact is kept as a
sentence with an embedding, and a question retrieves the most similar
sentences by cosine similarity. Nothing is replaced or removed and results are
ordered by similarity, not time, so the model sees an early wrong guess next to
its correction with no way to tell which came last. It gets the same extractor
output and the same answer model as the Recall side.

NAIVE_K sets how many notes a question retrieves (default 10).

NAIVE_BACKEND=mem0 swaps the store for Mem0 (open source, local). Mem0 is not
append-only: by default it reads each raw channel message and decides for
itself what to add, update or delete, so this is the comparison against a
real memory product rather than a strawman. MEM0_INFER=0 turns that off and
stores the extractor's notes as they are.
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

    def add(self, subject: str, proposal: dict, author: str = "", text: str = "") -> None:
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

    def add(self, subject: str, proposal: dict, author: str = "", text: str = "") -> None:
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


class Mem0NaiveMemory:
    """naive-bot on Mem0, running locally (Qdrant on disk under data/mem0).

    With infer on, Mem0 gets the raw channel message and runs its own
    extraction and its own add/update/delete pass; the Recall extractor's
    proposal is ignored. With infer off, the extractor's notes are stored
    verbatim and Mem0 is only the vector store. Either way a question gets the
    k most similar memories, with Mem0's own scores.
    """

    INSTRUCTIONS = ("These are messages from a production incident channel. Keep the incident's facts: "
                    "suspected root cause, severity, incident commander, mitigation status, customer impact, "
                    "affected regions and affected services. Ignore questions, requests to bots and chatter.")

    def __init__(self, k: int = 10, data_dir: str = "data/mem0", infer: bool = True):
        os.environ.setdefault("MEM0_TELEMETRY", "false")  # mem0 phones home unless told not to
        from mem0 import Memory

        self.k = k
        self.infer = infer
        os.makedirs(data_dir, exist_ok=True)
        if os.environ.get("OPENROUTER_API_KEY"):
            base = {"api_key": os.environ["OPENROUTER_API_KEY"], "openai_base_url": "https://openrouter.ai/api/v1"}
            llm, embedder = "openai/gpt-4.1-mini", "openai/text-embedding-3-small"
        else:
            base = {}
            llm, embedder = "gpt-4.1-mini", "text-embedding-3-small"
        self.memory = Memory.from_config({
            "llm": {"provider": "openai",
                    "config": {"model": os.environ.get("LLM_MODEL", llm), "temperature": 0}},
            "embedder": {"provider": "openai",
                         "config": {"model": os.environ.get("EMBED_MODEL", embedder), **base}},
            "vector_store": {"provider": "qdrant",
                             "config": {"collection_name": "naive_bot", "path": os.path.join(data_dir, "qdrant"),
                                        "on_disk": True}},
            "history_db_path": os.path.join(data_dir, "history.db"),
            "custom_instructions": self.INSTRUCTIONS,
        })

    def add(self, subject: str, proposal: dict, author: str = "", text: str = "") -> None:
        if self.infer:
            if text.strip():
                self.memory.add([{"role": "user", "content": f"{author}: {text}"}], user_id=subject)
            return
        for note in _notes(proposal):
            self.memory.add(note["text"], user_id=subject, infer=False)

    def search(self, subject: str, query: str) -> list[str]:
        return [note for note, _ in self.search_scored(subject, query)]

    def search_scored(self, subject: str, query: str) -> list[tuple[str, float]]:
        if not query.strip():
            return []
        out = self.memory.search(query, filters={"user_id": subject}, top_k=self.k, threshold=0.0)
        return [(r["memory"], float(r.get("score") or 0.0)) for r in out.get("results", [])]


def open_naive_memory(embed: Embed):
    """NAIVE_BACKEND picks the store: mem0, neo4j or memory. Unset, it is
    Neo4j when NEO4J_URI is set, otherwise in process."""
    k = int(os.environ.get("NAIVE_K", "10"))
    backend = os.environ.get("NAIVE_BACKEND", "").strip().lower()
    if backend == "mem0":
        return Mem0NaiveMemory(k, os.environ.get("MEM0_DIR", "data/mem0"), os.environ.get("MEM0_INFER", "1") != "0")
    uri = os.environ.get("NEO4J_URI")
    if backend == "memory" or not uri:
        return NaiveMemory(embed, k)
    return Neo4jNaiveMemory(uri, os.environ.get("NEO4J_USER", "neo4j"), os.environ["NEO4J_PASSWORD"], embed, k)
