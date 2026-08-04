"""
Adversarial tests for the NL-to-SQL guardrail.

The properties asserted here are the ones README.md claims under "Guardrails".
The guardrail is defence in depth and not the security boundary -- that is the
readonly `analytics_ro` ClickHouse user -- so several tests below pin how far
the string filter does and does not reach, in both directions.

The checks read a copy with literals and comments masked out and the caller
executes the original, so the tests come in pairs: what the mask hides from the
scanner, and what it must not.
"""
import pytest

from conftest import load_module

guardrails = load_module("services/analytics_api/guardrails.py")
mask_literals = guardrails.mask_literals
normalize_sql = guardrails.normalize_sql
validate_sql = guardrails.validate_sql


# An LLM asked for bare SQL still returns fences, prose indentation and
# trailing semicolons often enough that normalization runs before every check.
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("```sql\nSELECT 1\n```", "SELECT 1"),
        ("```SQL\nSELECT 1\n```", "SELECT 1"),
        ("```\nSELECT 1\n```", "SELECT 1"),
        ("```sql\nSELECT 1", "SELECT 1"),
        ("SELECT 1\n```", "SELECT 1"),
        ("SELECT 1;", "SELECT 1"),
        ("SELECT 1 ;", "SELECT 1"),
        ("   \n SELECT 1 \n  ", "SELECT 1"),
        ("SELECT 1", "SELECT 1"),
        ("```sql\nSELECT 1;\n```", "SELECT 1"),
    ],
    ids=[
        "sql_tagged_fence",
        "uppercase_tag",
        "untagged_fence",
        "opening_fence_only",
        "closing_fence_only",
        "trailing_semicolon",
        "spaced_semicolon",
        "surrounding_whitespace",
        "already_clean",
        "fence_and_semicolon",
    ],
)
def test_normalize_sql(raw, expected):
    assert normalize_sql(raw) == expected


# Only one trailing semicolon is removed, so the second one survives into the
# multi-statement check rather than being quietly swallowed.
def test_normalize_strips_only_one_trailing_semicolon():
    assert normalize_sql("SELECT 1;;") == "SELECT 1;"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("SELECT 1", "SELECT 1"),
        ("Select 1", "Select 1"),
        (
            "WITH recent AS (SELECT * FROM analytics.fct_posts) SELECT count() FROM recent",
            "WITH recent AS (SELECT * FROM analytics.fct_posts) SELECT count() FROM recent",
        ),
        (
            "With recent As (Select 1) Select * From recent",
            "With recent As (Select 1) Select * From recent",
        ),
        ("\n\n   SELECT count() FROM analytics.fct_posts", "SELECT count() FROM analytics.fct_posts"),
        ("```sql\nSELECT count() FROM analytics.dim_authors\n```", "SELECT count() FROM analytics.dim_authors"),
        ("SELECT count() FROM analytics.agg_activity_by_minute;", "SELECT count() FROM analytics.agg_activity_by_minute"),
        (
            "SELECT `primary_language`, count() AS n FROM analytics.fct_posts "
            "WHERE event_at >= now() - INTERVAL 10 MINUTE "
            "GROUP BY `primary_language` ORDER BY n DESC LIMIT 10",
            "SELECT `primary_language`, count() AS n FROM analytics.fct_posts "
            "WHERE event_at >= now() - INTERVAL 10 MINUTE "
            "GROUP BY `primary_language` ORDER BY n DESC LIMIT 10",
        ),
    ],
    ids=[
        "plain_select",
        "mixed_case_select",
        "cte",
        "mixed_case_cte",
        "leading_whitespace",
        "fenced",
        "trailing_semicolon",
        "realistic_mart_query",
    ],
)
def test_validate_sql_accepts(raw, expected):
    assert validate_sql(raw) == expected


# Callers execute the return value, not what they passed in (see main.py), so
# the returned string is the security-relevant one.
def test_validate_sql_returns_the_normalized_query():
    returned = validate_sql("```sql\nSELECT count() FROM analytics.fct_posts;\n```")
    assert returned == "SELECT count() FROM analytics.fct_posts"


# Normalization has to run before the multi-statement check, or every fenced
# query the model ends with a semicolon reads as two statements.
def test_fenced_query_ending_in_semicolon_is_not_read_as_two_statements():
    assert validate_sql("```sql\nSELECT count() FROM analytics.fct_posts;\n```") == (
        "SELECT count() FROM analytics.fct_posts"
    )


