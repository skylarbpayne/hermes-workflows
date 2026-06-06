from __future__ import annotations

import argparse
import html
import json
import keyword
import sys
from pathlib import Path
from typing import Any

from hermes_workflows import AgentStep, SubprocessAgentRunner, Workflow, WorkflowEngine, workflow
from hermes_workflows.engine import JsonCodec

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNNER_PATH = REPO_ROOT / "examples" / "runners" / "workflows_demo_agent.py"
DEFAULT_INPUTS = {
    "event": "Hack the Valley post-event participant emails",
    "goal": "Generate a personalized follow-up email draft for each participant using roster, project submission, and prize context.",
    "constraints": [
        "must find every participant before drafting",
        "must fetch each participant's submitted project and judging/prize result",
        "must demonstrate both agent approval and human approval gates",
        "must not send email or create Gmail drafts during the demo",
        "must render participant roster, project/prize lookup, generated drafts, approvals, and audit log in a web interface",
    ],
}

@workflow
async def workflows_meeting_demo(ctx, inputs):
    roster = await AgentStep(
        "participant_roster_agent",
        prompt="Find the hackathon participant roster that needs post-event follow-up emails.",
        variables={"event": inputs["event"], "constraints": inputs["constraints"]},
    )(ctx)

    projects = await AgentStep(
        "project_lookup_agent",
        prompt="Fetch project submission context for every participant in the roster.",
        variables={"participants": roster["participants"], "event": inputs["event"]},
    )(ctx)

    prizes = await AgentStep(
        "prize_lookup_agent",
        prompt="Fetch judging results and prize context for each participant project.",
        variables={"projects": projects["projects"], "event": inputs["event"]},
    )(ctx)

    draft_batch = await AgentStep(
        "participant_email_drafter_agent",
        prompt="Generate personalized post-hackathon email drafts using participant, project, and prize context. Do not send anything.",
        variables={
            "participants": roster["participants"],
            "projects": projects["projects"],
            "prizes": prizes["prizes"],
            "event": inputs["event"],
        },
    )(ctx)

    quality_review = await AgentStep(
        "email_quality_reviewer_agent",
        prompt="Review every generated participant email for factual accuracy, tone, missing prize context, and send-risk. Return an approval recommendation.",
        variables={"draft_batch": draft_batch, "projects": projects["projects"], "prizes": prizes["prizes"]},
    )(ctx)

    generated_workflow = await AgentStep(
        "workflow_architect_agent",
        prompt="Generate a Python @workflow that gates the reviewed participant email draft batch behind agent approval.",
        variables={"roster": roster, "projects": projects, "prizes": prizes, "draft_batch": draft_batch, "quality_review": quality_review, "constraints": inputs["constraints"]},
        returns=Workflow,
    )(ctx)

    email_packet = await generated_workflow(
        ctx,
        {
            "event": inputs["event"],
            "participants": roster["participants"],
            "projects": projects["projects"],
            "prizes": prizes["prizes"],
            "draft_batch": draft_batch,
            "quality_review": quality_review,
            "constraints": inputs["constraints"],
        },
        key="participant_email_demo",
    )

    human_decision = await ctx.approval.request(
        "Human approval: approve creating Gmail drafts for the participant email batch? No messages are sent in this demo.",
        key="human_email_batch_approval",
        artifact={
            "draft_batch": email_packet["draft_batch"],
            "quality_review": email_packet["quality_review"],
            "agent_decision": email_packet["agent_decision"],
            "source_data": {"roster": roster, "projects": projects, "prizes": prizes},
        },
        approver="human:skylar",
        allowed=["approve", "reject", "edit"],
        authority=["create_gmail_drafts_after_review"],
    )
    if human_decision.get("action") != "approve":
        return {
            "ready_to_create_drafts": False,
            "stage": "human_email_batch_rejected",
            "draft_batch": email_packet["draft_batch"],
            "quality_review": email_packet["quality_review"],
            "human_decision": human_decision,
            "side_effects": {"gmail_drafts_created": 0, "emails_sent": 0},
        }

    draft_creation_packet = await AgentStep(
        "draft_creation_packet_agent",
        prompt="Prepare the post-approval Gmail draft creation packet without creating drafts or sending email in the demo.",
        variables={"draft_batch": email_packet["draft_batch"], "quality_review": email_packet["quality_review"], "human_approval": human_decision},
    )(ctx)

    return {
        "ready_to_create_drafts": True,
        "side_effects": draft_creation_packet["side_effects"],
        "source_data": {"roster": roster, "projects": projects, "prizes": prizes},
        "generated_workflow": {
            "symbol": generated_workflow.symbol,
            "source_sha256": generated_workflow.source_sha256,
            "approval_key": generated_workflow.approval_key,
        },
        "draft_batch": email_packet["draft_batch"],
        "quality_review": email_packet["quality_review"],
        "agent_decision": email_packet["agent_decision"],
        "human_decision": human_decision,
        "draft_creation_packet": draft_creation_packet,
    }

