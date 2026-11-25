# probe.py: throwaway check that Render can reach the VŠE LiteLLM proxy.
# Makes 1 embedding call + 1 chat call (with seed) and prints status and time. Never prints the key.
# Run locally:  python scripts/probe.py      On Render: see README "Phase 1 probe".
# Model names are typed here only because this script is temporary; the app reads them from app_config.json.
import os, sys, time
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()                                        # local .env; no effect on Render
key = os.environ.get("LITELLM_API_KEY", "")
base = os.environ.get("LITELLM_BASE_URL", "https://litellm.vse.cz/v1")
print("python", sys.version.split()[0], "| base_url", base, "| key set:", bool(key))
if not key:
    sys.exit("PROBE FAILED: LITELLM_API_KEY is not set")

client = OpenAI(base_url=base, api_key=key, timeout=60)

def timed(name, fn):
    t0 = time.time()
    try:
        out = fn()
        print(f"OK   {name}: {time.time() - t0:.1f} s | {out}")
    except Exception as e:                           # show the error type and message, not the key
        print(f"FAIL {name}: {time.time() - t0:.1f} s | {type(e).__name__}: {str(e)[:200]}")

timed("embedding qwen3-embedding:4b",
      lambda: f"dim {len(client.embeddings.create(model='qwen3-embedding:4b', input=['Praha']).data[0].embedding)}")
timed("chat Qwen3.8-27B with seed=42",
      lambda: repr(client.chat.completions.create(
          model="Qwen3.8-27B", temperature=0, seed=42, max_tokens=1024,
          messages=[{"role": "user", "content": 'Odpověz pouze tímto JSON a ničím jiným: {"ok": true}'}]
      ).choices[0].message.content.strip()[:60]))
