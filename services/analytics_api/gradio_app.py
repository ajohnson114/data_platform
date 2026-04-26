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
            choices=PROVIDER_MODELS["openai"],
            value=PROVIDER_MODELS["openai"][0],
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
- What is the latest price for each coin?
- Which coin has the highest USD price right now?
- Show me all Bitcoin prices from the last hour
- How does the Ethereum price in USD compare to EUR over time?
- What was the highest Solana price recorded today?
- Rank all coins by their most recent USD price
""")

    question = gr.Textbox(
        label="Ask a question",
        placeholder="e.g. What is the latest price for each coin?",
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
