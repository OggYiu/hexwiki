# Model and quality support

Protocol compatibility and demonstrated behavioral quality are different
claims. HexWiki uses one exact OpenAI-compatible base URL and model ID per run.
Network preflight must prove model discovery, chat, and tool calling on that
binding before a smoke is meaningful.

| Route or model class | Protocol path | Offline integration | Live public-package smoke | Public semantic benchmark |
|---|---|---:|---:|---:|
| Synthetic in-process provider | Injected test boundary | Yes | Not applicable | No |
| OpenAI-compatible endpoint with exact model ID | Model listing and chat/tool calls | Yes, through the synthetic boundary | Not yet demonstrated | No |
| OpenAI API models | OpenAI-compatible | Expected by protocol; verify with preflight | Not yet demonstrated | No |
| xAI Grok through an operator-managed compatible endpoint | OpenAI-compatible | Yes | Yes — `grok-aiprogramming`, 2026-08-29 | Measured once: 86.41/100 with 14/14 hard gates; not accepted (`> 95` required) |
| Other compatible providers | OpenAI-compatible | Conditional on matching behavior | Not yet demonstrated | No |

“Expected by protocol” is not a quality endorsement. Providers may differ in
tool semantics, timeouts, output limits, and instruction following even when
their HTTP surface is compatible. A passing preflight proves only the probed
capabilities. A passing smoke proves the production-shaped path for one exact
binding. A passing build plus deterministic gates still does not establish
complete semantic quality without an appropriate independent reference.

When reporting a measured result, name the exact package commit and version,
model ID, route identity without leaking credentials, profile and source lock,
date, benchmark, gates, and limitations. Do not average scores from different
references or generalize from one book or model to all sources.

## The one measured result, in full

`hexwiki` 0.1.0 at runtime commit `87e12e4`, model `grok-aiprogramming` on an
operator-managed OpenAI-compatible endpoint, 2026-08-29, against a frozen
privately held reference bundle for one chapter of one book (25 PDF pages, 18
apparatus entries):

- **86.41 / 100**, **14/14 hard gates**, `accepted: false` — the bundle's own
  acceptance condition is `> 95.00`, so this run did not meet it.
- Semantic alignment 28.05/40; structure and retrieval 18.36/20; source fidelity
  and provenance 20/20; epistemic discipline 15/15; integrity 5/5.
- 18 of 43 reference units covered, 25 partial, **0 missing**. Retrieval 10/12.
- 615 model calls, zero provider errors, 64 stages; independent review closed
  with zero material findings and complete page coverage.
- Quotation verification: 362 substantive quotations checked, 91.16% supported
  by a page the note cites, 2 attributable to an uncited page.

The shortfall is concentrated in partial alignment rather than missing or wrong
content — no reference unit is absent and every deterministic dimension is
full. The recorded reasons are largely editorial: the run's motif matrix covers
32 episodes where the reference selects 12, and its argument map and rosters
organize the same material differently. Matching those would mean encoding one
reference wiki's selections into the compiler, which raises this number without
improving the tool, so it has not been done.

This is one chapter, one book, one route, one run. It is not evidence about
other sources, scopes, providers, or models. The reference is not a blinded
control, and the comparator's semantic half is itself a model judgment; its
deterministic half uses no model.
