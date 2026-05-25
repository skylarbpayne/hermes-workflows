# Implementation plan: {{goal}}

## Goal
{{goal}}

## Non-goals
{{non_goals}}

## Current baseline / state
- Repo: `{{repo_path}}`
- Workflow id: `{{workflow_id}}`
- This plan must be approved before implementation work starts.

## Proposed file/module changes
{{proposed_changes}}

## API / schema / event changes
{{api_or_event_changes}}

## Execution sequence
1. Create this plan artifact.
2. Wait for explicit human approval of this plan.
3. Implement with TDD only after approval.
4. Open/update PR and produce landing evidence.
5. Wait for separate merge approval.

## Approval gates
- Plan approval: `approve_implementation_plan`, approver `human:skylar`, before implementation.
- Landing approval: `approve_pr_landing`, approver `human:skylar`, before merge/landing.

## Tests / verification
{{verification_commands}}

## Side effects
- After approval only: code changes, commit, branch push, PR creation/update, GitHub check watching, Kanban evidence.
- No merge/deploy without a separate landing approval.

## Risks / rollback
- Risk: plan approval is confused with merge approval. Mitigation: separate keys and report sections.
- Risk: missing provenance. Mitigation: require human source plus channel/message provenance.
- Rollback: stop before implementation, or close/supersede the PR if the approved slice proves wrong.

## Open questions / decision points
{{open_questions}}
