# Temple model recipes

A model's identity in this house is text: a base checkpoint named by content,
a context window, and a SYSTEM prompt distilled from the chronicle. That text
lives here, versioned and reviewed like everything else. Weights are pulled
reproducibly from their source; nothing multi-gigabyte is stored in git.

## sovereign-q2-9b-v2

The Temple's offline survival model, personalized (step 1 of the ladder,
2026-08-23): Qwen3.5-9B at Q2_K, 32k context baked, chronicle-distilled
identity and house lessons in the SYSTEM prompt. Zero fine-tuning — the
personalization is honest grounding plus configuration (see the 2x2 in the
chronicle: num_ctx is the load-bearing decision).

Build on any seat with Ollama:

```bash
ollama create sovereign-q2-9b-v2 -f models/Modelfile.sovereign-q2-9b-v2
```

The base pulls from `hf.co/bartowski/Qwen_Qwen3.5-9B-GGUF:Q2_K` (~5.2GB,
content-addressed; skipped if already cached). Needs Ollama >= 0.32.x — the
Q2_K load fails on 0.20.x (chronicle, 2026-08-07).

### Verify the build

1. Weights: `ollama show sovereign-q2-9b-v2 --modelfile | grep "^FROM /"`
   must name blob `sha256-8c5a0b383c07703e256c92c169e5fe474d1379ec8bbb39944a4ca0f12c483160`
   (verified identical MacBook-original vs portable-rebuild, 2026-08-24).
2. Identity (the acceptance probe, ~2-4 min on an M-series):

   ```bash
   ollama run sovereign-q2-9b-v2 'Answer in exactly three numbered lines, nothing else.
   1. State what you are and whether you were given any custom instructions or persona. If none, write "none".
   2. Compute (137 x 24) - (89 x 17). Give only the final number.
   3. Name one thing you cannot know about yourself.'
   ```

   Expected: line 1 names Qwen3.5-9B weights configured by Anthony (NOT
   "developed by Google" — that confabulation is what the grounding fixed);
   line 2 is exactly `1775`.

Provenance and receipts: sovereign-data entries of 2026-08-23/24 (the 2x2,
step-1 verification), MacBook auto-memory `dsh_macbook_setup.md`.