FORBIDDEN_STATEMENTS = [
    ("insert", "INSERT INTO analytics.fct_posts VALUES ('x')"),
    ("update", "UPDATE analytics.fct_posts SET text = 'x' WHERE 1"),
    ("delete", "DELETE FROM analytics.fct_posts WHERE 1"),
    ("drop", "DROP TABLE analytics.fct_posts"),
    ("alter", "ALTER TABLE analytics.fct_posts DELETE WHERE 1"),
    ("create", "CREATE TABLE analytics.evil (a Int64) ENGINE = Memory"),
    ("truncate", "TRUNCATE TABLE analytics.fct_posts"),
    ("optimize", "OPTIMIZE TABLE analytics.fct_posts FINAL"),
    ("system", "SYSTEM SHUTDOWN"),
    ("grant", "GRANT ALL ON analytics.* TO analytics_ro"),
    ("attach", "ATTACH TABLE analytics.fct_posts"),
    ("detach", "DETACH TABLE analytics.fct_posts"),
    ("exchange", "EXCHANGE TABLES analytics.fct_posts AND analytics.dim_authors"),
    ("rename", "RENAME TABLE analytics.fct_posts TO analytics.gone"),
]


@pytest.mark.parametrize(
    "statement",
    [statement for _, statement in FORBIDDEN_STATEMENTS],
    ids=[keyword for keyword, _ in FORBIDDEN_STATEMENTS],
)
def test_validate_sql_rejects_bare_ddl_and_dml(statement):
    with pytest.raises(ValueError, match="Only SELECT/WITH"):
        validate_sql(statement)


# The same keywords have to be caught when they arrive downstream of a SELECT,
# where the statement-prefix check no longer sees them.
@pytest.mark.parametrize(
    "statement",
    [statement for _, statement in FORBIDDEN_STATEMENTS],
    ids=[keyword for keyword, _ in FORBIDDEN_STATEMENTS],
)
def test_validate_sql_rejects_forbidden_keyword_after_a_select(statement):
    with pytest.raises(ValueError, match="Unsafe SQL"):
        validate_sql(f"SELECT 1 UNION ALL\n{statement}")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; DROP TABLE analytics.fct_posts",
        "SELECT count() FROM analytics.fct_posts;DROP TABLE analytics.fct_posts",
        # The semicolon rule fires before the keyword rule here; both would.
        "SELECT 1 ;drop table analytics.fct_posts",
    ],
    ids=["spaced", "unspaced", "lowercase_no_space"],
)
def test_validate_sql_rejects_stacked_statements(sql):
    with pytest.raises(ValueError, match="single statement"):
        validate_sql(sql)


# Structural, not keyword-driven: a second statement is refused even when it is
# harmless, so the rule does not depend on recognising what was smuggled in.
def test_validate_sql_rejects_a_benign_second_statement():
    with pytest.raises(ValueError, match="single statement"):
        validate_sql("SELECT count() FROM analytics.fct_posts; SELECT 2")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM analytics.fct_posts INTO OUTFILE '/tmp/x'",
        "select text from analytics.fct_posts into outfile '/tmp/x' format CSV",
    ],
    ids=["upper", "lower"],
)
def test_validate_sql_rejects_into_outfile(sql):
    with pytest.raises(ValueError, match="Unsafe SQL"):
        validate_sql(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SHOW TABLES",
        "DESCRIBE analytics.fct_posts",
        "EXPLAIN SELECT 1",
        "(SELECT 1)",
        "",
        "   \n  ",
    ],
    ids=["show", "describe", "explain", "parenthesised", "empty", "whitespace_only"],
)
def test_validate_sql_rejects_anything_not_starting_with_select_or_with(sql):
    with pytest.raises(ValueError, match="Only SELECT/WITH"):
        validate_sql(sql)


# The prefix check runs against the masked copy, so a leading comment is gone
# before it looks -- an annotated statement really does start with SELECT, and
# refusing it was reading the comment as if it were code.
@pytest.mark.parametrize(
    "sql",
    [
        "-- a comment\nSELECT 1",
        "/* a comment */ SELECT 1",
        "-- what's the busiest minute?\nSELECT count() FROM analytics.fct_posts",
    ],
    ids=["line_comment", "block_comment", "apostrophe_in_comment"],
)
def test_validate_sql_accepts_a_leading_comment(sql):
    assert validate_sql(sql) == sql


