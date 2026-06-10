"""One-shot probe: verify the OpenAI key in .env works.

Throwaway. Delete after Day 4 verification.
"""

import os
import sys

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()


def main() -> int:
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        print("✗ OPENAI_API_KEY not set in .env")
        return 1
    if not key.startswith("sk-"):
        print(f"✗ Key doesn't look right (starts with {key[:5]!r}, expected 'sk-')")
        return 1
    print(f"✓ Key loaded ({key[:7]}...{key[-4:]})")

    client = OpenAI()
    print("Calling gpt-4o-mini with a 1-token prompt...")
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "Reply with exactly: ok"}],
        max_tokens=5,
    )
    text = resp.choices[0].message.content.strip()
    usage = resp.usage
    print(f"✓ Response: {text!r}")
    print(f"  Tokens: prompt={usage.prompt_tokens}, completion={usage.completion_tokens}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
