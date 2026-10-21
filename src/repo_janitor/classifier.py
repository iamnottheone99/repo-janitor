"""Lightweight decision classifier and clone detection engine for repo hygiene."""

from __future__ import annotations

import ast
import hashlib
import json
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


@dataclass
class Finding:
    """A tri-bucket repository issue classified as an Error, Gap, or Redundancy."""

    category: str  # "error" | "gap" | "redundancy"
    kind: str  # e.g. "syntax_error", "broken_import", "stub_function", "code_clone", "dead_code"
    title: str
    file_path: str
    line: int
    confidence: float
    is_actionable: bool
    recommendation: str
    related_file: str | None = None
    related_line: int | None = None


class DecisionClassifier(Protocol):
    """Protocol for a decision model classifying code hygiene anomalies."""

    def classify_finding(
        self,
        category: str,
        kind: str,
        file_path: str,
        symbol_name: str,
        snippet: str,
    ) -> tuple[bool, float, str]:
        """Return (is_actionable, confidence, recommendation)."""
        ...


class FastDecisionClassifier:
    """Fast, local decision model using structural feature weighting and heuristics."""

    def classify_finding(
        self,
        category: str,
        kind: str,
        file_path: str,
        symbol_name: str,
        snippet: str,
    ) -> tuple[bool, float, str]:
        clean = snippet.strip()

        if kind == "syntax_error":
            return True, 0.99, "Fix syntax error to prevent parse/runtime failures."

        if kind == "broken_import":
            return True, 0.95, f"Update or remove broken import target in `{file_path}`."

        if kind == "empty_exception":
            return True, 0.90, "Replace bare or silent exception pass with logging or specific handling."

        if kind == "stub_function":
            # Differentiate abstract base class / protocol methods from neglected TODOs
            if "@abstractmethod" in clean or "Protocol" in clean or "raise NotImplementedError" in clean:
                return False, 0.60, "Intentional interface/abstract stub; no immediate fix needed."
            if "TODO" in clean or "FIXME" in clean or "pass" in clean or clean == "...":
                return True, 0.92, f"Implement body for unfinished stub `{symbol_name}`."
            return True, 0.80, f"Review empty implementation in `{symbol_name}`."

        if kind == "code_clone":
            return True, 0.88, f"Extract duplicate implementation `{symbol_name}` into a shared helper."

        if kind == "dead_code":
            if symbol_name.startswith("__") or symbol_name in ("main", "app", "cli", "handler"):
                return False, 0.50, f"`{symbol_name}` may be an entrypoint or framework hook."
            return True, 0.85, f"Delete unused `{symbol_name}` or export it in `__all__` / public API."

        if kind == "untested_function":
            return True, 0.75, f"Add unit test suite covering `{symbol_name}`."

        return True, 0.70, f"Review {kind} in `{file_path}`."


class KevDecisionClassifier:
    """Kev / System-One HTTP decision model wrapper with fallback to FastDecisionClassifier."""

    def __init__(self, endpoint: str = "http://localhost:11434/api/chat", model: str = "kev") -> None:
        self.endpoint = endpoint
        self.model = model
        self.fallback = FastDecisionClassifier()

    def classify_finding(
        self,
        category: str,
        kind: str,
        file_path: str,
        symbol_name: str,
        snippet: str,
    ) -> tuple[bool, float, str]:
        schema = {
            "is_actionable": True,
            "confidence": 0.85,
            "recommendation": "",
        }
        prompt = (
            f"Classify this repository {category} ({kind}) for symbol '{symbol_name}' in '{file_path}':\n"
            f"Snippet:\n{snippet[:400]}\n\n"
            "Respond ONLY with a JSON object matching: "
            '{"is_actionable": bool, "confidence": float (0.0-1.0), "recommendation": string}'
        )
        try:
            payload = json.dumps(
                {
                    "model": self.model,
                    "stream": False,
                    "format": "json",
                    "messages": [{"role": "user", "content": prompt}],
                }
            ).encode("utf-8")
            req = urllib.request.Request(
                self.endpoint,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                text = data.get("message", {}).get("content", "{}")
                parsed = json.loads(text)
                return (
                    bool(parsed.get("is_actionable", True)),
                    float(parsed.get("confidence", 0.85)),
                    str(parsed.get("recommendation", f"Review {kind} in {symbol_name}")),
                )
        except Exception:
            return self.fallback.classify_finding(category, kind, file_path, symbol_name, snippet)


def tokenize_code(source: str) -> list[str]:
    """Normalize code into structural tokens (keywords, operators, and ID literals) for clone detection."""
    keywords = {
        "def", "class", "return", "if", "else", "elif", "for", "while", "try", "except", "finally",
        "with", "import", "from", "as", "async", "await", "function", "export", "let", "const", "var",
        "func", "fn", "pub", "struct", "enum", "type", "interface", "package", "range", "match", "switch", "case"
    }
    no_comments = re.sub(r"#.*$|//.*$|/\*[\s\S]*?\*/|'{3}[\s\S]*?'{3}|\"{3}[\s\S]*?\"{3}", "", source, flags=re.MULTILINE)
    raw_tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*|[+\-*/%=<>!&|^~]+", no_comments)
    return [
        tok.lower()
        if tok.lower() in keywords
        else ("ID" if tok.isidentifier() else tok)
        for tok in raw_tokens
    ]


def compute_token_fingerprint(tokens: list[str]) -> set[str]:
    """Generate 2-gram shingles from token stream for fast set-based Jaccard similarity."""
    if len(tokens) < 2:
        return set(tokens)
    return {f"{tokens[i]}_{tokens[i+1]}" for i in range(len(tokens) - 1)}


def jaccard_similarity(set_a: set[str], set_b: set[str]) -> float:
    """Compute Jaccard similarity between two token fingerprint sets."""
    if not set_a or not set_b:
        return 0.0
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return intersection / union if union > 0 else 0.0