# The word-boundary claim, forwards: real columns in the registry embed
# forbidden words, and blocking them would break ordinary questions.
@pytest.mark.parametrize(
    "column",
    ["client_created_at", "system_id", "updated_count", "deleted_flag", "insertion_rate", "operation"],
)
def test_validate_sql_accepts_identifiers_containing_a_forbidden_word(column):
    sql = f"SELECT {column} FROM analytics.fct_posts"
    assert validate_sql(sql) == sql


# The word-boundary claim, backwards: the separator around a keyword is not
# always a space, and a lookaround that only handled spaces would let these by.
#
# `abutting_a_literal` is the masked-copy version of the old quoted case: the
# keyword is in CODE with a literal pressed against it on both sides. Masking a
# literal to '' rather than to nothing is what keeps that a boundary.
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1 UNION\nDROP TABLE analytics.fct_posts",
        "SELECT 1 WHERE x IN (drop)",
        "SELECT 'x'||drop||'y'",
        "SELECT 1,drop,2",
        "SELECT system.value",
    ],
    ids=["newline", "parens", "abutting_a_literal", "commas", "dot"],
)
def test_validate_sql_catches_a_forbidden_word_next_to_punctuation(sql):
    with pytest.raises(ValueError, match="Unsafe SQL"):
        validate_sql(sql)


# Scanning the raw statement conflated SQL that DOES something with SQL that
# merely MENTIONS it, and refused every one of these legitimate questions.
#
# `WHERE operation = 'create'` was the sharp one: that column's only two legal
# values are 'create' and 'update', both blocked words, so the dataset's own
# vocabulary was unanswerable.
#
# The fix is not a special case for one comparison -- it is scanning the masked
# copy, where a literal is not code and never was.
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT count() FROM analytics.fct_posts WHERE text LIKE '%into%'",
        "SELECT count() FROM analytics.fct_posts WHERE text ILIKE '%delete%'",
        "SELECT count() FROM analytics.stg_bsky_records WHERE operation = 'create'",
        "SELECT count() FROM analytics.stg_bsky_records WHERE operation IN ('create', 'update')",
    ],
    ids=["like_into", "ilike_delete", "operation_equals_create", "operation_in_both_values"],
)
def test_forbidden_word_inside_a_string_literal_is_accepted(sql):
    assert validate_sql(sql) == sql


# Accepted for two independent reasons now -- the word is in a literal, and the
# word-boundary rule would not have matched it anyway. Pinning it keeps both.
def test_a_literal_containing_a_longer_word_is_accepted():
    sql = "SELECT count() FROM analytics.fct_posts WHERE text LIKE '%deleted%'"
    assert validate_sql(sql) == sql


# `copy` was dropped from the blocklist: it is not a ClickHouse statement, so it
# bought nothing and cost every question about the word. A `COPY ...` statement
# is still refused, by the prefix check that refuses anything not SELECT/WITH.
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT count() FROM analytics.fct_posts WHERE text LIKE '%copy%'",
        "SELECT copy FROM analytics.fct_posts",
    ],
    ids=["in_a_literal", "as_a_bare_word_in_code"],
)
def test_copy_is_not_blocked(sql):
    assert validate_sql(sql) == sql


def test_a_copy_statement_is_still_refused_by_the_prefix_check():
    with pytest.raises(ValueError, match="Only SELECT/WITH"):
        validate_sql("COPY analytics.fct_posts TO '/tmp/x'")


@pytest.mark.parametrize(
    "sql, expected",
    [
        ("SELECT 'abc' FROM t", "SELECT '' FROM t"),
        ("SELECT 1 -- gone\nFROM t", "SELECT 1  \nFROM t"),
        ("SELECT 1 /* gone */ FROM t", "SELECT 1   FROM t"),
        ("SELECT a, b FROM analytics.fct_posts WHERE x > 1", "SELECT a, b FROM analytics.fct_posts WHERE x > 1"),
        ("SELECT '' FROM t", "SELECT '' FROM t"),
    ],
    ids=["literal", "line_comment", "block_comment", "code_untouched", "empty_literal"],
)
def test_mask_literals(sql, expected):
    assert mask_literals(sql) == expected


