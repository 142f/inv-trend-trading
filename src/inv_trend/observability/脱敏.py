"""Remove credentials and opaque URLs before persisting or emitting errors."""
from collections.abc import Mapping
import re
from urllib.parse import urlsplit


_SENSITIVE = re.compile(r'(?i)^(api[_ -]?key|access[_ -]?token|token|secret|password|authorization|signature)$')


def redact(value):
    if isinstance(value, Mapping):
        return {str(key): '[REDACTED]' if _SENSITIVE.match(str(key)) else redact(item)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if not isinstance(value, str):
        return value
    def safe_url(match):
        try:
            parsed = urlsplit(match.group(0))
            return f'{parsed.scheme}://{parsed.hostname or "unknown"}/[REDACTED]'
        except ValueError:
            return '[REDACTED_URL]'
    value = re.sub(r'https?://[^\s\"\'<>]+', safe_url, value, flags=re.IGNORECASE)
    value = re.sub(r'(?i)\bBearer\s+[^\s,;]+', 'Bearer [REDACTED]', value)
    value = re.sub(r'''(?i)(["'](?:api[_ -]?key|access[_ -]?token|token|secret|password|authorization|signature)["']\s*:\s*)(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')''',
                   r'\1"[REDACTED]"', value)
    value = re.sub(r'(?i)\b(api[_ -]?key|access[_ -]?token|token|secret|password|authorization|signature)'
                   r'(\s*[:=]\s*)[^\s,;]+', r'\1\2[REDACTED]', value)
    return value[:2048]
