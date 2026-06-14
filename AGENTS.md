# AGENTS.md

## Pre-launch API discipline

Hermes Workflows is pre-launch. Do **not** preserve backwards compatibility for author-facing APIs unless Skylar explicitly asks for a compatibility window.

When a primitive, argument name, status field, dashboard surface, plugin tool, or docs example is replaced:

- remove the old public surface in the same PR;
- migrate tests, examples, docs, and workspace workflows that matter;
- do not keep aliases, shims, fallback handlers, or “legacy during cutover” paths;
- do not describe compatibility as safety — right now it is product confusion;
- search the repo before claiming the old surface is gone.

Private runtime plumbing may remain only when it is truly internal and not visible in authoring docs, examples, dashboard UI, plugin manifests, or normal status payloads.
