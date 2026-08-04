"""
A cheap first filter over LLM-generated SQL.

WHERE THE ACTUAL BOUNDARY IS.
Not here. The service connects as `analytics_ro`, a ClickHouse user with
`readonly=1` and a single grant, `SELECT ON analytics.*`, and without the global
FILE/S3/URL/REMOTE privileges the table functions need. The server refuses every
write, DDL, mutation and table function regardless of what the model generates
and regardless of what this module does or does not catch. An LLM writes this
service's SQL, so the enforcement has to sit somewhere the LLM cannot reach.

What this adds is defence in depth: obviously unsafe generated SQL never leaves
the process, and the failure is a clear message instead of a driver error. That
is worth having, and it is worth being honest that a gap here costs a round trip
to a server that says no, not a breach.
"""
import re

_CODE_FENCE_RE = re.compile(r"^\s*```(?:sql)?\s*|\s*```\s*$", re.IGNORECASE)

# Blocked as whole words, and scanned against a copy with literals and comments
# masked out -- see mask_literals.
#
# Most of this list is belt-and-braces rather than load-bearing, and it is worth
# knowing which is which. A statement here must already START with SELECT or
# WITH and contain no statement separator, so a DDL keyword can only appear as
# an identifier, inside a literal, or as a syntax error -- there is nowhere for
# a second statement to go. The two that genuinely matter mid-SELECT are INTO
# and OUTFILE: ClickHouse's `SELECT ... INTO OUTFILE` is a real exfiltration
# path out of an otherwise read-only query.
#
# `copy` used to be on this list and is not a ClickHouse statement at all. It
# bought nothing and cost every question about the word.
_FORBIDDEN = (
    "insert", "update", "delete", "drop", "alter", "create",
    "attach", "detach", "exchange", "rename", "truncate",
    "optimize", "system", "grant",
    "into", "outfile",
)

_FORBIDDEN_RE = re.compile(
    r"(?<![a-z0-9_])(?:" + "|".join(_FORBIDDEN) + r")(?![a-z0-9_])"
)


def normalize_sql(sql: str) -> str:
    s = _CODE_FENCE_RE.sub("", sql.strip()).strip()

    if s.endswith(";"):
        s = s[:-1].rstrip()

    return s


def _consume_quoted(sql: str, i: int, quote: str) -> int:
    """
    Index just past the quoted run starting at `sql[i] == quote`.

    Handles both escape forms ClickHouse accepts inside a quoted run: a
    backslash pair, and the delimiter doubled. An unterminated run returns the
    end of the string.
    """
    n = len(sql)
    i += 1
    while i < n:
        if sql[i] == "\\":
            i += 2
        elif sql[i] == quote:
            if sql[i:i + 2] == quote * 2:
                i += 2                            # doubled delimiter is an escape
            else:
                return i + 1                      # the closing delimiter
        else:
            i += 1
    return n


def mask_literals(sql: str) -> str:
    """
    Blank out string literals and comments, so the checks below see code only.

    WHY THIS IS NEEDED.
    Scanning the raw statement conflates SQL that DOES something with SQL that
    merely MENTIONS it. `WHERE operation = 'create'` was rejected -- and that
    column's only two legal values are 'create' and 'update', both blocked
    words, so an entirely ordinary question about this dataset was unanswerable.
    So was "how many posts mention 'delete'". Both fail closed, so they were a
    usability cost rather than a hole, but the cost was real and the fix is not
    a special case for one comparison: it is scanning the right string.

    ONE PASS, BOTH CONSTRUCTS, and that is deliberate rather than tidy. Handling
    them separately desynchronises the mask from what ClickHouse will parse.
    Strip comments first and `LIKE '%--%'` loses the rest of its literal; mask
    quotes first and an apostrophe inside `-- don't` opens a literal that
    swallows real code the server would execute. Only a single scan agrees with
    the parser on both.

    Comments become a space rather than nothing, so masking cannot fuse two
    tokens into a word that was never there.

    QUOTED IDENTIFIERS ARE MASKED TOO, and that is not cosmetic. Backtick and
    double-quote delimit identifiers in ClickHouse -- `"create"` is a column
    reference, not the string -- so their contents can never be a statement
    keyword and blanking them is correct. The reason it is *necessary* is that
    an apostrophe inside one would otherwise open a literal the parser never
    opened: `SELECT `it's`, 1 FROM t; DROP TABLE t` masked to `SELECT `it''`,
    swallowing the separator and the DDL behind it, and the scan then saw a
    clean single SELECT. Every construct an apostrophe can legally sit inside
    has to be modelled, or the mask disagrees with the parser in the direction
    that fails open.

    An unterminated literal or comment consumes the remainder. That is the safe
    direction: it can only hide text from the scanner, and text hidden this way
    is text ClickHouse will reject as a syntax error.
    """
    out = []
    i, n = 0, len(sql)

    while i < n:
        char = sql[i]

        if char == "'":
            out.append("''")
            i = _consume_quoted(sql, i, "'")

        elif char in "`\"":
            # Emit the empty identifier rather than nothing, so masking cannot
            # fuse the tokens on either side into a word that was never there.
            out.append(char * 2)
            i = _consume_quoted(sql, i, char)

        # `#` is a line comment in ClickHouse as well as `--`. Missing it left
        # the same apostrophe hole: `SELECT 1 # don't` opened a literal that ran
        # past the newline and hid everything after it.
        elif sql.startswith("--", i) or char == "#":
            while i < n and sql[i] != "\n":
                i += 1
            out.append(" ")

        elif sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            i = n if end == -1 else end + 2
            out.append(" ")

        else:
            out.append(char)
            i += 1

    return "".join(out)


def validate_sql(sql: str) -> str:
    """
    Return the statement to execute, or raise ValueError.

    Every check runs against the masked copy; what comes back is the real
    statement. Validating one string and executing another is the only ordering
    that works here -- the mask exists to answer "what does this SQL do", and
    the server needs the literals it was going to operate on.
    """
    s = normalize_sql(sql)
    masked = mask_literals(s).lower().strip()

    if not (masked.startswith("select") or masked.startswith("with")):
        raise ValueError("Only SELECT/WITH queries are allowed.")

    if ";" in masked:
        raise ValueError("Only a single statement is allowed.")

    if _FORBIDDEN_RE.search(masked):
        raise ValueError("Unsafe SQL detected.")

    return s
