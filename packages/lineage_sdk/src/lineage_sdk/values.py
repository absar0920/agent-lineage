"""Value extraction and fingerprinting (SPEC §5.2).

Every step's input and output is walked; each meaningful scalar becomes a
(path, fingerprint, display) triple. Identifier-like tokens inside free text
(e.g. an order ID the model copied into its reply) are extracted too, so a value
can be followed from a tool output into prose and back into a later tool call.
"""

import hashlib
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Literal

Direction = Literal["in", "out"]

# Identifier-like tokens found inside longer text.
ID_PATTERNS = [
    re.compile(r"\b[A-Z]{2,5}-\d{3,}\b"),                       # ORD-7781, TCK-20001
    re.compile(r"\b[A-Z]{2,5}-[A-Z0-9]{6,}\b"),                 # RF-3FA1C2D9
    re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"),
    re.compile(r"<[A-Z]+:[0-9a-f]{6,}>"),                        # redaction tokens
    re.compile(r"\b(?:ch|re|pi)_[A-Za-z0-9]{8,}\b"),             # payment-provider IDs
]
AMOUNT = re.compile(r"\$\s?(\d{1,7}(?:,\d{3})*(?:\.\d{2})?)")
# Values too common to carry lineage signal.
STOP = {"true", "false", "null", "none", "ok", "yes", "no", "usd", "open", "delivered", "offered",
        "succeeded", "refunded", "customer", "assistant", "user", "tool", "system", "function"}
MAX_TEXT_SCAN = 20_000


@dataclass(frozen=True)
class Value:
    path: str
    fingerprint: str
    display: str


def normalize(value: Any) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int | float):
        return f"{float(value):.2f}".rstrip("0").rstrip(".") if isinstance(value, float) else str(value)
    s = str(value).strip().lower()
    if re.fullmatch(r"-?\d+(\.\d+)?", s):  # "129.00" and 129 are the same value
        return f"{float(s):.2f}".rstrip("0").rstrip(".")
    return s


def fingerprint(value: Any) -> str:
    return hashlib.sha256(normalize(value).encode()).hexdigest()[:16]


def _meaningful(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, int | float):
        return abs(value) >= 10  # small counters/quantities create noise, not lineage
    s = str(value).strip()
    return len(s) >= 3 and s.lower() not in STOP


def _tokens(text: str) -> Iterator[str]:
    text = text[:MAX_TEXT_SCAN]
    seen: set[str] = set()
    for pattern in ID_PATTERNS:
        for match in pattern.findall(text):
            if match not in seen:
                seen.add(match)
                yield match
    for match in AMOUNT.findall(text):
        amount = match.replace(",", "")
        if amount not in seen:
            seen.add(amount)
            yield amount


def extract(obj: Any, path: str = "$") -> list[Value]:
    """All meaningful values in a JSON-like object, with their JSON paths."""
    out: list[Value] = []
    seen: set[tuple[str, str]] = set()

    def add(p: str, v: Any) -> None:
        fp = fingerprint(v)
        if (p, fp) not in seen:
            seen.add((p, fp))
            out.append(Value(p, fp, str(v)[:300]))

    def walk(node: Any, p: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{p}.{k}")
        elif isinstance(node, list | tuple):
            for i, v in enumerate(node):
                walk(v, f"{p}[{i}]")
        elif isinstance(node, str):
            parsed = _maybe_json(node)
            if parsed is not None:
                walk(parsed, p)
                return
            if len(node) <= 64 and "\n" not in node and _meaningful(node):
                add(p, node)
            for tok in _tokens(node):
                if normalize(tok) != normalize(node):
                    add(f"{p}~", tok)
        elif _meaningful(node):
            add(p, node)

    walk(obj, path)
    return out


def _maybe_json(s: str) -> Any:
    """Tool-call arguments arrive as JSON strings; look inside them."""
    s = s.strip()
    if len(s) < 2 or s[0] not in "{[":
        return None
    try:
        return json.loads(s)
    except ValueError:
        return None
