"""Model calls: fact extraction and answers.

Uses OpenRouter when OPENROUTER_API_KEY is set, otherwise the OpenAI API
(OPENAI_API_KEY). LLM_MODEL overrides the model, EMBED_MODEL the embedder and
JUDGE_MODEL the judge's model.
"""

from __future__ import annotations

import json
import os

from openai import OpenAI

EXTRACT_PROMPT = """You maintain the fact sheet for a live production incident, from messages in the incident channel.

Allowed predicates (JSON):
{predicates}

Given the current facts and one new message, propose the fact updates the message states.
Rules:
- Use only the allowed predicates. Values are short strings unless value_type says number or boolean.
- evidence must be an exact, contiguous quote copied character for character from the message.
- For a single-valued predicate, a new value replaces the old one, so just state the new value.
- For multi-valued predicates, when the message says a current value no longer applies (recovered, ruled out, not affected), put it in removals using the exact current value.
- When a message says impact has ended or service is back to normal, also update customer_impact to say so.
- Questions, requests to bots, and chatter produce no statements.

Reply in JSON as {{"statements": [{{"predicate": "...", "value": "...", "evidence": "..."}}], "removals": [{{"predicate": "...", "value": "..."}}]}}."""

ANSWER_PROMPT = """You are an incident bot in an SRE team's incident channel.
Answer using only the context below. Be brief: at most three short lines, Slack formatting.
If the context does not answer the question, say you do not know.

Context:
{context}"""


class LLM:
    def __init__(self):
        if os.environ.get("OPENROUTER_API_KEY"):
            self.client = OpenAI(base_url="https://openrouter.ai/api/v1",
                                 api_key=os.environ["OPENROUTER_API_KEY"])
            default, embedder = "openai/gpt-4.1-mini", "openai/text-embedding-3-small"
        else:
            self.client = OpenAI()
            default, embedder = "gpt-4.1-mini", "text-embedding-3-small"
        self.model = os.environ.get("LLM_MODEL", default)
        self.embed_model = os.environ.get("EMBED_MODEL", embedder)
        # The judge reads a long transcript in order, which the small model gets wrong.
        self.judge_model = os.environ.get("JUDGE_MODEL", default.replace("gpt-4.1-mini", "gpt-4.1"))

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        out = self.client.embeddings.create(model=self.embed_model, input=texts)
        return [d.embedding for d in sorted(out.data, key=lambda d: d.index)]

    def extract(self, registry: dict, current: dict, author: str, text: str) -> dict:
        predicates = json.dumps(
            [{k: p[k] for k in ("predicate", "cardinality", "value_type", "description")} for p in registry.values()])
        facts = "\n".join(f"- {p}: {', '.join(map(str, vs))}" for p, vs in current.items()) or "(none yet)"
        out = self.client.chat.completions.create(
            model=self.model,
            temperature=0,
            max_tokens=600,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": EXTRACT_PROMPT.format(predicates=predicates)},
                {"role": "user", "content": f"Current facts:\n{facts}\n\nNew message:\n{author}: {text}"},
            ],
        ).choices[0].message.content
        try:
            proposal = json.loads(out or "{}")
        except json.JSONDecodeError:
            return {"statements": [], "removals": []}
        return {"statements": proposal.get("statements") or [], "removals": proposal.get("removals") or []}

    def complete_json(self, system: str, user: str, max_tokens: int = 500, model: str | None = None) -> dict:
        out = self.client.chat.completions.create(
            model=model or self.model,
            temperature=0,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        ).choices[0].message.content
        try:
            return json.loads(out or "{}")
        except json.JSONDecodeError:
            return {}

    def answer(self, context: str, question: str) -> str:
        return self.client.chat.completions.create(
            model=self.model,
            temperature=0,
            max_tokens=400,
            messages=[
                {"role": "system", "content": ANSWER_PROMPT.format(context=context)},
                {"role": "user", "content": question},
            ],
        ).choices[0].message.content.strip()
