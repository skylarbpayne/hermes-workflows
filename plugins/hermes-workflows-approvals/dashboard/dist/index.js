(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) {
    console.warn("Hermes Workflows dashboard plugin: Hermes plugin SDK not found");
    return;
  }

  const React = SDK.React;
  const hooks = SDK.hooks || React;
  const components = SDK.components || {};
  const Card = components.Card || "section";
  const CardHeader = components.CardHeader || "div";
  const CardTitle = components.CardTitle || "h2";
  const CardContent = components.CardContent || "div";
  const Badge = components.Badge || "span";
  const Button = components.Button || "button";
  const Input = components.Input || "input";
  const Select = components.Select || "select";
  const SelectOption = components.SelectOption || "option";
  const API = "/api/plugins/hermes-workflows-approvals";

  function e(type, props) {
    const children = Array.prototype.slice.call(arguments, 2);
    return React.createElement.apply(React, [type, props].concat(children));
  }

  function statusClass(status) {
    if (["completed", "approve", "decision_recorded"].includes(status)) return "hwf-ok";
    if (["waiting", "running", "pending"].includes(status)) return "hwf-warn";
    if (["failed", "reject", "cancelled", "invalid_decision"].includes(status)) return "hwf-bad";
    return "hwf-muted";
  }

  function riskClass(level) {
    if (level === "high") return "hwf-risk-high";
    if (level === "medium") return "hwf-risk-medium";
    return "hwf-risk-low";
  }

  function pretty(value) {
    if (value === null || value === undefined || value === "") return "—";
    if (typeof value === "string") return value;
    try { return JSON.stringify(value, null, 2); } catch (_err) { return String(value); }
  }

  function qs(params) {
    const search = new URLSearchParams();
    Object.keys(params || {}).forEach(function (key) {
      if (params[key] !== undefined && params[key] !== null && params[key] !== "") search.set(key, params[key]);
    });
    const text = search.toString();
    return text ? "?" + text : "";
  }

  function useJSON(path, refreshKey) {
    const useState = hooks.useState;
    const useEffect = hooks.useEffect;
    const state = useState({ loading: true, error: null, data: null });
    const value = state[0];
    const setValue = state[1];
    useEffect(function () {
      let cancelled = false;
      setValue({ loading: true, error: null, data: null });
      SDK.fetchJSON(path)
        .then(function (data) { if (!cancelled) setValue({ loading: false, error: null, data: data }); })
        .catch(function (err) { if (!cancelled) setValue({ loading: false, error: err.message || String(err), data: null }); });
      return function () { cancelled = true; };
    }, [path, refreshKey]);
    return value;
  }

  function StatCard(props) {
    return e(Card, { className: "hwf-stat" },
      e(CardContent, { className: "hwf-stat-content" },
        e("div", { className: "hwf-stat-label" }, props.label),
        e("div", { className: "hwf-stat-value" }, String(props.value || 0)),
        props.help && e("div", { className: "hwf-stat-help" }, props.help)));
  }

  function Pill(props) {
    return e(Badge, { className: "hwf-pill " + (props.className || "") }, props.children || props.label);
  }

  function ArtifactRenderSummary(props) {
    const render = props.render || {};
    if (!render.kind) return null;
    return e("div", { className: "hwf-artifact-render" },
      e(Pill, { label: "artifact: " + render.kind }),
      e(Pill, { label: "render: " + render.render }),
      render.warning && e("span", { className: "hwf-muted" }, render.warning));
  }

  function shortId(value) {
    const text = String(value || "");
    if (text.length <= 28) return text || "—";
    return text.slice(0, 12) + "…" + text.slice(-10);
  }

  function primitiveValue(value) {
    return value === null || value === undefined || ["string", "number", "boolean"].includes(typeof value);
  }

  function ArtifactSummary(props) {
    const artifact = props.artifact || {};
    const preview = artifact.preview;
    const rows = [];
    const noisy = { approval_queue: true, entity_proposals: true, archive_candidates: true, zero_side_effect_ledger: true, ledger: true, diagnostics: true, metadata: true };
    if (preview && typeof preview === "object" && !Array.isArray(preview)) {
      ["source_account", "account", "from", "sender", "subject", "snippet", "body_preview", "draft_to", "to", "draft_subject", "gmail_draft_id", "send_requires_approval", "path", "uri", "href", "url"].forEach(function (key) {
        if (preview[key] !== undefined && preview[key] !== null && rows.length < 10) rows.push([key, preview[key]]);
      });
      Object.keys(preview).forEach(function (key) {
        if (rows.length >= 10 || noisy[key] || rows.some(function (row) { return row[0] === key; })) return;
        if (primitiveValue(preview[key])) rows.push([key, preview[key]]);
      });
      if (!rows.length) {
        Object.keys(preview).slice(0, 6).forEach(function (key) {
          const value = preview[key];
          rows.push([key, Array.isArray(value) ? value.length + " items" : value && typeof value === "object" ? "object" : value]);
        });
      }
    } else {
      rows.push(["value", preview === undefined ? "—" : preview]);
    }
    return e("div", { className: "hwf-artifact-summary" },
      e("dl", null, rows.map(function (row) {
        return e("div", { key: row[0], className: "hwf-summary-row" }, e("dt", null, row[0].replace(/_/g, " ")), e("dd", null, pretty(row[1])));
      })),
      preview && typeof preview === "object" && preview.approval_queue && e("p", { className: "hwf-muted" }, "approval_queue hidden in summary; open Raw JSON for debug."));
  }

  function ArtifactCard(props) {
    const artifact = props.artifact;
    const raw = artifact.preview !== undefined ? artifact.preview : artifact;
    return e(Card, { key: artifact.id, className: "hwf-artifact-card" },
      e(CardHeader, null,
        e("div", { className: "hwf-artifact-title" },
          e(CardTitle, null, artifact.title || artifact.kind || "Artifact"),
          e("div", { className: "hwf-meta" },
            e(Pill, { label: "Workflow → Run → Step → Artifact" }),
            e(Pill, { label: artifact.kind || "artifact" }),
            artifact.workflow_id && e("code", { className: "hwf-run-id", title: artifact.workflow_id }, shortId(artifact.workflow_id)),
            artifact.source && artifact.source.key && e(Pill, { label: "step: " + artifact.source.key })))),
      e(CardContent, null,
        e(ArtifactRenderSummary, { render: artifact.artifact_render }),
        e(ArtifactSummary, { artifact: artifact }),
        e("details", { className: "hwf-raw-json" },
          e("summary", null, "Raw JSON"),
          e("pre", null, pretty(raw)))));
  }

  function Tabs(props) {
    return e("div", { className: "hwf-tabs", role: "tablist" }, props.tabs.map(function (tab) {
      return e("button", {
        key: tab,
        type: "button",
        className: "hwf-tab " + (props.active === tab ? "is-active" : ""),
        onClick: function () { props.setActive(tab); }
      }, tab);
    }));
  }

  function ApprovalActions(props) {
    const approval = props.approval;
    const workflowId = approval && approval.workflow_id || props.workflowId;
    const db = props.db;
    const onDecided = props.onDecided;
    const useState = hooks.useState;
    const state = useState({ busy: false, error: null, done: null, note: "", reason: "" });
    const ui = state[0];
    const setUi = state[1];
    if (!approval || approval.decision || !approval.allowed || approval.allowed.length === 0) {
      return e("span", { className: "hwf-muted" }, approval && approval.decision ? "decided" : "no actions");
    }
    function submit(action) {
      setUi(Object.assign({}, ui, { busy: true, error: null, done: null }));
      SDK.fetchJSON(API + "/approvals/decision", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          db: db,
          workflow_id: workflowId,
          key: approval.key,
          action: action,
          note: ui.note || undefined,
          reason: action === "reject" ? (ui.reason || "Rejected from dashboard") : undefined,
          resume: true
        })
      }).then(function (data) {
        setUi(Object.assign({}, ui, { busy: false, error: null, done: data.receipt && data.receipt.status || "recorded" }));
        if (onDecided) onDecided();
      }).catch(function (err) {
        setUi(Object.assign({}, ui, { busy: false, error: err.message || String(err), done: null }));
      });
    }
    return e("div", { className: "hwf-approval-actions" },
      e(Input, {
        value: ui.note,
        placeholder: "Optional note",
        onInput: function (event) { setUi(Object.assign({}, ui, { note: event.target.value })); }
      }),
      approval.allowed.includes("approve") && e(Button, { disabled: ui.busy, onClick: function () { submit("approve"); } }, "Approve"),
      approval.allowed.includes("reject") && e(Button, { disabled: ui.busy, variant: "outline", onClick: function () { submit("reject"); } }, "Reject"),
      ui.done && e("span", { className: "hwf-ok" }, ui.done),
      ui.error && e("span", { className: "hwf-bad" }, ui.error));
  }

  function ApprovalCard(props) {
    const approval = props.approval;
    const risk = approval.risk || { level: "low" };
    return e(Card, { className: "hwf-approval-card" },
      e(CardHeader, null,
        e("div", null,
          e(CardTitle, null, approval.headline || approval.prompt || approval.key),
          e("div", { className: "hwf-meta" },
            e(Pill, { label: approval.status || "waiting", className: statusClass(approval.status) }),
            e(Pill, { label: "risk: " + (risk.level || "low"), className: riskClass(risk.level) }),
            approval.workflow_name && e(Pill, { label: approval.workflow_name }),
            approval.approver && e(Pill, { label: "approver: " + approval.approver }))),
        e("div", { className: "hwf-row-actions" },
          e(Button, { variant: "outline", onClick: function () { props.onView(approval); } }, "View approval"))),
      e(CardContent, null,
        e("p", { className: "hwf-consequence" }, approval.consequence || "Records decision and resumes trusted workflow code"),
        e("div", { className: "hwf-two-col" },
          e("div", null,
            e("div", { className: "hwf-section-title" }, "What you are approving"),
            e("p", null, approval.prompt || approval.key),
            e("p", { className: "hwf-muted" }, "Workflow: " + (approval.workflow_id || "—"))),
          e("div", null,
            e("div", { className: "hwf-section-title" }, "Artifact preview"),
            e(ArtifactRenderSummary, { render: approval.artifact_render }),
            e("pre", null, pretty(approval.artifact_preview || approval.artifact)))),
        e(ApprovalActions, { db: props.db, approval: approval, onDecided: props.onRefresh })));
  }

  function ApprovalDetail(props) {
    const refreshState = hooks.useState(0);
    const refreshKey = refreshState[0];
    const setRefreshKey = refreshState[1];
    const dialogRef = hooks.useRef(null);
    const detail = useJSON(API + "/approvals/detail" + qs({ db: props.db, workflow_id: props.approval.workflow_id, key: props.approval.key }), refreshKey + ":" + props.outerRefresh);

    function closeDialog() {
      const node = dialogRef.current;
      if (node && node.open && typeof node.close === "function") {
        try { node.returnValue = "closed"; node.close("closed"); } catch (_err) { /* noop */ }
      }
      props.onClose();
    }

    hooks.useEffect(function () {
      const node = dialogRef.current;
      if (!node) return;
      if (typeof node.showModal === "function" && !node.open) {
        try { node.showModal(); } catch (_err) { node.setAttribute("open", ""); }
      } else if (!node.open) {
        node.setAttribute("open", "");
      }
      return function () {
        if (node.open && typeof node.close === "function") {
          try { node.close("component-unmount"); } catch (_err) { /* noop */ }
        }
      };
    }, []);

    function frame(content) {
      return e("dialog", {
        ref: dialogRef,
        className: "hwf-approval-dialog",
        onCancel: function (event) { event.preventDefault(); closeDialog(); },
        onClick: function (event) { if (event.target === event.currentTarget) closeDialog(); }
      }, content);
    }

    if (detail.loading) return frame(e("aside", { className: "hwf-approval-detail", role: "dialog", "aria-modal": "true" },
      e("div", { className: "hwf-approval-detail-body" }, "Loading approval…")));
    if (detail.error) return frame(e("aside", { className: "hwf-approval-detail hwf-bad", role: "dialog", "aria-modal": "true" },
      e("div", { className: "hwf-approval-detail-body" }, detail.error)));

    const data = detail.data || {};
    const approval = data.approval_card || props.approval;
    const what = data.what_you_are_approving || {};
    const risk = data.risk || approval.risk || {};
    const header = e("div", { className: "hwf-detail-header" },
      e("div", null,
        e("p", { className: "hwf-eyebrow" }, "Single approval review"),
        e("h2", null, approval.headline || what.prompt || approval.key)),
      e(Button, { className: "hwf-close-button", variant: "outline", onClick: closeDialog }, "Close"));
    const body = e("div", { className: "hwf-approval-detail-body" },
      e("div", { className: "hwf-approval-hero" },
        e("div", null,
          e("div", { className: "hwf-section-title" }, "What you are approving"),
          e("p", null, what.prompt || approval.prompt || approval.key),
          e(ArtifactRenderSummary, { render: what.artifact_render || approval.artifact_render }),
          e("pre", null, pretty(what.artifact || approval.artifact_preview))),
        e("div", null,
          e("div", { className: "hwf-section-title" }, "Consequence"),
          e("p", { className: "hwf-consequence" }, data.consequence || approval.consequence),
          e("div", { className: "hwf-section-title" }, "Risk / blast radius"),
          e(Pill, { label: (risk.level || "low") + " risk", className: riskClass(risk.level) }),
          e("p", { className: "hwf-muted" }, risk.reason || "Records human provenance, then resumes trusted local workflow code."),
          e("div", { className: "hwf-section-title" }, "Decision semantics"),
          e("p", null, (data.decision_semantics && data.decision_semantics.label) || "Record and resume"),
          e("p", { className: "hwf-muted" }, data.decision_semantics && data.decision_semantics.description))),
      e(ApprovalActions, { db: props.db, approval: approval, onDecided: function () { setRefreshKey(refreshKey + 1); if (props.onRefresh) props.onRefresh(); } }),
      e("div", { className: "hwf-section-title" }, "Approval timeline"),
      e("ol", { className: "hwf-timeline" }, (data.timeline || []).map(function (event) {
        return e("li", { key: event.seq + ":" + event.type },
          e("span", null, "#" + event.seq + " " + event.type),
          e("code", null, event.key || ""));
      })));
    return frame(e("aside", { className: "hwf-approval-detail", role: "dialog", "aria-modal": "true", onClick: function (event) { event.stopPropagation(); } }, header, body));
  }

  function PythonCode(props) {
    const code = props.code || "";
    const parts = [];
    const pattern = /(#[^\n]*|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\b(?:async|await|def|class|return|if|elif|else|for|while|try|except|finally|with|from|import|as|raise|yield|True|False|None|and|or|not|in|is)\b)/g;
    let last = 0;
    let match;
    while ((match = pattern.exec(code))) {
      if (match.index > last) parts.push(code.slice(last, match.index));
      const token = match[0];
      let className = "hwf-code-keyword";
      if (token.startsWith("#")) className = "hwf-code-comment";
      else if (token.startsWith("\"") || token.startsWith("'")) className = "hwf-code-string";
      parts.push(e("span", { key: parts.length, className: className }, token));
      last = match.index + token.length;
    }
    if (last < code.length) parts.push(code.slice(last));
    return e("code", { className: props.className || "language-python" }, parts);
  }

  function WorkflowSourceModal(props) {
    const dialogRef = hooks.useRef(null);
    const source = useJSON(API + "/definitions/" + encodeURIComponent(props.definition.id) + "/source" + qs({ db: props.db }), props.definition.id + ":source");
    function closeDialog() {
      const node = dialogRef.current;
      if (node && node.open && typeof node.close === "function") {
        try { node.returnValue = "closed"; node.close("closed"); } catch (_err) { /* noop */ }
      }
      props.onClose();
    }
    hooks.useEffect(function () {
      const node = dialogRef.current;
      if (!node) return;
      if (typeof node.showModal === "function" && !node.open) {
        try { node.showModal(); } catch (_err) { node.setAttribute("open", ""); }
      } else if (!node.open) {
        node.setAttribute("open", "");
      }
      return function () {
        if (node.open && typeof node.close === "function") {
          try { node.close("component-unmount"); } catch (_err) { /* noop */ }
        }
      };
    }, []);
    const data = source.data || {};
    const location = data.location || {};
    return e("dialog", {
      ref: dialogRef,
      className: "hwf-approval-dialog",
      onCancel: function (event) { event.preventDefault(); closeDialog(); },
      onClick: function (event) { if (event.target === event.currentTarget) closeDialog(); }
    }, e("aside", { className: "hwf-approval-detail", role: "dialog", "aria-modal": "true", onClick: function (event) { event.stopPropagation(); } },
      e("div", { className: "hwf-detail-header" },
        e("div", null,
          e("p", { className: "hwf-eyebrow" }, "Workflow source"),
          e("h2", null, props.definition.name || props.definition.id),
          e("p", { className: "hwf-muted" }, location.file ? location.file + ":" + location.line_start + "-" + location.line_end : props.definition.workflow_ref)),
        e(Button, { className: "hwf-close-button", variant: "outline", onClick: closeDialog }, "Close")),
      e("div", { className: "hwf-approval-detail-body" },
        source.loading && e("p", null, "Loading workflow source…"),
        source.error && e("p", { className: "hwf-bad" }, source.error),
        data.code && e("pre", { className: "hwf-code-block" }, e(PythonCode, { className: data.highlight_class || "language-python", code: data.code })))));
  }

  function DefinitionCard(props) {
    const definition = props.definition;
    const useState = hooks.useState;
    const sourceState = useState(false);
    const showSource = sourceState[0];
    const setShowSource = sourceState[1];
    const inputState = useState(pretty(definition.input_defaults || {}));
    const inputText = inputState[0];
    const setInputText = inputState[1];
    const runState = useState({ busy: false, error: null, result: null });
    const runUi = runState[0];
    const setRunUi = runState[1];
    function runWorkflow() {
      let input;
      try { input = JSON.parse(inputText || "{}"); }
      catch (err) { setRunUi({ busy: false, error: "Invalid JSON input: " + err.message, result: null }); return; }
      setRunUi({ busy: true, error: null, result: null });
      SDK.fetchJSON(API + "/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ db: props.db, definition_id: definition.id, input: input })
      }).then(function (data) {
        setRunUi({ busy: false, error: null, result: data.run });
        if (props.onRefresh) props.onRefresh();
      }).catch(function (err) {
        setRunUi({ busy: false, error: err.message || String(err), result: null });
      });
    }
    const runs = definition.runs || { total: 0, by_status: {} };
    const canRun = definition.runnable !== false;
    return e(Card, { className: "hwf-definition-card" },
      e(CardHeader, null,
        e("div", null,
          e(CardTitle, null, definition.name || definition.id),
          e("p", { className: "hwf-muted" }, definition.description || definition.workflow_ref)),
        e("div", { className: "hwf-row-actions" },
          e(Button, { variant: "outline", onClick: function () { setShowSource(true); } }, "View code"),
          canRun
            ? e(Button, { disabled: runUi.busy, onClick: runWorkflow }, definition.run_button_label || "Run workflow")
            : e(Pill, { label: "history only" }))),
      e(CardContent, null,
        e("div", { className: "hwf-meta" },
          e(Pill, { label: "runs: " + runs.total }),
          Object.keys(runs.by_status || {}).map(function (status) { return e(Pill, { key: status, label: status + ": " + runs.by_status[status], className: statusClass(status) }); }),
          (definition.tags || []).map(function (tag) { return e(Pill, { key: tag, label: tag }); })),
        canRun ? e("div", { className: "hwf-run-box" },
          e("div", { className: "hwf-section-title" }, "Run workflow"),
          e("textarea", {
            value: inputText,
            rows: 7,
            onInput: function (event) { setInputText(event.target.value); },
            spellCheck: false
          }),
          e("p", { className: "hwf-muted" }, "Schema-driven inputs when available. This starts the configured workflow ref only."))
        : e("div", { className: "hwf-run-box hwf-history-only" },
          e("div", { className: "hwf-section-title" }, "History only"),
          e("p", { className: "hwf-muted" }, "This workflow was inferred from run history. Add it to workflow_catalog before browser launches are allowed.")),
        runUi.error && e("p", { className: "hwf-bad" }, runUi.error),
        runUi.result && e("div", { className: "hwf-run-result" },
          e("strong", null, "Started: "), e("code", null, runUi.result.workflow_id), " ", e(Pill, { label: runUi.result.status, className: statusClass(runUi.result.status) })),
        e("details", null,
          e("summary", null, "Input schema"),
          e("pre", null, pretty(definition.input_schema))),
        e("details", null,
          e("summary", null, "Run history"),
          definition.latest_run ? e("p", null, "Latest: ", e("code", null, definition.latest_run.workflow_id), " ", e(Pill, { label: definition.latest_run.status, className: statusClass(definition.latest_run.status) })) : e("p", { className: "hwf-muted" }, "No runs yet.")),
        showSource && e(WorkflowSourceModal, { db: props.db, definition: definition, onClose: function () { setShowSource(false); } })));
  }

  function RunApprovalSummary(props) {
    const approval = props.approval || {};
    return e("div", { className: "hwf-run-approval" },
      e("div", null,
        e("strong", null, approval.prompt || approval.key || "Approval needed"),
        e("p", { className: "hwf-muted" }, "Workflow → Run → Step → Approval")),
      e("div", { className: "hwf-meta" },
        e(Pill, { label: approval.status || "waiting", className: statusClass(approval.status || "waiting") }),
        approval.key && e(Pill, { label: "key: " + approval.key })));
  }

  function RunDag(props) {
    const selectedState = hooks.useState(null);
    const selectedDagNode = selectedState[0];
    const setSelectedDagNode = selectedState[1];
    const dag = useJSON(API + "/runs/" + encodeURIComponent(props.workflowId) + "/dag" + qs({ db: props.db }), props.workflowId + ":dag:" + props.refreshKey);
    if (dag.loading) return e("p", { className: "hwf-muted" }, "Loading Run DAG…");
    if (dag.error) return e("p", { className: "hwf-bad" }, dag.error);
    const data = dag.data || {};
    const nodes = data.nodes || [];
    const selected = selectedDagNode || nodes[0] || null;
    const edgesByTarget = {};
    (data.edges || []).forEach(function (edge) { edgesByTarget[edge.to] = edge.from; });
    return e("div", { className: "hwf-dag" },
      e("div", { className: "hwf-dag-strip" }, nodes.map(function (node) {
        return e("button", {
          key: node.id,
          type: "button",
          className: "hwf-dag-node " + (selected && selected.id === node.id ? "hwf-dag-node-selected" : ""),
          onClick: function () { setSelectedDagNode(node); },
          title: node.id
        },
          e("span", { className: "hwf-dag-kind" }, node.kind),
          e("strong", null, shortId(node.label || node.id)),
          e("span", { className: statusClass(node.status) }, node.status || "recorded"),
          node.artifact_count ? e("span", { className: "hwf-muted" }, node.artifact_count + " artifact" + (node.artifact_count === 1 ? "" : "s")) : null,
          edgesByTarget[node.id] && e("small", { className: "hwf-muted" }, "after " + shortId(edgesByTarget[node.id])));
      })),
      selected && e("div", { className: "hwf-dag-inspector" },
        e("div", { className: "hwf-section-title" }, "Selected piece"),
        e("div", { className: "hwf-meta" },
          e(Pill, { label: selected.kind || "node" }),
          e(Pill, { label: selected.status || "recorded", className: statusClass(selected.status) }),
          e("code", { className: "hwf-run-id", title: selected.id }, shortId(selected.id))),
        e("div", { className: "hwf-section-title" }, "Artifacts from this step"),
        selected.artifacts && selected.artifacts.length ? selected.artifacts.map(function (artifact) {
          return e(ArtifactCard, { key: artifact.id, artifact: artifact });
        }) : e("p", { className: "hwf-muted" }, "No artifacts captured for this piece yet.")));
  }

  function RunRow(props) {
    const run = props.run;
    return e(Card, { className: "hwf-run-row" },
      e(CardContent, null,
        e("div", { className: "hwf-run-grid" },
          e("div", { className: "hwf-run-main" },
            e("strong", { title: run.workflow_name || "workflow" }, run.workflow_name || "workflow"),
            e("p", { className: "hwf-muted", title: run.workflow_ref || "—" }, run.workflow_ref || "—")),
          e("code", { className: "hwf-run-id", title: run.workflow_id }, shortId(run.workflow_id)),
          e("div", { className: "hwf-run-signals" },
            e(Pill, { label: run.status, className: statusClass(run.status) }),
            e("span", { className: "hwf-muted hwf-waiting-on", title: run.waiting_on || "not waiting" }, run.waiting_on || "not waiting")),
          e("div", { className: "hwf-run-tail" },
            e(Button, { variant: "outline", onClick: function () { props.onInspect(run); } }, "Inspect run")))));
  }

  function RunsPanel(props) {
    const selectedState = hooks.useState(null);
    const selected = selectedState[0];
    const setSelected = selectedState[1];
    const statusPath = selected ? API + "/runs/" + encodeURIComponent(selected.workflow_id) + qs({ db: props.db }) : API + "/runs" + qs({ db: props.db, limit: 1 });
    const status = useJSON(statusPath, props.refreshKey + ":" + (selected && selected.workflow_id || "none"));
    return e("div", { className: "hwf-panel" },
      e("div", { className: "hwf-panel-header" }, e("h2", null, "Runs"), e("p", { className: "hwf-muted" }, "Workflow → Run → Step → Artifact/Approval. Inspect a run for outputs and decisions.")),
      (props.runs || []).map(function (run) { return e(RunRow, { key: run.workflow_id, run: run, onInspect: setSelected }); }),
      selected && e(Card, { className: "hwf-inspector" },
        e(CardHeader, null,
          e("div", null,
            e(CardTitle, null, "Run status"),
            e("p", { className: "hwf-muted" }, selected.workflow_name || selected.workflow_ref || selected.workflow_id)),
          e(Button, { variant: "outline", onClick: function () { setSelected(null); } }, "Close")),
        e(CardContent, null,
          status && status.loading && e("p", null, "Loading status…"),
          status && status.error && e("p", { className: "hwf-bad" }, status.error),
          status && status.data && e("div", null,
            e("div", { className: "hwf-meta" },
              e(Pill, { label: status.data.run.status, className: statusClass(status.data.run.status) }),
              e(Pill, { label: "events: " + status.data.run.event_count }),
              e(Pill, { label: "artifacts: " + status.data.artifacts.length }),
              e("code", { className: "hwf-run-id", title: status.data.run.workflow_id }, shortId(status.data.run.workflow_id))),
            e("div", { className: "hwf-section-title" }, "Run DAG"),
            e(RunDag, { db: props.db, workflowId: selected.workflow_id, refreshKey: props.refreshKey }),
            e("div", { className: "hwf-section-title" }, "Approvals in this run"),
            (status.data.run.approvals || []).length ? (status.data.run.approvals || []).map(function (approval) {
              return e(RunApprovalSummary, { key: approval.key, approval: approval });
            }) : e("p", { className: "hwf-muted" }, "No approval gates recorded for this run."),
            e("div", { className: "hwf-section-title" }, "Artifacts / outputs in this run"),
            status.data.artifacts.length ? status.data.artifacts.map(function (artifact) { return e(ArtifactCard, { key: artifact.id, artifact: artifact }); }) : e("p", { className: "hwf-muted" }, "No artifacts captured yet."),
            e("details", { className: "hwf-raw-json" },
              e("summary", null, "Recent events"),
              e("pre", null, pretty(status.data.run.recent_events || [])))))));
  }

  function OverviewPanel(props) {
    const counts = props.counts || {};
    const approvals = props.approvals || [];
    return e("div", { className: "hwf-panel" },
      e("div", { className: "hwf-stats" },
        e(StatCard, { label: "Runnable workflows", value: props.definitions.length, help: "Catalog" }),
        e(StatCard, { label: "Runs", value: props.runs.length, help: "Recent history" }),
        e(StatCard, { label: "Waiting", value: counts.waiting || 0, help: "Blocked runs" }),
        e(StatCard, { label: "Needs my approval", value: approvals.length, help: "Active approvals" }),
        e(StatCard, { label: "Artifacts", value: props.artifacts.length, help: "Outputs" })),
      e("div", { className: "hwf-two-col" },
        e("div", null,
          e("div", { className: "hwf-panel-header" }, e("h2", null, "Needs my approval")),
          approvals.length ? approvals.slice(0, 3).map(function (approval) { return e(ApprovalCard, { key: approval.workflow_id + approval.key, db: props.db, approval: approval, onView: props.onViewApproval, onRefresh: props.onRefresh }); }) : e(Card, null, e(CardContent, { className: "hwf-empty" }, "No active approvals."))),
        e("div", null,
          e("div", { className: "hwf-panel-header" }, e("h2", null, "Recent runs")),
          props.runs.slice(0, 5).map(function (run) { return e(RunRow, { key: run.workflow_id, run: run, onInspect: function () {} }); }))));
  }

  function ArtifactsPanel(props) {
    const grouped = {};
    (props.artifacts || []).forEach(function (artifact) {
      const id = artifact.workflow_id || "unknown-run";
      if (!grouped[id]) grouped[id] = [];
      grouped[id].push(artifact);
    });
    const runIds = Object.keys(grouped);
    return e("div", { className: "hwf-panel" },
      e("div", { className: "hwf-panel-header" },
        e("h2", null, "Artifacts"),
        e("p", { className: "hwf-muted" }, "Workflow → Run → Step → Artifact. Top-level queue for active approvals; artifacts live under their run.")),
      runIds.length ? runIds.map(function (workflowId) {
        const run = (props.runs || []).find(function (item) { return item.workflow_id === workflowId; }) || {};
        return e("details", { key: workflowId, className: "hwf-run-artifacts" },
          e("summary", { className: "hwf-run-artifacts-summary" },
            e("div", null,
              e(CardTitle, null, run.workflow_name || run.workflow_ref || "Run artifacts"),
              e("div", { className: "hwf-meta" },
                e("code", { className: "hwf-run-id", title: workflowId }, shortId(workflowId)),
                run.status && e(Pill, { label: run.status, className: statusClass(run.status) }),
                e(Pill, { label: grouped[workflowId].length + " artifact" + (grouped[workflowId].length === 1 ? "" : "s") }))),
            e("span", { className: "hwf-collapse-hint" }, "Expand")),
          e("div", { className: "hwf-run-artifacts-body" }, grouped[workflowId].map(function (artifact) { return e(ArtifactCard, { key: artifact.id, artifact: artifact }); })));
      }) : e(Card, null, e(CardContent, { className: "hwf-empty" }, "No artifacts yet. Inspect a run after it emits outputs.")));
  }

  function WorkflowsPage() {
    const useState = hooks.useState;
    const tabState = useState("Overview");
    const activeTab = tabState[0];
    const setActiveTab = tabState[1];
    const approvalState = useState(null);
    const selectedApproval = approvalState[0];
    const setSelectedApproval = approvalState[1];
    const refreshState = useState(0);
    const refreshKey = refreshState[0];
    const setRefreshKey = refreshState[1];
    const dbs = useJSON(API + "/dbs", refreshKey);
    const firstDb = dbs.data && dbs.data.dbs && dbs.data.dbs[0] && dbs.data.dbs[0].name;
    const activeDb = firstDb || "";
    const overview = useJSON(activeDb ? API + "/overview" + qs({ db: activeDb, recent_events: 10, command_limit: 10 }) : API + "/overview", refreshKey + ":" + activeDb);
    const definitionsData = useJSON(activeDb ? API + "/definitions" + qs({ db: activeDb }) : API + "/definitions", refreshKey + ":defs:" + activeDb);
    const runsData = useJSON(activeDb ? API + "/runs" + qs({ db: activeDb, limit: 100 }) : API + "/runs", refreshKey + ":runs:" + activeDb);
    const approvalsData = useJSON(activeDb ? API + "/approvals" + qs({ db: activeDb, status: "waiting" }) : API + "/approvals", refreshKey + ":approvals:" + activeDb);

    function refresh() { setRefreshKey(refreshKey + 1); }
    if (dbs.loading) return e("div", { className: "hwf-page" }, "Loading workflow DBs…");
    if (dbs.error) return e("div", { className: "hwf-page hwf-bad" }, dbs.error);
    const overviewData = overview.data || {};
    const definitions = definitionsData.data && definitionsData.data.definitions || overviewData.definitions || [];
    const runs = runsData.data && runsData.data.runs || overviewData.workflows || [];
    const approvals = approvalsData.data && approvalsData.data.approvals || overviewData.active_approvals || [];
    const artifacts = overviewData.artifacts || [];
    const counts = overviewData.counts_by_status || (runsData.data && runsData.data.counts && runsData.data.counts.by_status) || {};
    const hasConsoleData = Boolean(overview.data || definitionsData.data || runsData.data || approvalsData.data);
    const initialConsoleLoading = (overview.loading || definitionsData.loading || runsData.loading || approvalsData.loading) && !hasConsoleData;
    const refreshingConsole = (overview.loading || definitionsData.loading || runsData.loading || approvalsData.loading) && hasConsoleData;

    return e("div", { className: "hwf-page hwf-shell" },
      e("div", { className: "hwf-header" },
        e("div", null,
          e("p", { className: "hwf-eyebrow" }, "Operator console"),
          e("h1", null, "Hermes Workflows"),
          e("p", { className: "hwf-muted" }, "Run workflows, track status/history, review artifacts, and make high-context approvals that record provenance and resume trusted workflow code.")),
        e("div", { className: "hwf-controls" },
          e("div", { className: "hwf-active-source", title: "Workflow state source" },
            e("span", { className: "hwf-active-source-label" }, "Source"),
            e("strong", null, activeDb || "Not configured")),
          e(Button, { onClick: refresh }, "Refresh"))),
      e(Tabs, { tabs: ["Overview", "Workflows", "Runs", "Approvals", "Artifacts"], active: activeTab, setActive: setActiveTab }),
      e("div", { className: "hwf-runtime-note" },
        e("strong", null, "Runtime: "),
        "workflow code runs in the local WorkflowEngine process for the configured workflow state source. Dashboard approval buttons record human provenance and immediately resume trusted local workflow code."),
      approvals.length > 0 && e("div", { className: "hwf-attention", role: "alert" },
        e("strong", null, approvals.length + " approval" + (approvals.length === 1 ? "" : "s") + " waiting. "),
        "Open Approvals or a card below to review consequence, evidence, and record-and-resume decision semantics."),
      initialConsoleLoading && e("p", { className: "hwf-muted" }, "Loading workflow data…"),
      refreshingConsole && e("p", { className: "hwf-muted hwf-refreshing" }, "Refreshing workflow console…"),
      (overview.error || definitionsData.error || runsData.error || approvalsData.error) && e("p", { className: "hwf-bad" }, overview.error || definitionsData.error || runsData.error || approvalsData.error),
      activeTab === "Overview" && e(OverviewPanel, { db: activeDb, definitions: definitions, runs: runs, approvals: approvals, artifacts: artifacts, counts: counts, onViewApproval: setSelectedApproval, onRefresh: refresh }),
      activeTab === "Workflows" && e("div", { className: "hwf-panel" },
        e("div", { className: "hwf-panel-header" }, e("h2", null, "Workflows you can run"), e("p", { className: "hwf-muted" }, "See workflows I can run, then run one with JSON inputs.")),
        definitions.length ? definitions.map(function (definition) { return e(DefinitionCard, { key: definition.id, db: activeDb, definition: definition, onRefresh: refresh }); }) : e(Card, null, e(CardContent, { className: "hwf-empty" }, "No runnable workflows configured yet."))),
      activeTab === "Runs" && e(RunsPanel, { db: activeDb, runs: runs, refreshKey: refreshKey }),
      activeTab === "Approvals" && e("div", { className: "hwf-panel" },
        e("div", { className: "hwf-panel-header" }, e("h2", null, "Active approvals"), e("p", { className: "hwf-muted" }, "See a list of active approvals needed.")),
        approvals.length ? approvals.map(function (approval) { return e(ApprovalCard, { key: approval.workflow_id + approval.key, db: activeDb, approval: approval, onView: setSelectedApproval, onRefresh: refresh }); }) : e(Card, null, e(CardContent, { className: "hwf-empty" }, "No active approvals."))),
      activeTab === "Artifacts" && e(ArtifactsPanel, { artifacts: artifacts, runs: runs }),
      selectedApproval && e(ApprovalDetail, { db: activeDb, approval: selectedApproval, outerRefresh: refreshKey, onClose: function () { setSelectedApproval(null); }, onRefresh: refresh }));
  }

  window.__HERMES_PLUGINS__.register("hermes-workflows-approvals", WorkflowsPage);
})();
