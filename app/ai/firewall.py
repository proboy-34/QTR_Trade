"""Hallucination firewall.

AI output is parsed into typed claims. A claim can only be treated as factual when it
cites evidence QTR itself supplied (a source-verified event or a system-derived metric).
Everything else is recorded as interpretation, hypothesis, or UNVERIFIED.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any

CLAIM_TYPES = {"FACT", "INTERPRETATION", "HYPOTHESIS", "METRIC"}
NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


@dataclass
class EvidenceItem:
    """Something QTR knows independently of the AI, offered to it as citable context."""

    ref: str
    kind: str  # SOURCE_FACT | SYSTEM_METRIC | UNVERIFIED_SOURCE
    content: Any
    value: float | None = None


@dataclass
class EvidenceRegistry:
    items: dict[str, EvidenceItem] = field(default_factory=dict)

    def add(self, ref: str, kind: str, content: Any, value: float | None = None) -> None:
        self.items[ref] = EvidenceItem(ref, kind, content, value)

    def prompt_block(self) -> str:
        return json.dumps(
            [{"ref": item.ref, "kind": item.kind, "content": item.content} for item in self.items.values()],
            default=str, indent=1,
        )


class MalformedAIResponse(ValueError):
    pass


def parse_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise MalformedAIResponse(f"AI response is not valid JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise MalformedAIResponse("AI response must be a JSON object")
    if not isinstance(payload.get("claims", []), list) or not isinstance(payload.get("proposals", []), list):
        raise MalformedAIResponse("claims and proposals must be lists")
    return payload


class ClaimVerifier:
    def __init__(self, registry: EvidenceRegistry, tolerance: float = 0.01) -> None:
        self.registry = registry
        self.tolerance = tolerance

    def verify(self, claims: list[Any]) -> tuple[list[dict[str, Any]], dict[str, int]]:
        verified: list[dict[str, Any]] = []
        stats = {"facts_verified": 0, "unverified": 0, "interpretations": 0, "hypotheses": 0,
                 "metrics_verified": 0, "invalid": 0}
        for raw in claims:
            if not isinstance(raw, dict) or not isinstance(raw.get("text"), str):
                stats["invalid"] += 1
                continue
            claim_type = str(raw.get("type", "")).upper()
            refs = [str(item) for item in raw.get("refs", []) if isinstance(item, (str, int))]
            entry: dict[str, Any] = {"text": raw["text"][:1000], "declared_type": claim_type, "refs": refs}
            if claim_type not in CLAIM_TYPES:
                entry.update(type="INTERPRETATION", status="AI_INTERPRETATION", note="unknown claim type coerced")
                stats["interpretations"] += 1
            elif claim_type == "INTERPRETATION":
                entry.update(type="INTERPRETATION", status="AI_INTERPRETATION")
                stats["interpretations"] += 1
            elif claim_type == "HYPOTHESIS":
                entry.update(type="HYPOTHESIS", status="AI_HYPOTHESIS")
                stats["hypotheses"] += 1
            elif claim_type == "FACT":
                entry.update(self._fact(refs))
                stats["facts_verified" if entry["status"] == "VERIFIED_BY_REFERENCE" else "unverified"] += 1
            else:
                entry.update(self._metric(raw["text"], refs))
                stats["metrics_verified" if entry["status"] == "SYSTEM_METRIC_MATCH" else "unverified"] += 1
            verified.append(entry)
        return verified, stats

    def _fact(self, refs: list[str]) -> dict[str, Any]:
        if not refs:
            return {"type": "FACT", "status": "UNVERIFIED", "note": "no source reference"}
        unknown = [ref for ref in refs if ref not in self.registry.items]
        if unknown:
            return {"type": "FACT", "status": "UNVERIFIED", "note": f"unknown references: {unknown}"}
        weak = [ref for ref in refs if self.registry.items[ref].kind not in {"SOURCE_FACT", "SYSTEM_METRIC"}]
        if weak:
            return {"type": "FACT", "status": "UNVERIFIED", "note": f"references are not verified sources: {weak}"}
        return {"type": "FACT", "status": "VERIFIED_BY_REFERENCE"}

    def _metric(self, text: str, refs: list[str]) -> dict[str, Any]:
        metrics = [self.registry.items[ref] for ref in refs if ref in self.registry.items
                   and self.registry.items[ref].kind == "SYSTEM_METRIC"]
        if not metrics:
            return {"type": "METRIC", "status": "UNVERIFIED", "note": "metric not supplied by QTR"}
        numbers = [float(item) for item in NUMBER.findall(text)]
        for metric in metrics:
            if metric.value is None:
                continue
            target = metric.value
            if not any(abs(number - target) <= max(abs(target) * self.tolerance, 1e-9) or
                       abs(number - target * 100) <= max(abs(target * 100) * self.tolerance, 1e-9)
                       for number in numbers):
                return {"type": "METRIC", "status": "UNVERIFIED", "note": f"value does not match {metric.ref}"}
        return {"type": "METRIC", "status": "SYSTEM_METRIC_MATCH"}