def make_engine(db_path: Path) -> WorkflowEngine:
    return WorkflowEngine(
        db_path,
        agent_runner=SubprocessAgentRunner([sys.executable, str(RUNNER_PATH)], timeout_seconds=60),
    )


def run_full_demo(*, db_path: Path, workflow_id: str = "wf_workflows_demo_2026_06_05", artifact_path: Path | None = None) -> dict[str, Any]:
    db_path = Path(db_path)
    if db_path.exists():
        db_path.unlink()
    engine = make_engine(db_path)
    stage_results: list[dict[str, Any]] = []

    result = engine.run_until_idle(workflows_meeting_demo, DEFAULT_INPUTS, workflow_id=workflow_id)
    stage_results.append(_stage("started_waiting_on_generated_workflow_approval", result))
    generated_key = _latest_approval_key(engine, workflow_id)
    if not generated_key.startswith("generated-workflow:"):
        raise RuntimeError(f"expected generated workflow approval key, got {generated_key}")

    result = engine.signal(
        workflow_id,
        "approval.decision",
        key=generated_key,
        payload={"action": "approve", "by": "skylar", "note": "Approve generated participant-email workflow source before execution."},
        source=_human_source("demo-generated-workflow-approval"),
        idempotency_key="demo-generated-workflow-approval",
    )
    stage_results.append(_stage("approved_generated_workflow_and_started_child", result))

    child_id = _first_child_workflow_id(engine, workflow_id)
    result = engine.signal(
        child_id,
        "approval.decision",
        key="agent_email_quality_approval",
        payload={"action": "approve", "by": "agent:email_quality_reviewer", "note": "Agent QA approved factual accuracy, tone, prize mapping, and no-send guardrails."},
        source=_agent_source("email_quality_reviewer", "demo-agent-email-quality-approval"),
        idempotency_key="demo-agent-email-quality-approval",
    )
    stage_results.append(_stage("agent_approved_email_quality_review", result, workflow_id=child_id))

    result = engine.reconcile_children(workflow_id)
    stage_results.append(_stage("reconciled_child_and_waiting_on_human_email_batch_approval", result))

    result = engine.signal(
        workflow_id,
        "approval.decision",
        key="human_email_batch_approval",
        payload={"action": "approve", "by": "skylar", "note": "Human approved creating Gmail drafts from the reviewed batch; demo still performs zero side effects."},
        source=_human_source("demo-human-email-batch-approval"),
        idempotency_key="demo-human-email-batch-approval",
    )
    stage_results.append(_stage("human_approved_email_batch_and_completed", result))

    snapshot = build_snapshot(engine, workflow_id, stage_results=stage_results)
    if artifact_path is not None:
        render_snapshot_html(snapshot, artifact_path)
    return _receipt_from_snapshot(snapshot)

def render_demo_artifact(*, db_path: Path, workflow_id: str, artifact_path: Path) -> dict[str, Any]:
    engine = make_engine(Path(db_path))
    snapshot = build_snapshot(engine, workflow_id, stage_results=[])
    render_snapshot_html(snapshot, artifact_path)
    return snapshot


def build_snapshot(engine: WorkflowEngine, workflow_id: str, *, stage_results: list[dict[str, Any]]) -> dict[str, Any]:
    workflows = engine.list_workflows()
    ids = [
        row["workflow_id"]
        for row in workflows
        if row["workflow_id"] == workflow_id or row["workflow_id"].startswith(f"{workflow_id}.")
    ]
    if workflow_id not in ids:
        ids.insert(0, workflow_id)
    event_rows = []
    for wid in ids:
        try:
            for event in engine.events(wid):
                event_rows.append({"workflow_id": wid, **_jsonable_event(event)})
        except KeyError:
            continue
    status = engine.workflow_status(workflow_id, recent_events=50, command_history="all", command_limit=50, command_payload_chars=1200)
    agent_calls = _agent_calls(event_rows)
    generated = _generated_workflows(event_rows)
    approvals = _approvals(event_rows)
    outputs = _outputs(engine, ids)
    return {
        "workflow_id": workflow_id,
        "status": _json_roundtrip(status),
        "workflows": _json_roundtrip(workflows),
        "stages": stage_results,
        "agent_calls": agent_calls,
        "generated_workflows": generated,
        "approvals": approvals,
        "outputs": outputs,
        "audit_log": event_rows,
    }


