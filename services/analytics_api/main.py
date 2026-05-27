from fastapi import FastAPI
from pydantic import BaseModel
from llm_sql import generate_sql, explain_answer
from executor import execute_sql
from guardrails import validate_sql

app = FastAPI()


class QueryRequest(BaseModel):
    question: str
    provider: str
    model: str
    api_key: str


def is_clarification(sql_text: str) -> bool:
    s = sql_text.strip()
    return (
        s.startswith("AMBIGUOUS")
        or s.lower().startswith("i find that question to be a bit unclear")
    )


def is_out_of_domain(sql_text: str) -> bool:
    return sql_text.strip().lower().startswith("that's an interesting question")


@app.post("/query")
def query(request: QueryRequest):
    try:
        sql_raw = generate_sql(request.question, request.provider, request.model, request.api_key)

        if is_clarification(sql_raw):
            return {"status": "clarification", "message": sql_raw}

        if is_out_of_domain(sql_raw):
            return {"status": "unsupported", "message": sql_raw}

        sql = validate_sql(sql_raw)

        columns, result = execute_sql(sql)

        explanation = explain_answer(request.question, sql, result, request.provider, request.model, request.api_key)

        return {
            "status": "ok",
            "sql": sql,
            "columns": columns,
            "result": result,
            "explanation": explanation,
        }

    except Exception as e:
        return {
            "status": "error",
            "message": str(e),
            "sql": sql_raw if "sql_raw" in locals() else None,
        }
