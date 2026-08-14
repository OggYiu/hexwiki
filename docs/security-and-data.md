# Security and data handling

HexWiki processes material supplied by the operator. Confirm that you have the
right to extract it, send the selected scope to a model provider, retain local
copies, and distribute any resulting wiki.

## What leaves the machine

During networked preflight, smoke, and build, the configured provider receives
the requests needed for model discovery, chat, tool-use checks, generation, and
review. Scoped source text is included where the workflow needs evidence.

Langfuse receives observability payloads only when separately installed,
configured, and enabled. It is non-authoritative: initialization, callback, or
flush failures are contained and cannot alter retries, validation, output, or
exit status.

No network request is made by extraction, profile checking/locking, offline
preflight, lint, verify, query, or status unless a dependency outside HexWiki
has been independently configured to do so.

## Credentials and local records

Put provider credentials in the process environment. `hexwiki init` creates a
secret-free private config containing route, model, paths, limits, and an
observability switch; it does not store `HEXWIKI_API_KEY`. HexWiki does not
implicitly trust `.env` in the current directory. Known credential values are
redacted from local transcript serialization.

Extraction bundles, scoped native text, model/tool transcripts, audit logs,
review packets, failed candidates, and published wikis remain on local storage
until the operator removes them. Choose explicit private paths, restrict their
permissions, include them in backup/retention policy, and inspect them before
sharing.

## Source privacy

Repeating headers and footers may contain names, email addresses, purchase
identifiers, or distributor watermarks. Declare each edition-specific removal
as `scope.page_furniture` with a regular expression and a human-readable
reason. Removal happens before clipping and hashing, is recorded per page, and
fails if a declared rule matches nothing. Inspect the locked page map and
published wiki; a generic rule is not a substitute for edition-specific review.

Never report a source as verified merely because a quote is present. Quote
checking and deterministic gates are lower bounds on integrity, not proof that
the source is true or that the wiki’s selection is complete.
