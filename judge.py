"""The judge: checks both bots' answers against the channel itself.

Ground truth is the full channel transcript in time order, not either bot's
memory. The two answers are shown as A and B in random order, so the judge
cannot tell which bot wrote which.
"""

from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

JUDGE_PROMPT = """You check answers from two incident bots against the incident channel.
The transcript is the ground truth, in time order. Later messages override earlier ones: a correction replaces an earlier claim, and a recovery removes a region or service.

Judge each answer on facts only, not on style or length:
- CURRENT: every fact in it matches the latest state of the transcript.
- STALE: it uses at least one fact that a later message corrected or retracted.
- WRONG: it states something the transcript never said, or does not answer what was asked.

First work out the latest state by reading the transcript top to bottom and applying every message in order: for each of suspected cause, severity, incident commander, mitigation and each region, the last message that mentions it decides. A region is affected only if the last message about that region says so. Then judge both answers against that state, not against any single message.

Reply in JSON: {"state": {"suspected_cause": "...", "severity": "...", "incident_commander": "...", "mitigation": "...", "regions_affected": ["..."], "regions_recovered": ["..."]}, "A": {"verdict": "CURRENT|STALE|WRONG", "reason": "..."}, "B": {"verdict": "...", "reason": "..."}}
Each reason is one short sentence. For STALE, name the out-of-date fact and the time and person of the message that replaced it."""

ICONS = {"CURRENT": ":white_check_mark:", "STALE": ":warning:", "WRONG": ":x:"}


@dataclass
class Verdict:
    recall: dict
    naive: dict
    score: dict


class Judge:
    def __init__(self, llm):
        self.llm = llm
        self.transcript: dict[str, list[tuple[datetime, str, str]]] = defaultdict(list)
        self.scores: dict[str, dict[str, list[int]]] = defaultdict(lambda: {"recall": [0, 0], "naive": [0, 0]})

    def record(self, subject: str, author: str, text: str) -> None:
        self.transcript[subject].append((datetime.now().astimezone(), author, text))

    def verdict(self, subject: str, question: str, recall_answer: str, naive_answer: str) -> Verdict:
        flip = random.random() < 0.5
        a, b = (naive_answer, recall_answer) if flip else (recall_answer, naive_answer)
        lines = "\n".join(f"[{t.strftime('%H:%M:%S')}] {who}: {text}" for t, who, text in self.transcript[subject])
        user = (f"Transcript:\n{lines}\n\nQuestion asked: {question}\n\n"
                f"Answer A:\n{a}\n\nAnswer B:\n{b}")
        out = self.llm.complete_json(JUDGE_PROMPT, user, max_tokens=700, model=self.llm.judge_model)
        first, second = out.get("A") or {}, out.get("B") or {}
        recall, naive = (second, first) if flip else (first, second)
        score = self.scores[subject]
        for name, v in (("recall", recall), ("naive", naive)):
            score[name][1] += 1
            if str(v.get("verdict", "")).upper() == "CURRENT":
                score[name][0] += 1
        return Verdict(recall, naive, {k: tuple(v) for k, v in score.items()})

    @staticmethod
    def render(v: Verdict) -> str:
        def line(name: str, d: dict) -> str:
            verdict = str(d.get("verdict", "?")).upper()
            return f"• *{name}:* {ICONS.get(verdict, ':grey_question:')} {verdict}. {d.get('reason', '')}"
        (rc, rt), (nc, nt) = v.score["recall"], v.score["naive"]
        return "\n".join([
            "*Judge* (checked both answers against the channel transcript, without knowing which bot wrote which)",
            line("recall-bot", v.recall),
            line("naive-bot", v.naive),
            f"_Score so far: recall-bot {rc}/{rt} current, naive-bot {nc}/{nt} current_",
        ])
