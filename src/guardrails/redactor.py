import copy
import re
from typing import Any, Dict, List, Optional, Pattern, Tuple, Union


class PIIRedactor:
    """
    Sanitizes sensitive Personally Identifiable Information (PII) and credentials
    from execution traces, step logs, extracted data payloads, and diagnostic outputs.
    Scrubs SSNs, payment card PANs, bank account numbers, credentials, and email addresses.
    """

    DEFAULT_PATTERNS: List[Tuple[str, str, Pattern]] = [
        (
            "SSN",
            "[REDACTED_SSN]",
            re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
        ),
        (
            "CREDIT_CARD",
            "[REDACTED_CARD]",
            re.compile(r"\b(?:\d{4}[ -]?){3}\d{4}\b"),
        ),
        (
            "BANK_ACCOUNT",
            "[REDACTED_ACCOUNT]",
            re.compile(r"\b(?:CHK|SAV|MM)-\d{4,8}\b", re.IGNORECASE),
        ),
        (
            "SECRET_TOKEN",
            "[REDACTED_TOKEN]",
            re.compile(r"\b(?:sk-[a-zA-Z0-9_\-]{20,}|Bearer\s+[a-zA-Z0-9_\-\.]{15,})\b", re.IGNORECASE),
        ),
        (
            "EMAIL",
            "[REDACTED_EMAIL]",
            re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
        ),
    ]

    def __init__(self, custom_patterns: Optional[List[Tuple[str, str, Pattern]]] = None):
        self.patterns = list(self.DEFAULT_PATTERNS)
        if custom_patterns:
            self.patterns.extend(custom_patterns)

    def redact_text(self, text: str) -> str:
        """Applies all regex redaction patterns sequentially to a string."""
        if not text or not isinstance(text, str):
            return text

        result = text
        for _, placeholder, pattern in self.patterns:
            result = pattern.sub(placeholder, result)
        return result

    def redact_data(self, data: Any) -> Any:
        """
        Recursively scrubs PII across arbitrary data structures
        (dicts, lists, tuples, and primitives).
        """
        if data is None:
            return None

        if isinstance(data, str):
            return self.redact_text(data)

        if isinstance(data, dict):
            return {
                self.redact_text(str(k)): self.redact_data(v)
                for k, v in data.items()
            }

        if isinstance(data, list):
            return [self.redact_data(item) for item in data]

        if isinstance(data, tuple):
            return tuple(self.redact_data(item) for item in data)

        return data

    def redact_dict(self, d: Dict[str, Any]) -> Dict[str, Any]:
        """Convenience wrapper for scrubbing a dictionary payload."""
        return self.redact_data(d)