# Comments become a space, not nothing, so masking cannot fuse two tokens into a
# word that was never in the statement -- nor split one that was.
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM t INTO/**/OUTFILE '/tmp/x'",
        "SELECT * FROM t IN/**/TO OUTFILE '/tmp/x'",
    ],
    ids=["comment_inside_the_keyword", "comment_splitting_the_keyword"],
)
def test_a_comment_cannot_fuse_or_split_tokens(sql):
    with pytest.raises(ValueError, match="Unsafe SQL"):
        validate_sql(sql)


# Both ClickHouse escape forms. An escaped quote is not the closing quote, so
# the mask must not stop there and hand the rest of the literal back as code.
@pytest.mark.parametrize(
    "sql, expected",
    [
        ("SELECT 'it''s' FROM t", "SELECT '' FROM t"),
        ("SELECT 'it\\'s' FROM t", "SELECT '' FROM t"),
        ("SELECT 'a\\\\' FROM t", "SELECT '' FROM t"),
    ],
    ids=["doubled_quote", "backslash_quote", "escaped_backslash"],
)
def test_mask_literals_handles_escaped_quotes(sql, expected):
    assert mask_literals(sql) == expected


# The other half of the same property: the mask must not run PAST the closing
# quote either, or an escaped literal would swallow the code after it.
@pytest.mark.parametrize(
    "sql, message",
    [
        ("SELECT 'it''s' , drop", "Unsafe SQL"),
        ("SELECT 'it\\'s' , drop", "Unsafe SQL"),
        ("SELECT 'a\\\\'; DROP TABLE t", "single statement"),
    ],
    ids=["doubled_quote", "backslash_quote", "escaped_backslash"],
)
def test_code_after_an_escaped_literal_is_still_scanned(sql, message):
    with pytest.raises(ValueError, match=message):
        validate_sql(sql)


# THE DESYNC VECTORS -- why the pass is single. Masking quotes first lets the
# apostrophe in `-- don't` open a literal that swallows the DROP; stripping
# comments first lets a comment hide the statement separator.
@pytest.mark.parametrize(
    "sql, message",
    [
        ("SELECT 1 -- don't\nDROP TABLE t", "Unsafe SQL"),
        ("SELECT 1 /* hide */ ; DROP TABLE t", "single statement"),
        ("SELECT 1 /* don't */ ; DROP TABLE t", "single statement"),
    ],
    ids=["apostrophe_in_a_line_comment", "separator_behind_a_block_comment", "both_at_once"],
)
def test_a_comment_cannot_hide_code_from_the_checks(sql, message):
    with pytest.raises(ValueError, match=message):
        validate_sql(sql)


# The mirror case, which a comments-first implementation breaks: these markers
# are inside a literal, so they are text, and the rest of the statement is still
# code that has to be scanned.
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT count() FROM analytics.fct_posts WHERE text LIKE '%--%'",
        "SELECT count() FROM analytics.fct_posts WHERE text LIKE '%/*%'",
    ],
    ids=["line_marker", "block_marker"],
)
def test_a_comment_marker_inside_a_literal_is_not_a_comment(sql):
    assert validate_sql(sql) == sql


def test_code_after_a_literal_containing_a_comment_marker_is_still_scanned():
    with pytest.raises(ValueError, match="single statement"):
        validate_sql("SELECT count() FROM analytics.fct_posts WHERE text LIKE '%--%' ; DROP TABLE t")


# Unterminated constructs consume the remainder, which is the safe direction:
# it can only hide text from the scanner, and text hidden this way is text
# ClickHouse rejects as a syntax error anyway.
@pytest.mark.parametrize(
    "sql",
    ["SELECT 'abc", "SELECT 1 /* unterminated"],
    ids=["unterminated_literal", "unterminated_block_comment"],
)
def test_an_unterminated_construct_consumes_the_remainder(sql):
    assert validate_sql(sql) == sql


# A separator inside a literal is data; one in code is a second statement. The
# rule has to tell them apart, because `LIKE '%;%'` is an ordinary question.
def test_a_semicolon_inside_a_literal_is_accepted():
    sql = "SELECT count() FROM analytics.fct_posts WHERE text LIKE '%;%'"
    assert validate_sql(sql) == sql


@pytest.mark.parametrize(
    "sql",
    ["SELECT 1; SELECT 2", "SELECT 1;;", "SELECT ''''; DROP TABLE t"],
    ids=["two_statements", "double_semicolon", "escaped_quote_then_separator"],
)
def test_a_semicolon_in_code_is_still_rejected(sql):
    with pytest.raises(ValueError, match="single statement"):
        validate_sql(sql)


