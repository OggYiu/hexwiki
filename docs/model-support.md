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
| xAI Grok models through a compatible endpoint | OpenAI-compatible | Expected by protocol; verify with preflight | Not yet demonstrated | No |
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
