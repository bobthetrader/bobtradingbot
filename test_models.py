"""Quick test — calls each OpenRouter model directly and prints the result."""
import os
from dotenv import load_dotenv
load_dotenv()

import openai

OR_KEY = os.getenv("OPENROUTER_API_KEY", "")
AI_KEY = os.getenv("OPENAI_API_KEY", "")

MODELS = [
    ("openrouter", "nousresearch/hermes-3-llama-3.1-70b",      "hermes"),
    ("openrouter", "perplexity/sonar",                          "sonar"),
    ("openrouter", "deepseek/deepseek-r1",                      "deepseek"),
    ("openrouter", "meta-llama/llama-3.1-8b-instruct",         "llama"),
    ("openrouter", "openai/gpt-4o-mini",                        "gpt-via-or"),
]

PROMPT = "Reply with exactly: 'Score: 1. Test: working.' Nothing else."

for provider, model_id, name in MODELS:
    print(f"\n{'='*50}")
    print(f"Testing: {name} ({model_id})")
    try:
        if provider == "openrouter":
            client = openai.OpenAI(
                api_key=OR_KEY,
                base_url="https://openrouter.ai/api/v1",
                default_headers={"HTTP-Referer": "https://github.com/tradingbot"},
            )
        else:
            client = openai.OpenAI(api_key=AI_KEY)

        resp = client.chat.completions.create(
            model=model_id,
            messages=[{"role": "user", "content": PROMPT}],
            max_tokens=50,
            timeout=20,
        )
        print(f"SUCCESS: {resp.choices[0].message.content.strip()}")
    except Exception as e:
        print(f"FAILED:  {e}")
