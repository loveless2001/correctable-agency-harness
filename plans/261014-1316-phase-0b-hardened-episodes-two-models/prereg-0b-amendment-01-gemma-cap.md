# Prereg 0b amendment 01: raise the Gemma budget cap

**Date:** 2026-10-14 16:13. The frozen prereg (96ca453) is unchanged; this file amends it.

- **Decision:** user, #agent 24059 ("just extend the cap tho?"), replying to my note that Gemma's full grid might not fit $20.
- **Change:** `--cap-usd 35` for `runs/pilot0b-gemma4` (was 20). Qwen stays at $20.
  - The planned total of $40 becomes a ≈$44 expected total, ≤ $55 worst case under the caps.
- **Basis (infra measurements only, no metric values):**
  - Gemma smoke: 59/60 episodes at 16:13, $11.11 including the ~$1.76 first boot.
  - 18.5 steps per episode, ~10.6 calls/min with 16 workers (Qwen: 15.8 steps, ~20 calls/min).
  - Remaining 120 episodes ≈ $0.156 each ≈ $19 more, so Gemma ≈ $31 in total.
- **Unchanged:** episodes, arms, scorer, sampling and limits. The pair-major order and the complete-pairs analysis rule still apply if the cap is reached.
