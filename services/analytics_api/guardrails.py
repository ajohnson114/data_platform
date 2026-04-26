import re

_CODE_FENCE_RE = re.compile(r"^\s*```(?:sql)?\s*|\s*```\s*$", re.IGNORECASE)


def normalize_sql(sql: str) -> str:
    s = _CODE_FENCE_RE.sub("", sql.strip()).strip()

    if s.endswith(";"):
        s = s[:-1].rstrip()

    return s


def validate_sql(sql: str) -> str:
    s = normalize_sql(sql)
    s_lower = s.lower()

    if not (s_lower.startswith("select") or s_lower.startswith("with")):
        raise ValueError("Only SELECT/WITH queries are allowed.")

    if ";" in s_lower:
        raise ValueError("Only a single statement is allowed.")

    forbidden = ["insert", "update", "delete", "drop", "alter", "create", "attach", "copy"]
    for word in forbidden:
        if re.search(rf'(?<![a-z0-9_]){word}(?![a-z0-9_])', s_lower):
            raise ValueError("Unsafe SQL detected.")

    return s
