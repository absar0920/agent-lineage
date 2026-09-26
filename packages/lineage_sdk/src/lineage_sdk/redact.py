"""PII redaction with stable tokens (SPEC §5.3).

Runs inside the instrumented process, before anything is queued for export.
The same input value always yields the same token (HMAC with a per-install key),
so lineage still links across redacted values without storing the value itself.
"""

import hashlib
import hmac
import re
from collections import OrderedDict
from collections.abc import Callable, Iterable
from typing import Any

EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE = re.compile(r"(?<![\w-])\+?\d{1,3}[\s.-]?\(?\d{2,4}\)?[\s.-]?\d{3,4}[\s.-]?\d{3,4}(?![\w-])")
CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")

# Field names whose values are always PII. Deliberately excludes plain "name",
# which usually holds business data (product names, tool names).
DEFAULT_FIELDS = frozenset(
    {
        "email",
        "phone",
        "address",
        "customer_name",
        "full_name",
        "first_name",
        "last_name",
        "street",
        "postal_code",
        "zip",
    }
)
MAX_LEARNED = 10_000


def _luhn(digits: str) -> bool:
    nums = [int(d) for d in digits][::-1]
    total = sum(nums[0::2]) + sum(sum(divmod(2 * d, 10)) for d in nums[1::2])
    return total % 10 == 0


class Redactor:
    def __init__(self, key: str | bytes, *, fields: Iterable[str] = DEFAULT_FIELDS) -> None:
        if len(key) < 16:
            raise ValueError("redaction key must be at least 16 characters")
        self._key = key.encode() if isinstance(key, str) else key
        self._fields = {f.lower() for f in fields}
        # Values redacted by field name, so the same value inside free text (e.g. the
        # model greeting the customer by name) gets the same token.
        self._learned: OrderedDict[str, str] = OrderedDict()

    def token(self, kind: str, value: str) -> str:
        digest = hmac.new(self._key, value.strip().lower().encode(), hashlib.sha256).hexdigest()[:10]
        return f"<{kind}:{digest}>"

    def _learn(self, value: str, token: str) -> None:
        value = value.strip()
        if len(value) < 3:
            return
        self._learned[value] = token
        self._learned.move_to_end(value)
        while len(self._learned) > MAX_LEARNED:
            self._learned.popitem(last=False)

    def _replace_learned(self, text: str) -> str:
        lowered = text.lower()
        for value, token in reversed(self._learned.items()):
            if value.lower() in lowered:
                text = re.sub(rf"(?i)(?<!\w){re.escape(value)}(?!\w)", token, text)
                lowered = text.lower()
            # First name of a learned full name ("Hi Sara,"): exact capitalised match only,
            # so a customer called "Will" never redacts the word "will".
            first = value.split()[0] if " " in value else ""
            if len(first) >= 3 and first[0].isupper() and first in text:
                text = re.sub(rf"(?<!\w){re.escape(first)}(?!\w)", token, text)
        return text

    def redact_text(self, text: str) -> str:
        if self._learned:
            text = self._replace_learned(text)
        text = EMAIL.sub(lambda m: self.token("EMAIL", m.group()), text)

        def card(m: re.Match[str]) -> str:
            digits = re.sub(r"\D", "", m.group())
            return self.token("CARD", digits) if 13 <= len(digits) <= 19 and _luhn(digits) else m.group()

        text = CARD.sub(card, text)
        return PHONE.sub(self._phone, text)

    def _phone(self, m: re.Match[str]) -> str:
        raw = m.group()
        digits = re.sub(r"\D", "", raw)
        bare = raw.isdigit()  # no "+", spaces, dots or dashes
        if not 10 <= len(digits) <= 15 or (bare and len(digits) > 11):
            return raw  # long bare digit runs are IDs, not phone numbers
        return self.token("PHONE", digits)

    def _field_token(self, key: str, value: str) -> str:
        # Same kind + normalisation as the text detectors, so a value redacted by field
        # and the same value found in free text get the same token.
        if "email" in key:
            return self.token("EMAIL", value)
        if "phone" in key:
            return self.token("PHONE", re.sub(r"\D", "", value))
        if "name" in key:
            return self.token("NAME", value)
        return self.token(key.upper()[:12], value)

    def __call__(self, obj: Any) -> Any:
        return self._walk(obj, None)

    def _walk(self, obj: Any, key: str | None) -> Any:
        if isinstance(obj, dict):
            return {k: self._walk(v, str(k)) for k, v in obj.items()}
        if isinstance(obj, list | tuple):
            return [self._walk(v, key) for v in obj]
        if isinstance(obj, str):
            if key and key.lower() in self._fields and obj.strip():
                token = self._field_token(key.lower(), obj)
                self._learn(obj, token)
                return token
            return self.redact_text(obj)
        return obj


def no_redaction() -> Callable[[Any], Any]:
    return lambda obj: obj