def render_snapshot_html(snapshot: dict[str, Any], artifact_path: Path) -> None:
    artifact_path = Path(artifact_path)
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    data_json = json.dumps(snapshot, indent=2, sort_keys=True).replace("</", "<\\/")
    approval_flow_json = json.dumps(_approval_flow(snapshot), indent=2, sort_keys=True).replace("</", "<\\/")
    title = "Hackathon Participant Email Command Center"
    status = snapshot["status"].get("status", "unknown")
    parent_code = _code_block(
        "Parent workflow code",
        "This is the actual parent @workflow used for the demo run: roster/project/prize lookup, generated email workflow execution, agent QA approval, and human draft approval.",
        _parent_workflow_source(),
    )
    approval_lab = _approval_walkthrough(snapshot)
    approval_cards = "".join(_approval_card(item) for item in snapshot["approvals"])
    agent_cards = "".join(_agent_card(item) for item in snapshot["agent_calls"])
    generated_cards = "".join(_generated_card(item) for item in snapshot["generated_workflows"])
    output_cards = "".join(_output_card(item) for item in snapshot["outputs"])
    audit_rows = "".join(_audit_row(item) for item in snapshot["audit_log"])
    stage_rows = "".join(_stage_row(item) for item in snapshot["stages"])
    html_text = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{html.escape(title)}</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg:#07080d; --bg2:#0c1019; --panel:#101521; --panel2:#151c2b; --panel3:#1a2233;
      --text:#f6f8ff; --muted:#a5afc3; --dim:#68758e; --line:#273144;
      --good:#74e8a4; --wait:#ffd166; --accent:#8ab4ff; --pink:#ff8bd1; --purple:#b7a4ff; --danger:#ff7b7b;
      --shadow: 0 24px 80px rgba(0,0,0,.35);
    }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: radial-gradient(circle at 8% -8%, rgba(138,180,255,.22), transparent 34rem), radial-gradient(circle at 88% 2%, rgba(255,139,209,.14), transparent 28rem), linear-gradient(180deg,var(--bg),#090b12 46rem); color:var(--text); }}
    header {{ padding:34px clamp(18px,4vw,58px) 22px; border-bottom:1px solid rgba(255,255,255,.09); }}
    h1 {{ margin:0; font-size:clamp(38px,6vw,78px); letter-spacing:-0.065em; line-height:.9; max-width:1180px; }}
    h2 {{ margin:0 0 14px; font-size:21px; letter-spacing:-0.025em; }}
    h3 {{ margin:0 0 8px; font-size:15px; letter-spacing:-.01em; }}
    .sub {{ color:var(--muted); max-width:980px; line-height:1.55; font-size:17px; }}
    .statusbar {{ display:flex; gap:10px; flex-wrap:wrap; margin-top:18px; }}
    .pill {{ border:1px solid var(--line); background:rgba(255,255,255,.055); border-radius:999px; padding:8px 12px; color:var(--muted); backdrop-filter: blur(10px); }}
    .pill strong {{ color:var(--text); }}
    main {{ padding:24px clamp(18px,4vw,58px) 64px; display:grid; gap:20px; }}
    .grid {{ display:grid; grid-template-columns: repeat(12, minmax(0,1fr)); gap:18px; }}
    .card {{ grid-column: span 4; background:linear-gradient(180deg,rgba(21,28,43,.96),rgba(12,16,25,.98)); border:1px solid rgba(255,255,255,.10); border-radius:24px; padding:18px; box-shadow:var(--shadow); }}
    .wide {{ grid-column: span 8; }} .full {{ grid-column:1/-1; }}
    .label {{ color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.12em; }}
    .big {{ font-size:38px; font-weight:850; letter-spacing:-.05em; }}
    .good {{ color:var(--good); }} .wait {{ color:var(--wait); }} .accent {{ color:var(--accent); }} .pink {{ color:var(--pink); }} .purple {{ color:var(--purple); }}
    .mini {{ color:var(--muted); font-size:13px; line-height:1.45; }}
    .list {{ margin:10px 0 0 18px; padding:0; color:var(--muted); }}
    .list li {{ margin:6px 0; }}
    .section-title {{ display:flex; justify-content:space-between; gap:14px; align-items:end; margin-bottom:8px; }}
    .timeline {{ display:grid; gap:10px; }}
    .row {{ display:grid; grid-template-columns: 76px 1fr 170px; gap:12px; align-items:start; padding:10px 0; border-top:1px solid rgba(255,255,255,.08); }}
    .seq {{ color:var(--muted); font-variant-numeric:tabular-nums; }}
    .event {{ font-weight:750; }}
    .key {{ color:var(--muted); overflow-wrap:anywhere; font-size:12px; }}
    details summary {{ cursor:pointer; color:var(--accent); margin-top:10px; }}

    .approval-lab {{ position:relative; overflow:hidden; }}
    .approval-lab::before {{ content:""; position:absolute; inset:0; pointer-events:none; background:radial-gradient(circle at 78% 10%, rgba(116,232,164,.13), transparent 22rem); }}
    .approval-head {{ display:grid; grid-template-columns: minmax(0,1fr) auto; gap:18px; align-items:start; position:relative; }}
    .approval-progress {{ display:flex; gap:8px; flex-wrap:wrap; margin:18px 0; position:relative; }}
    .approval-dot {{ border:1px solid var(--line); color:var(--muted); background:rgba(255,255,255,.04); padding:8px 10px; border-radius:999px; font-size:12px; }}
    .approval-dot.current {{ border-color:var(--wait); color:var(--wait); }}
    .approval-dot.done {{ border-color:rgba(116,232,164,.65); color:var(--good); background:rgba(116,232,164,.09); }}
    .approval-stack {{ display:grid; grid-template-columns: repeat(3, minmax(0,1fr)); gap:14px; position:relative; }}
    .approval-step {{ border:1px solid var(--line); background:rgba(8,11,17,.66); border-radius:18px; padding:14px; opacity:.55; transition:opacity .2s, transform .2s, border-color .2s; }}
    .approval-step.current {{ opacity:1; border-color:rgba(255,209,102,.76); transform:translateY(-2px); }}
    .approval-step.done {{ opacity:1; border-color:rgba(116,232,164,.56); }}
    button {{ appearance:none; border:1px solid rgba(138,180,255,.62); background:linear-gradient(180deg,rgba(138,180,255,.20),rgba(138,180,255,.10)); color:var(--text); border-radius:12px; padding:10px 12px; font-weight:750; cursor:pointer; }}
    button:hover {{ border-color:rgba(138,180,255,.9); }}
    button:disabled {{ opacity:.42; cursor:not-allowed; }}
    .reset {{ border-color:var(--line); background:rgba(255,255,255,.05); color:var(--muted); }}

    .code-panel {{ margin-top:12px; border:1px solid var(--line); background:#070a0f; border-radius:16px; overflow:hidden; }}
    .code-head {{ display:flex; justify-content:space-between; gap:12px; padding:12px 14px; border-bottom:1px solid rgba(255,255,255,.08); background:rgba(255,255,255,.035); }}
    .code-title {{ font-weight:800; }}
    .code-subtitle {{ color:var(--muted); font-size:12px; }}
    .code-block {{ margin:0; overflow:auto; max-height:680px; font:12px/1.55 "JetBrains Mono", "SFMono-Regular", Consolas, monospace; }}
    .code-line {{ display:grid; grid-template-columns: 52px minmax(max-content, 1fr); min-width:100%; }}
    .code-line:hover {{ background:rgba(138,180,255,.06); }}
    .ln {{ color:#526079; text-align:right; padding:0 12px 0 8px; user-select:none; border-right:1px solid rgba(255,255,255,.06); }}
    .code-line code {{ padding:0 14px; white-space:pre; color:#dbe7ff; }}
    .tok.kw {{ color:#ff8bd1; font-weight:750; }}
    .tok.comment {{ color:#71809b; font-style:italic; }}
    .tok.decorator {{ color:#74e8a4; }}
    pre.json {{ overflow:auto; background:#070a0f; border:1px solid var(--line); border-radius:14px; padding:14px; color:#dbe7ff; font-size:12px; line-height:1.5; max-height:420px; }}
    @media (max-width: 1050px) {{ .card,.wide {{ grid-column:1/-1; }} .approval-stack {{ grid-template-columns:1fr; }} .row {{ grid-template-columns: 52px 1fr; }} .row .key {{ grid-column:2; }} .approval-head {{ grid-template-columns:1fr; }} }}
  </style>
</head>
<body>
<header>
  <div class="label">Hermes /workflows local demo · no-send hackathon ops</div>
  <h1>Personalized hackathon emails, with agents and approvals doing real work.</h1>
  <p class="sub">A useful Hack the Valley workflow: find the participant roster, fetch project submissions, check prize results, generate personalized email drafts, require Agent approval, then require Human approval before any Gmail draft step. Zero emails sent in the demo.</p>
  <div class="statusbar">
    <span class="pill"><strong>Workflow</strong> {html.escape(snapshot['workflow_id'])}</span>
    <span class="pill"><strong>Status</strong> <span class="good">{html.escape(str(status))}</span></span>
    <span class="pill"><strong>Agent calls</strong> {len(snapshot['agent_calls'])}</span>
    <span class="pill"><strong>Approval gates</strong> {len(snapshot['approvals'])}</span>
    <span class="pill"><strong>Audit events</strong> {len(snapshot['audit_log'])}</span>
  </div>
</header>
<main>
  <section class="grid">
    <article class="card"><div class="label">1 / participant data</div><div class="big accent">4</div><p class="mini">Participant roster is read from a local fixture, filtered to opted-in recipients, and never mutated.</p></article>
    <article class="card"><div class="label">2 / project + prize lookup</div><div class="big pink">2</div><p class="mini">Project submissions and winner/prize records enrich the drafts without touching real systems.</p></article>
    <article class="card"><div class="label">3 / approval gates</div><div class="big wait">{len(snapshot['approvals'])}</div><p class="mini">Generated-code execution, Agent approval, and Human approval are all visible receipts.</p></article>
  </section>

  {approval_lab}

  <section class="grid">
    <article class="card wide"><div class="section-title"><h2>Stage walkthrough</h2><span class="mini">actual persisted run stages</span></div><div class="timeline">{stage_rows}</div></article>
    <article class="card"><h2>Demo script</h2><ol class="list"><li>Start at the approval walkthrough and hit Reset.</li><li>Approve generated workflow code only after showing the Python source and hash.</li><li>Show the child workflow fetching Participant roster, Project submissions, and Project + prize lookup data from local fixtures.</li><li>Approve the Agent approval gate from email_quality_reviewer.</li><li>Approve the Human approval gate as Skylar; point out side effects stayed at zero.</li><li>Then jump to personalized email drafts, audit log, and raw JSON receipts.</li></ol></article>
  </section>

  <section class="grid">
    <article class="card full">{parent_code}</article>
    <article class="card full"><h2>Generated workflow code</h2>{generated_cards}</article>
    <article class="card full"><h2>Agent calls</h2><div class="grid">{agent_cards}</div></article>
    <article class="card full"><h2>Approval receipts</h2><div class="grid">{approval_cards}</div></article>
    <article class="card full"><h2>Generated artifacts / outputs</h2>{output_cards}</article>
    <article class="card full"><h2>Audit log</h2><div class="timeline">{audit_rows}</div></article>
  </section>
</main>
<script type="application/json" id="workflow-demo-data">{data_json}</script>
<script type="application/json" id="approval-flow-data">{approval_flow_json}</script>
<script>
(function() {{
  const flow = JSON.parse(document.getElementById('approval-flow-data').textContent);
  const key = 'workflows-demo-approval-step:' + {json.dumps(snapshot['workflow_id'])};
  let approved = Math.min(Number(localStorage.getItem(key) || 0), flow.length);
  const dots = Array.from(document.querySelectorAll('[data-approval-dot]'));
  const cards = Array.from(document.querySelectorAll('[data-approval-card]'));
  const state = document.querySelector('[data-approval-state]');
  function render() {{
    dots.forEach((dot, index) => {{ dot.classList.toggle('done', index < approved); dot.classList.toggle('current', index === approved); }});
    cards.forEach((card, index) => {{ card.classList.toggle('done', index < approved); card.classList.toggle('current', index === approved); }});
    document.querySelectorAll('[data-approve-index]').forEach((button) => {{
      const index = Number(button.dataset.approveIndex);
      button.disabled = index !== approved;
      button.textContent = index < approved ? 'Approved' : (index === approved ? 'Approve this gate' : 'Waiting on previous gate');
    }});
    if (state) {{ state.textContent = approved >= flow.length ? 'All approvals cleared — final output can exist.' : 'Waiting on: ' + flow[approved].label; }}
  }}
  document.querySelectorAll('[data-approve-index]').forEach((button) => button.addEventListener('click', () => {{
    const index = Number(button.dataset.approveIndex);
    if (index === approved) {{ approved += 1; localStorage.setItem(key, String(approved)); render(); }}
  }}));
  const reset = document.querySelector('[data-reset-approvals]');
  if (reset) reset.addEventListener('click', () => {{ approved = 0; localStorage.setItem(key, '0'); render(); }});
  render();
}})();
</script>
</body>
</html>
"""
    artifact_path.write_text(html_text, encoding="utf-8")
    json.loads(_extract_script_json(html_text))


def _approval_flow(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "label": approval.get("label"),
            "key": approval.get("key"),
            "workflow_id": approval.get("workflow_id"),
            "prompt": approval.get("prompt"),
            "authority": approval.get("authority"),
            "allowed": approval.get("allowed"),
            "decision": (approval.get("decision") or {}).get("payload"),
        }
        for approval in snapshot.get("approvals", [])
    ]


def _approval_walkthrough(snapshot: dict[str, Any]) -> str:
    cards = []
    dots = []
    for index, approval in enumerate(snapshot.get("approvals", [])):
        label = approval.get("label") or f"approval_{index + 1}"
        dots.append(f'<span class="approval-dot" data-approval-dot="{index}">{index + 1}. {html.escape(label)}</span>')
        signal = _approval_signal_snippet(approval)
        cards.append(
            f"""
            <article class="approval-step" data-approval-card="{index}">
              <div class="label">Gate {index + 1}</div>
              <h3>{html.escape(label)}</h3>
              <p class="mini">{html.escape(approval.get('prompt') or '')}</p>
              <p class="mini"><strong>Approver:</strong> {html.escape(str(approval.get('approver') or 'unspecified'))}</p><p class="mini"><strong>Authority:</strong> {html.escape(', '.join(approval.get('authority') or []))}</p>
              <button data-approve-index="{index}">Approve this gate</button>
              {_code_block('Approval signal', 'The exact workflow signal this approval represents.', signal)}
            </article>
            """
        )
    return f"""
  <section class="card full approval-lab" id="approval-walkthrough">
    <div class="approval-head">
      <div>
        <div class="label">Agent + human approval rehearsal</div>
        <h2>Interactive approval walkthrough</h2>
        <p class="mini">Use this in the room: reset, inspect the code/artifact for each gate, then approve each step as Skylar. The underlying run receipts below are from the actual workflow engine signals.</p>
      </div>
      <div>
        <button class="reset" data-reset-approvals>Reset approvals</button>
        <p class="mini" data-approval-state></p>
      </div>
    </div>
    <div class="approval-progress">{''.join(dots)}</div>
    <div class="approval-stack">{''.join(cards)}</div>
  </section>
    """


def _approval_signal_snippet(approval: dict[str, Any]) -> str:
    decision = (approval.get("decision") or {}).get("payload") or {"action": "approve", "by": "skylar", "note": "Approved in live demo after inspecting the displayed artifact/code."}
    source = (approval.get("decision") or {}).get("source") or {"kind": "human", "id": "skylar", "channel": "demo-room", "message_id": "live-demo-approval"}
    workflow_id = approval.get("workflow_id") or "<workflow_id>"
    key = approval.get("key") or "<approval_key>"
    return "\n".join([
        f'engine.signal(',
        f'    {workflow_id!r},',
        f'    "approval.decision",',
        f'    key={key!r},',
        f'    payload={decision!r},',
        f'    source={source!r},',
        f')',
    ])


def _parent_workflow_source() -> str:
    lines = Path(__file__).read_text(encoding="utf-8").splitlines()
    start = None
    for index, line in enumerate(lines):
        if line == "@workflow" and index + 1 < len(lines) and "workflows_meeting_demo" in lines[index + 1]:
            start = index
            break
    if start is None:
        return "# parent workflow source not found"
    end = start + 1
    while end < len(lines) and not lines[end].startswith("def make_engine"):
        end += 1
    return "\n".join(lines[start:end]).rstrip()


def _highlight_python_line(line: str) -> str:
    if not line:
        return ""
    if line.lstrip().startswith("@"):  # decorators are the important visual cue in this demo
        return f'<span class="tok decorator">{html.escape(line, quote=False)}</span>'

    pieces: list[str] = []
    index = 0
    while index < len(line):
        char = line[index]
        if char == "#":
            pieces.append(f'<span class="tok comment">{html.escape(line[index:], quote=False)}</span>')
            break
        if char in {'"', "'"}:
            quote = char
            end = index + 1
            escaped_char = False
            while end < len(line):
                current = line[end]
                if current == quote and not escaped_char:
                    end += 1
                    break
                escaped_char = current == "\\" and not escaped_char
                if current != "\\":
                    escaped_char = False
                end += 1
            pieces.append(f'<span class="tok string">{html.escape(line[index:end], quote=False)}</span>')
            index = end
            continue
        if char == "_" or char.isalpha():
            end = index + 1
            while end < len(line) and (line[end] == "_" or line[end].isalnum()):
                end += 1
            word = line[index:end]
            text = html.escape(word, quote=False)
            if keyword.iskeyword(word):
                pieces.append(f'<span class="tok kw">{text}</span>')
            else:
                pieces.append(text)
            index = end
            continue
        if char.isdigit():
            end = index + 1
            while end < len(line) and (line[end].isalnum() or line[end] in "._"):
                end += 1
            pieces.append(f'<span class="tok number">{html.escape(line[index:end], quote=False)}</span>')
            index = end
            continue
        pieces.append(html.escape(char, quote=False))
        index += 1
    return "".join(pieces)


def _code_block(title: str, subtitle: str, source: str) -> str:
    lines = source.strip("\n").splitlines() or [""]
    rows = []
    for number, line in enumerate(lines, 1):
        rows.append(
            f'<div class="code-line"><span class="ln">{number}</span><code>{_highlight_python_line(line) or " "}</code></div>'
        )
    return f"""
    <div class="code-panel">
      <div class="code-head"><div><div class="code-title">{html.escape(title)}</div><div class="code-subtitle">{html.escape(subtitle)}</div></div><div class="label">Python</div></div>
      <pre class="code-block language-python">{''.join(rows)}</pre>
    </div>
    """

def _receipt_from_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    generated = snapshot["generated_workflows"][0] if snapshot["generated_workflows"] else {}
    final = snapshot["status"]
    return {
        "workflow_id": snapshot["workflow_id"],
        "final_result": final,
        "agent_calls": len(snapshot["agent_calls"]),
        "generated_workflow": generated,
        "approvals": [approval["label"] for approval in snapshot["approvals"]],
        "event_count": len(snapshot["audit_log"]),
    }


def _stage(label: str, result: Any, *, workflow_id: str | None = None) -> dict[str, Any]:
    return {
        "label": label,
        "workflow_id": workflow_id or result.workflow_id,
        "status": result.status,
        "waiting_on": result.waiting_on,
        "error": result.error,
    }


def _human_source(message_id: str) -> dict[str, Any]:
    return {"kind": "human", "id": "skylar", "channel": "demo-room", "message_id": message_id}


def _agent_source(agent_id: str, message_id: str) -> dict[str, Any]:
    return {"kind": "agent", "id": agent_id, "channel": "workflow-runtime", "message_id": message_id}


def _latest_approval_key(engine: WorkflowEngine, workflow_id: str) -> str:
    approvals = [event for event in engine.events(workflow_id) if event["type"] == "ApprovalRequested"]
    if not approvals:
        raise RuntimeError(f"no approval requested for {workflow_id}")
    return approvals[-1]["payload"]["key"]


def _first_child_workflow_id(engine: WorkflowEngine, workflow_id: str) -> str:
    for event in engine.events(workflow_id):
        if event["type"] == "ChildWorkflowRequested":
            return event["payload"]["child_workflow_id"]
    raise RuntimeError("no child workflow requested")


def _json_roundtrip(value: Any) -> Any:
    return json.loads(JsonCodec.dumps(value))


def _jsonable_event(event: dict[str, Any]) -> dict[str, Any]:
    return _json_roundtrip(event)


def _agent_calls(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    calls = []
    for event in events:
        if event["type"] != "StepCompleted":
            continue
        metadata = (event.get("payload") or {}).get("metadata") or {}
        if metadata.get("kind") != "agent_step.live_result.v1":
            continue
        request = metadata.get("request") or {}
        response = metadata.get("response") or {}
        calls.append({
            "workflow_id": event["workflow_id"],
            "seq": event["seq"],
            "name": request.get("name"),
            "returns": request.get("returns"),
            "prompt": request.get("rendered_prompt"),
            "provenance": response.get("provenance") or metadata.get("provenance"),
            "output": response.get("output"),
        })
    return calls


def _generated_workflows(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    generated = []
    for call in _agent_calls(events):
        output = call.get("output") or {}
        if isinstance(output, dict) and "source" in output and "symbol" in output:
            source = output["source"]
            generated.append({
                "label": "generated_workflow_execution",
                "symbol": output["symbol"],
                "source_sha256": __import__("hashlib").sha256(source.encode("utf-8")).hexdigest(),
                "source": source,
                "agent_call": call["name"],
                "provenance": call["provenance"],
            })
    return generated


def _approval_label(key: str) -> str:
    if key.startswith("generated-workflow:"):
        return "generated_workflow_execution"
    return key


def _approvals(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    decisions: dict[str, dict[str, Any]] = {}
    for event in events:
        if event["type"] == "SignalReceived" and (event.get("payload") or {}).get("signal_type") == "approval.decision":
            payload = event["payload"]
            decisions[payload["key"]] = {"payload": payload.get("payload"), "source": payload.get("source"), "workflow_id": event["workflow_id"], "seq": event["seq"]}

    requested = []
    for event in events:
        if event["type"] != "ApprovalRequested":
            continue
        payload = event["payload"]
        key = payload["key"]
        requested.append({
            "workflow_id": event["workflow_id"],
            "seq": event["seq"],
            "created_at": event.get("created_at", 0),
            "label": _approval_label(key),
            "key": key,
            "prompt": payload.get("prompt"),
            "authority": payload.get("authority"),
            "allowed": payload.get("allowed"),
            "approver": payload.get("approver"),
            "artifact": payload.get("artifact"),
            "decision": decisions.get(key),
        })
    order = {
        "generated_workflow_execution": 0,
        "agent_email_quality_approval": 1,
        "human_email_batch_approval": 2,
    }
    return sorted(requested, key=lambda item: (order.get(str(item.get("label") or ""), 99), item.get("created_at", 0), item.get("seq", 0)))


def _outputs(engine: WorkflowEngine, ids: list[str]) -> list[dict[str, Any]]:
    outputs = []
    for wid in ids:
        try:
            status = engine.workflow_status(wid, recent_events=5)
        except KeyError:
            continue
        if status.get("result") is not None:
            outputs.append({"workflow_id": wid, "result": _json_roundtrip(status["result"])})
    return outputs


def _approval_card(item: dict[str, Any]) -> str:
    decision = item.get("decision") or {}
    decision_payload = decision.get("payload") or {}
    source = decision.get("source") or {}
    body = {
        "key": item.get("key"),
        "authority": item.get("authority"),
        "approver": item.get("approver"),
        "decision": decision_payload,
        "source": source,
    }
    return f"<article class='card'><div class='label'>{html.escape(item.get('workflow_id',''))}</div><h3>{html.escape(item.get('label','approval'))}</h3><p class='mini'>{html.escape(item.get('prompt',''))}</p><pre class='json'>{html.escape(json.dumps(body, indent=2, sort_keys=True))}</pre></article>"


def _agent_card(item: dict[str, Any]) -> str:
    body = {"provenance": item.get("provenance"), "output": item.get("output")}
    return f"<article class='card'><div class='label'>Agent calls</div><h3>{html.escape(str(item.get('name')))}</h3><p class='mini'>{html.escape(str(item.get('prompt') or ''))}</p><pre class='json'>{html.escape(json.dumps(body, indent=2, sort_keys=True))}</pre></article>"


def _generated_card(item: dict[str, Any]) -> str:
    source = item.get("source", "")
    meta = f"symbol: {item.get('symbol','')} · sha256: {item.get('source_sha256','')} · generated by: {item.get('agent_call','')}"
    return _code_block("Generated workflow: " + str(item.get("symbol", "")), meta, source)


def _output_card(item: dict[str, Any]) -> str:
    return f"<h3>{html.escape(item.get('workflow_id',''))}</h3><pre class='json'>{html.escape(json.dumps(item.get('result'), indent=2, sort_keys=True))}</pre>"


def _audit_row(item: dict[str, Any]) -> str:
    payload = item.get("payload")
    preview = json.dumps(payload, sort_keys=True)
    if len(preview) > 260:
        preview = preview[:260] + "…"
    return f"<div class='row'><div class='seq'>#{item.get('seq')}</div><div><div class='event'>{html.escape(item.get('type',''))}</div><div class='mini'>{html.escape(preview)}</div></div><div class='key'>{html.escape(item.get('workflow_id',''))}<br>{html.escape(item.get('key',''))}</div></div>"


def _stage_row(item: dict[str, Any]) -> str:
    return f"<div class='row'><div class='seq'>{html.escape(str(item.get('status')))}</div><div><div class='event'>{html.escape(item.get('label',''))}</div><div class='mini'>workflow: {html.escape(item.get('workflow_id',''))}</div></div><div class='key'>{html.escape(str(item.get('waiting_on') or 'not waiting'))}</div></div>"

def _extract_script_json(html_text: str) -> str:
    start = html_text.index('<script type="application/json" id="workflow-demo-data">') + len('<script type="application/json" id="workflow-demo-data">')
    end = html_text.index("</script>", start)
    return html_text[start:end]


def _summary_from_receipt(receipt: dict[str, Any], artifact_path: Path) -> dict[str, Any]:
    final_result = receipt.get("final_result") or {}
    result = final_result.get("result") or {}
    draft_batch = result.get("draft_batch") or {}
    generated_workflow = receipt.get("generated_workflow") or {}
    return {
        "artifact_path": str(artifact_path),
        "status": final_result.get("status"),
        "agent_calls": receipt.get("agent_calls"),
        "approvals": receipt.get("approvals"),
        "event_count": receipt.get("event_count"),
        "draft_count": draft_batch.get("draft_count") or len(draft_batch.get("drafts") or []),
        "side_effects": result.get("side_effects"),
        "generated_workflow": {
            "symbol": generated_workflow.get("symbol"),
            "source_sha256": generated_workflow.get("source_sha256"),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the June 5 /workflows demo and render the command-center artifact.")
    parser.add_argument("--db", type=Path, default=Path("/tmp/workflows-demo-2026-06-05.sqlite"))
    parser.add_argument("--id", default="wf_workflows_demo_2026_06_05", dest="workflow_id")
    parser.add_argument("--artifact", type=Path, default=REPO_ROOT / "dist" / "workflows-demo-2026-06-05" / "index.html")
    parser.add_argument("--render-only", action="store_true")
    parser.add_argument("--receipt-json", type=Path, help="Write the full receipt to a private JSON file instead of printing it.")
    parser.add_argument("--full-receipt", action="store_true", help="Print the full receipt. Do not use with real participant data.")
    args = parser.parse_args(argv)

    if args.render_only:
        snapshot = render_demo_artifact(db_path=args.db, workflow_id=args.workflow_id, artifact_path=args.artifact)
        print(json.dumps({"artifact_path": str(args.artifact), "status": snapshot["status"].get("status")}, sort_keys=True))
    else:
        receipt = run_full_demo(db_path=args.db, workflow_id=args.workflow_id, artifact_path=args.artifact)
        receipt["artifact_path"] = str(args.artifact)
        if args.receipt_json is not None:
            args.receipt_json.parent.mkdir(parents=True, exist_ok=True)
            args.receipt_json.write_text(json.dumps(receipt, indent=2, sort_keys=True), encoding="utf-8")
        if args.full_receipt:
            print(json.dumps(receipt, indent=2, sort_keys=True))
        else:
            print(json.dumps(_summary_from_receipt(receipt, args.artifact), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
