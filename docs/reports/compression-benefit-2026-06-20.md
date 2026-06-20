# Compression benefit report — code skeletons (2026-06-20)

> Our own "Headroom-style" compression: shrink code to its **skeleton** (signatures, types,
> docstrings; bodies elided) before it enters the model's context. Deterministic, reversible
> (the exact original is always one `read_full` away). ADR-0026. Implemented in
> `scripts/codeskeleton.py`, wired into `scripts/kb_agent.py`.

## The headline (deterministic — reproducible, model-independent)

Ran the compressor over **every source file in `services/mcp-server/src` (80 files)** and measured
raw tokens vs skeleton tokens. This is a **pure function over real files** — no LLM, no flakiness,
fully reproducible:

| | tokens |
|---|---|
| Raw source | 80,902 |
| Skeleton | 47,483 |
| **Saved** | **33,419 (41% overall)** |

And on the **big files an agent actually reads to orient**, the cut is far larger:

| file | saved |
|---|---|
| verify.py | −4,470 (62%) |
| change_context.py | −4,197 (73%) |
| expand.py | −2,204 (73%) |
| pack.py | −2,037 (78%) |
| request_more.py | −1,736 (76%) |
| graph.py | −1,628 (76%) |

Reproduce:
```bash
cd services/mcp-server && uv run python -c "import sys; sys.path.insert(0,'../../scripts'); import codeskeleton, pathlib; \
fs=sorted(pathlib.Path('src/agentic_mcp_server').rglob('*.py')); \
r=[codeskeleton.skeletonize(f.read_text(),filename=f.name) for f in fs]; \
print(sum(x.original_tokens for x in r), '->', sum(x.skeleton_tokens for x in r))"
```

## What the skeleton looks like (real output, `budgets.py`)

The compressor keeps everything the model needs to *orient and write code that fits* — imports,
decorators, class headers, signatures, type hints, the first docstring line — and elides bodies:

```python
@dataclass(frozen=True)
class BudgetPolicy:
    allowances: Mapping[str, AgentAllowance] = field(default_factory=dict)
    default_allowance: AgentAllowance = DEFAULT_AGENT_ALLOWANCE

    def allowance_for(self, subject: str) -> AgentAllowance:
        ... # 1 line elided

def parse_agent_allowances(raw: str | None) -> dict[str, AgentAllowance]:
    """Parse the MCP_AGENT_ALLOWANCES deployment value.
    ... (docstring continues) ..."""
    ... # 29 lines elided
```

The skeleton is **still valid Python** (the elision is a comment), so it never confuses the model.

## End-to-end integration (one clean run)

`kb_agent.py` now returns code as a skeleton by default (`read_file`), with `read_full` for the
exact body the model edits or quotes. One clean end-to-end run (Groq `llama-3.3-70b-versatile`,
task: *"read budgets.py and explain how the per-agent token budget is enforced"*):

| | result |
|---|---|
| Answer | ✅ correct, cited `budgets.py` |
| Tokens | in=1,984 out=166 **total=2,150** |
| Compression | 1 skeleton, **426 input tokens saved** on the one read |

The integration works: the model read the **skeleton**, understood the structure, and answered
correctly without ever needing the full bodies.

## Honest caveats

- **End-to-end A/B on Groq-Llama is too noisy to publish.** Both `llama-3.1-8b-instant` and
  `llama-3.3-70b-versatile` are **unreliable at tool-calling** — they frequently emit malformed
  function-call syntax that Groq's API rejects with a 400, on the *first* model response (before
  compression is even in play). The compression-OFF arm crashed on 4 of 5 attempts for this reason,
  which is a **model-quality** problem, not a compression problem. So the trustworthy number is the
  deterministic corpus measurement above, not a single lucky LLM run. Re-run the end-to-end A/B on a
  reliable tool-calling model (Claude / GPT-4-class) to get a clean paired number.
- **41% is the whole-corpus average**; it skews higher (60–80%) exactly on the large files that
  dominate an agent's reading, and lower on tiny files (little to elide). The *effective* saving in
  a real session is weighted toward the big reads, so it trends above 41%.
- **Compression is lossy by design** — bodies are dropped. It is for *orientation/thinking*, never
  for an exact quote. The exact text is always one `read_full` away (reversible), so citations stay
  exact. ([[verification-receipt]] / L0 quote-grounding is unaffected.)
- The non-Python path is a **line heuristic** (kept signatures/decls, collapsed indented bodies) —
  good enough for JS/Go/Rust skeletons, not as precise as the Python AST path.

## Bottom line

Compression is **real and measurable**: ~**41% fewer tokens** to read the codebase overall, **60–80%
on the files that matter**, deterministically and reproducibly, with the exact original always
recoverable. This is the lever that makes "let the model read freely" *affordable* — the model is
never blocked or gated; everything it reads is just smaller. It pairs with KB grounding (which makes
the answer *correct*): **grounding finds the right place, compression makes reading it cheap.**
