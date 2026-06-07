# Security

`hermes-workflows` is an experimental durable workflow runtime. Treat it as alpha software until the public API and threat model are hardened.

## Generated workflow code

Dynamic workflows are Python source returned by an agent runner. Approval gates make generated code inspectable and auditable; they are **not** a sandbox.

Do not execute generated workflow source from an untrusted agent or user without a real sandbox boundary such as a locked-down process/container, scoped filesystem, network policy, and explicit capability allowlist.

Required safeguards for generated workflow execution:

- record generated source and SHA-256 before execution
- require a human or policy approval before execution
- preserve agent provenance and prompt/request hashes
- fail closed if approval is missing, rejected, or malformed
- separate code-execution approval from external side-effect approval

## External side effects

Workflow examples must default to dry-run/read-only behavior. Creating Gmail drafts, sending email, mutating Sheets/D1/R2, publishing sites, posting to social media, or changing repo visibility requires an explicit approval gate in the workflow and an explicit operator decision outside the workflow.

Draft approval is not send approval. Sending/scheduling should be a separate gate.

## Sensitive data

Do not commit real participant exports, workflow SQLite databases, generated receipts from real runs, public artifact share tokens, or local `.env` files.

The Hack the Valley real-run path uses local snapshots and private `/tmp/...` outputs. Public examples use synthetic data only.

The email triage live-fixture helper may read private Gmail search metadata, but it must only write bounded symbolic handles/signals to its fixture JSON. Do not commit generated live email fixtures or workflow DBs from private runs.

## Reporting issues

Before public launch, report issues directly to the repository owner. After the repo is public, replace this section with the preferred security contact and disclosure process.
