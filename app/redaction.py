import re


_PATTERNS = (
    (re.compile(r'(?i)\bBearer\s+[^\s"\']+'), 'Bearer [redacted]'),
    (re.compile(r'(?i)((?:api[_-]?key|password|secret|token|authorization|cookie)\s*[=:]\s*)[^\r\n,;]+'), r'\1[redacted]'),
    (re.compile(r'\bsk-[A-Za-z0-9_-]{12,}'), '[redacted]'),
    (re.compile(r'(?s)-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----'), '[redacted]'),
)


def sanitize_text(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text
