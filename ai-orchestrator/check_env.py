
import os
import sys
from pathlib import Path

# Add services to path
sys.path.insert(0, str(Path(os.getcwd())))

from services.local_agent_runner import detect_anthropic, detect_ollama, OLLAMA_HOST, ANTHROPIC_MODEL

print("--- DIAGNOSTIC START ---")
print(f"OS: {os.name}")
print(f"ANTHROPIC_API_KEY set: {bool(os.environ.get('ANTHROPIC_API_KEY'))}")
print(f"OLLAMA_HOST: {OLLAMA_HOST}")
print(f"ANTHROPIC_MODEL: {ANTHROPIC_MODEL}")

print("\nTesting detectors...")
print(f"detect_anthropic(): {detect_anthropic()}")
print(f"detect_ollama(): {detect_ollama()}")

if detect_anthropic():
    print("\nTesting Anthropic mapping manually...")
    import anthropic
    try:
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        prompt = "Return ONLY the string '#test'"
        resp = client.messages.create(model=ANTHROPIC_MODEL, max_tokens=10, messages=[{"role":"user", "content":prompt}])
        print(f"Anthropic Response: {resp.content[0].text if isinstance(resp.content, list) else resp.content}")
    except Exception as e:
        print(f"Anthropic direct test failed: {e}")

print("--- DIAGNOSTIC END ---")
