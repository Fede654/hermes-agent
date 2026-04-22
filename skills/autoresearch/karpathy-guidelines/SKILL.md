---
name: karpathy-guidelines
description: >
  Behavioral guidelines to reduce common LLM coding mistakes in research
  experiments. Use when writing, reviewing, or iterating on experiment code
  to avoid overcomplication, make surgical changes, surface assumptions, and
  define verifiable metric-based success criteria. Auto-applied inside the
  Hermes AutoResearch loop. Triggers on: "simplify", "refactor experiment",
  "why isn't the metric improving", "code review", or any iteration step.
metadata:
  author: hermes
  source: https://x.com/karpathy/status/2015883857489522876
  category: experiment
  priority: "1"
---

# Karpathy Research Guidelines

Behavioral guidelines for LLM-driven experiment code, derived from Andrej
Karpathy's observations on common LLM coding pitfalls. Applied to every
iteration of the Hermes AutoResearch Karpathy loop.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial
one-shot experiments, use judgment.

---

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before writing or modifying any experiment code:

- State your **assumptions** explicitly: what do you expect `main.py` to do?
  What is the binding bottleneck causing the metric to be where it is?
- If multiple interpretations exist, present them — don't pick silently.
- If a simpler approach exists, say so. Prefer it.
- If something is unclear, name what is confusing in your NOTES output.
  Do NOT guess silently and move on.

In the research loop, this means: before touching `main.py`, write a brief
mental model of why the last metric was what it was. If you can't explain
it, don't change it yet.

## 2. Simplicity First

**Minimum code that moves the metric. Nothing speculative.**

- No features beyond what the hypothesis requires.
- No abstractions for single-use experiment code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, write 50.

Ask yourself: "Would a senior engineer say this is overcomplicated?"
If yes, simplify. In experiment code, complexity is an enemy — it hides
the signal you're trying to measure.

## 3. Surgical Changes

**Touch only what your hypothesis requires. Clean up only your own mess.**

When iterating on experiment code:

- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code or issues, mention them in NOTES —
  don't fix them silently.

When your changes create orphans:

- Remove imports/variables that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

**The test:** Every changed line should trace directly to the hypothesis
that motivated this iteration.

## 4. Goal-Driven Execution

**Define success in metric terms. Loop until verified.**

Transform vague improvement goals into verifiable metric movements:

- "Make it faster" → "`accuracy` should increase from 0.72 toward 0.80"
- "Fix convergence" → "`loss` should decrease by at least 10% vs. baseline"
- "Try a different optimizer" → "Adam should yield higher `accuracy` than SGD at iter N"

For each iteration, state a brief plan before coding:

```
Hypothesis: [what I think will improve the metric and why]
Change: [the ONE thing I will modify]
Verify: [metric moves from X toward Y]
```

Strong success criteria let the loop self-correct. Weak criteria
("make it better") waste iterations and lose signal.

---

## Application to Hermes AutoResearch Loop

| Loop Step | Karpathy Principle |
|-----------|-------------------|
| Step 0: Think | Principle 1 — state assumptions + bottleneck before any code |
| Step 1: Run | Principle 3 — touch only what the hypothesis requires |
| Step 2: Measure | Principle 4 — compare against your stated success criterion |
| Improve iteration | All 4 — think → minimal change → verify → repeat |
| Early stop (3 non-improving) | Principle 1 — re-examine assumptions before giving up |

If 3 consecutive iterations fail to improve the metric, stop and ask:
"Is my mental model of the bottleneck correct?" before iterating further.
This is the Karpathy diagnostic: the loop failing usually means the
hypothesis was wrong, not that you need more iterations.
