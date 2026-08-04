import os
import gradio as gr
import requests
from config import PROVIDER_MODELS, DEFAULT_PROVIDER

API_URL = os.getenv("API_URL", "http://localhost:8000/query")


def update_models(provider):
    models = PROVIDER_MODELS[provider]
    return gr.Dropdown(choices=models, value=models[0])


def ask(question, provider, model, api_key):
    if not question.strip():
        return "", "", "", "Please enter a question."

    if not api_key.strip():
        return "", "", "", "Please enter an API key."

    try:
        response = requests.post(
            API_URL,
            json={"question": question, "provider": provider, "model": model, "api_key": api_key},
            timeout=60,
        )
    except Exception as e:
        return "", "", "", f"Could not reach API: {e}"

    try:
        data = response.json()
    except Exception:
        return "", "", "", f"API returned non-JSON (HTTP {response.status_code})."

    status = data.get("status")

    if status == "clarification":
        return "", "", "", data.get("message", "")

    if status == "unsupported":
        return "", "", "", data.get("message", "")

    if status == "error":
        return data.get("sql") or "", "", "", data.get("message", "")

    return (
        data.get("sql", ""),
        str(data.get("result", "")),
        data.get("explanation", ""),
        "Query completed successfully.",
    )


with gr.Blocks() as demo:
    gr.Markdown("## Conversational Analytics Interface")

    with gr.Row():
        provider = gr.Dropdown(
            choices=list(PROVIDER_MODELS.keys()),
            value=DEFAULT_PROVIDER,
            label="Provider",
            scale=1,
        )
        model = gr.Dropdown(
            # Keyed off DEFAULT_PROVIDER rather than hardcoding "openai", so
            # changing the default provider doesn't leave the model dropdown
            # showing another provider's models until the user touches it.
            choices=PROVIDER_MODELS[DEFAULT_PROVIDER],
            value=PROVIDER_MODELS[DEFAULT_PROVIDER][0],
            label="Model",
            scale=2,
        )
        api_key = gr.Textbox(
            label="API Key",
            type="password",
            placeholder="Paste your API key here",
            scale=3,
        )

    provider.change(fn=update_models, inputs=provider, outputs=model)

    gr.Markdown("""
**Example questions:**
- How many posts were made in the last ten minutes?
- What are the most common languages people are posting in?
- Show me the ten longest posts
- Which authors have posted most often?
- How many posts mention "coffee"?
- How does the volume of likes compare to posts?

*The warehouse holds likes, reposts and follows alongside posts — only posts
carry text, and they are a minority of the rows. The last question is there to
exercise that; the model is told to filter on collection accordingly.*
""")

    question = gr.Textbox(
        label="Ask a question",
        placeholder="e.g. How many posts were made in the last ten minutes?",
        lines=2,
    )
    submit_btn = gr.Button("Run Query")

    status = gr.Markdown()
    sql = gr.Textbox(label="Generated SQL", lines=6)
    result = gr.Textbox(label="Result")
    explanation = gr.Textbox(label="Explanation", lines=4)

    submit_btn.click(
        ask,
        inputs=[question, provider, model, api_key],
        outputs=[sql, result, explanation, status],
        show_progress=True,
    )

demo.launch(server_name="0.0.0.0")
