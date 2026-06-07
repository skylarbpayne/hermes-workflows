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
    if (["failed", "reject", "cancelled"].includes(status)) return "hwf-bad";
    return "hwf-muted";
  }

  function pretty(value) {
    if (value === null || value === undefined || value === "") return "—";
    if (typeof value === "string") return value;
    try { return JSON.stringify(value, null, 2); } catch (_err) { return String(value); }
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
        e("div", { className: "hwf-stat-value" }, String(props.value || 0))));
  }

  function ApprovalActions(props) {
    const approval = props.approval;
    const workflow = props.workflow;
    const db = props.db;
    const onDecided = props.onDecided;
    const useState = hooks.useState;
    const state = useState({ busy: false, error: null, done: null });
    const ui = state[0];
    const setUi = state[1];
    if (!approval || approval.decision || !approval.allowed || approval.allowed.length === 0) {
      return e("span", { className: "hwf-muted" }, approval && approval.decision ? "decided" : "no actions");
    }
    function submit(action) {
      const approver = approval && approval.approver ? String(approval.approver) : "human";
      const defaultBy = approver.startsWith("human:") ? approver.slice("human:".length) : approver;
      const by = window.prompt("Approver id", defaultBy || "human");
      if (!by) return;
      const messageId = "dashboard-" + Date.now() + "-" + Math.random().toString(36).slice(2);
      setUi({ busy: true, error: null, done: null });
      SDK.fetchJSON(API + "/approvals/decision", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          db: db,
          workflow_id: workflow.workflow_id,
          key: approval.key,
          action: action,
          by: by,
          channel: "hermes-dashboard",
          message_id: messageId,
          resume: false
        })
      }).then(function (data) {
        setUi({ busy: false, error: null, done: data.receipt && data.receipt.status || "recorded" });
        if (onDecided) onDecided();
      }).catch(function (err) {
        setUi({ busy: false, error: err.message || String(err), done: null });
      });
    }
    return e("div", { className: "hwf-approval-actions" },
      approval.allowed.includes("approve") && e(Button, { disabled: ui.busy, onClick: function () { submit("approve"); } }, "Approve"),
      approval.allowed.includes("reject") && e(Button, { disabled: ui.busy, variant: "outline", onClick: function () { submit("reject"); } }, "Reject"),
      ui.done && e("span", { className: "hwf-ok" }, ui.done),
      ui.error && e("span", { className: "hwf-bad" }, ui.error));
  }

  function WorkflowCard(props) {
    const wf = props.workflow;
    const approvals = wf.approvals || [];
    const pending = wf.pending_commands || [];
    const diagnostics = wf.diagnostics || [];
    const events = wf.recent_events || wf.events || [];
    return e(Card, { className: "hwf-workflow-card" },
      e(CardHeader, null,
        e(CardTitle, null, wf.workflow_id),
        e("div", { className: "hwf-meta" },
          e(Badge, { className: statusClass(wf.status) }, "status: " + wf.status),
          e(Badge, null, wf.workflow_name || "workflow"),
          e(Badge, null, "waiting: " + (wf.waiting_on || "none")))),
      e(CardContent, null,
        e("div", { className: "hwf-section-title" }, "Approvals"),
        approvals.length ? approvals.map(function (approval) {
          return e("div", { key: approval.key, className: "hwf-approval-row" },
            e("div", null,
              e("strong", null, approval.key),
              e("span", { className: statusClass(approval.status) }, " " + approval.status),
              e("p", { className: "hwf-muted" }, approval.prompt || ""),
              e("pre", null, pretty(approval.artifact))),
            e(ApprovalActions, { db: props.db, workflow: wf, approval: approval, onDecided: props.onRefresh }));
        }) : e("p", { className: "hwf-muted" }, "No approvals recorded."),
        e("div", { className: "hwf-section-title" }, "Pending commands"),
        pending.length ? e("ul", { className: "hwf-list" }, pending.map(function (cmd) {
          return e("li", { key: cmd.key }, e("code", null, cmd.key), " ", cmd.type, " ", e("span", { className: statusClass(cmd.status) }, cmd.status));
        })) : e("p", { className: "hwf-muted" }, "None."),
        diagnostics.length ? e("div", null,
          e("div", { className: "hwf-section-title" }, "Diagnostics"),
          e("ul", { className: "hwf-list" }, diagnostics.map(function (diag, idx) {
            return e("li", { key: idx }, e("strong", null, diag.label), ": ", diag.message);
          }))) : null,
        e("details", null,
          e("summary", null, "Recent events"),
          e("pre", null, pretty(events)))));
  }

  function WorkflowsPage() {
    const useState = hooks.useState;
    const selectedState = useState("");
    const selectedDb = selectedState[0];
    const setSelectedDb = selectedState[1];
    const refreshState = useState(0);
    const refreshKey = refreshState[0];
    const setRefreshKey = refreshState[1];
    const dbs = useJSON(API + "/dbs", refreshKey);
    const firstDb = dbs.data && dbs.data.dbs && dbs.data.dbs[0] && dbs.data.dbs[0].name;
    const activeDb = selectedDb || firstDb || "";
    const overview = useJSON(activeDb ? API + "/overview?db=" + encodeURIComponent(activeDb) + "&recent_events=10&command_limit=10" : API + "/overview", refreshKey + ":" + activeDb);

    if (dbs.loading) return e("div", { className: "hwf-page" }, "Loading workflow DBs…");
    if (dbs.error) return e("div", { className: "hwf-page hwf-bad" }, dbs.error);
    const workflows = overview.data && overview.data.workflows || [];
    const counts = overview.data && overview.data.counts_by_status || {};
    return e("div", { className: "hwf-page" },
      e("div", { className: "hwf-header" },
        e("div", null,
          e("h1", null, "Hermes Workflows"),
          e("p", { className: "hwf-muted" }, "Workflow observability, pending commands, diagnostics, events, and record-only approval decisions.")),
        e("div", { className: "hwf-controls" },
          e(Select, { value: activeDb, onValueChange: function (value) { setSelectedDb(value); } },
            (dbs.data.dbs || []).map(function (db) { return e(SelectOption, { key: db.name, value: db.name }, db.name + (db.exists ? "" : " (missing)")); })),
          e(Button, { onClick: function () { setRefreshKey(refreshKey + 1); } }, "Refresh"))),
      e("div", { className: "hwf-stats" },
        e(StatCard, { label: "Workflows", value: workflows.length }),
        e(StatCard, { label: "Waiting", value: counts.waiting || 0 }),
        e(StatCard, { label: "Running", value: counts.running || 0 }),
        e(StatCard, { label: "Completed", value: counts.completed || 0 })),
      overview.loading && e("p", { className: "hwf-muted" }, "Loading workflows…"),
      overview.error && e("p", { className: "hwf-bad" }, overview.error),
      workflows.length ? workflows.map(function (wf) {
        return e(WorkflowCard, { key: wf.workflow_id, db: activeDb, workflow: wf, onRefresh: function () { setRefreshKey(refreshKey + 1); } });
      }) : e(Card, null, e(CardContent, { className: "hwf-empty" }, "No workflows found for this DB.")));
  }

  window.__HERMES_PLUGINS__.register("hermes-workflows-approvals", WorkflowsPage);
})();
