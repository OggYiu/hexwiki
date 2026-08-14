# Security and privacy

## Reporting

Report vulnerabilities through a private channel maintained by the repository
owner. Do not place credentials, private routes, copyrighted documents, or
sensitive generated output in a public issue.

## Data boundary

HexWiki will send scoped source text to the model endpoint selected by the
operator. Optional observability is outbound-only and non-authoritative. Local
append-only records remain the source of truth.

Provider credentials belong only in process environment variables or a private
user configuration file. Installed builds must never trust a working-directory
configuration file implicitly.