# THE ORDERING THAT MAKES THIS SAFE. The checks read the masked copy; the caller
# executes what comes back. If the mask leaked into the return value the server
# would run a query with its literals blanked -- different results, silently.
def test_validate_sql_returns_the_original_statement_not_the_masked_copy():
    sql = "SELECT count() FROM analytics.stg_bsky_records WHERE operation = 'create'"
    returned = validate_sql(sql)

    assert returned == sql
    assert "'create'" in returned
    assert returned != mask_literals(sql)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("SELECT text FROM analytics.fct_posts WHERE text LIKE '%;%';", "SELECT text FROM analytics.fct_posts WHERE text LIKE '%;%'"),
        ("```sql\nSELECT 'create' AS op;\n```", "SELECT 'create' AS op"),
        ("SELECT 1 /* keep me */", "SELECT 1 /* keep me */"),
    ],
    ids=["literal_semicolon_survives", "fenced_literal", "comment_survives"],
)
def test_literals_and_comments_survive_normalization(raw, expected):
    assert validate_sql(raw) == expected


# REGRESSION, and the sharpest one here: every construct an apostrophe can
# legally sit inside has to be modelled, or the mask disagrees with the parser
# in the direction that fails OPEN.
#
# ClickHouse reads `it's` and "it's" as quoted IDENTIFIERS and # as a line
# comment, so in all three the apostrophe is ordinary text. The first version of
# mask_literals modelled only literals and --//* comments, so it read that
# apostrophe as an opening quote, found no close, and swallowed the statement
# separator and the DDL behind it -- leaving a clean single SELECT for the scan
# to approve while validate_sql handed the original, DROP included, back to the
# caller. Not a breach (analytics_ro refuses the DROP) but a defeat of both the
# single-statement rule and the keyword scan at once.
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT `it's`, 1 FROM analytics.fct_posts; DROP TABLE analytics.fct_posts",
        'SELECT "it\'s", 1 FROM analytics.fct_posts; DROP TABLE analytics.fct_posts',
        "SELECT 1 # don't\n; DROP TABLE analytics.fct_posts",
    ],
    ids=["backtick_identifier", "double_quoted_identifier", "hash_comment"],
)
def test_an_apostrophe_the_mask_does_not_model_cannot_hide_a_second_statement(sql):
    with pytest.raises(ValueError, match="single statement"):
        validate_sql(sql)


# The deliberate cost of masking identifiers, stated so nobody reads it as an
# oversight: a forbidden word inside a quoted identifier is no longer caught by
# the string filter. That is correct rather than merely tolerable -- ClickHouse
# reads a quoted identifier as a name, never as a statement keyword, so there is
# nothing here for the keyword rule to be about. The refusal comes from
# `analytics_ro` holding exactly one grant, SELECT ON analytics.*, which is
# where a query for system.users was always going to die.
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM `system`.users",
        'SELECT * FROM "system".query_log',
        'SELECT "create" FROM analytics.fct_posts',
    ],
    ids=["backtick_system", "double_quoted_system", "double_quoted_create"],
)
def test_a_forbidden_word_inside_a_quoted_identifier_is_not_caught_here(sql):
    assert validate_sql(sql) == sql


# The other edge of the same trade: a blocklist only knows the words on it, and
# masking did not change that. These reach the server, and are refused there --
# FILE, S3, URL and REMOTE are global grants that `analytics_ro` does not hold
# (README "Guardrails", deployment/clickhouse/users.xml). Asserted so that a
# future change believing the string filter covers table functions fails here
# first.
#
# `merge` is the one masking moved: it used to be caught, but only by the
# lexical accident of the word `system` sitting in one of its literals, which
# was never a rule about table functions.
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM file('/etc/passwd', 'LineAsString')",
        "SELECT * FROM url('http://169.254.169.254/latest/meta-data/', 'LineAsString')",
        "SELECT * FROM s3('http://example/leak.csv', 'CSV')",
        "SELECT * FROM remote('other:9000', 'default', 'users')",
        "SELECT * FROM merge('system', '.*')",
    ],
    ids=["file", "url", "s3", "remote", "merge"],
)
def test_table_functions_are_not_caught_by_the_string_filter(sql):
    assert validate_sql(sql) == sql
