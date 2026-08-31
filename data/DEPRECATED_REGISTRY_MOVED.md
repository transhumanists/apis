# DEPRECATED — platform_registry.json has moved

**This file is no longer the canonical source.**

`data/platform_registry.json` has been migrated to its canonical home:

```
neohiro/apis/src/registry.json
```

and is accessible at runtime via:

```js
import { registry, loadRegistry, platform } from '@neohiro/apis/registry';
// or
import { registry } from '@neohiro/apis';

// registry === { github: { ... }, tailscale: { ... }, ... }
```

**What changed:** The platform registry (auth methods, rate limits, env vars, docs links, health endpoints for all neohiro-integrated platforms) is now maintained in `neohiro/apis` as the single source of truth.

**What this means for transhumanists/apis consumers:**
- Python scripts that read `data/platform_registry.json` should instead call `@neohiro/apis/registry` directly (Node.js) or mirror the JSON from `neohiro/apis/src/registry.json`.
- This file will be removed in a future release of `transhumanists/apis`.
- No Python code in this repo was actually importing this file at the time of migration (the `_meta.usage` field was stale documentation).

**Why:** The registry describes neohiro infrastructure (Tailscale, GitHub, OpenAI, Groq, OpenRouter, Cerebras, HuggingFace, Cloudflare, YouTube, Facebook, Vercel, Render) — all of which are already implemented in `neohiro/apis`. Centralizing it there makes it the one authoritative source for both Node.js and Python consumers.
