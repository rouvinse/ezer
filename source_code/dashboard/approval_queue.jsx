import { useState, useEffect } from "react";

// ── Mock data (replace with real API calls in production) ─────────────────────
const MOCK_QUEUE = [
  {
    action_id: "a001", customer_id: "C0288", action_type: "escalate_to_csm",
    reason: "Critical churn risk (82%) on a $957/mo Enterprise account. Immediate CSM intervention required.",
    priority: 5, status: "pending_approval", created_at: "2025-07-29 09:14:22",
    customer_name: "Customer 0288", customer_plan: "Enterprise", customer_mrr: 957.04,
    churn_score: 0.82, segment: "At-Risk", ltv_estimate: 3448,
  },
  {
    action_id: "a002", customer_id: "C0144", action_type: "send_retention_email",
    reason: "High churn risk (67%). Segment: Lost. Send targeted retention offer.",
    priority: 4, status: "pending_approval", created_at: "2025-07-29 09:14:22",
    customer_name: "Customer 0144", customer_plan: "Pro", customer_mrr: 214.74,
    churn_score: 0.67, segment: "Lost", ltv_estimate: 1543,
  },
  {
    action_id: "a003", customer_id: "C0391", action_type: "send_upgrade_offer",
    reason: "High conversion probability (78%) on Starter plan. Send personalised upgrade offer.",
    priority: 4, status: "pending_approval", created_at: "2025-07-29 09:14:23",
    customer_name: "Customer 0391", customer_plan: "Starter", customer_mrr: 65.44,
    churn_score: 0.12, conversion_score: 0.78, segment: "Prospect", ltv_estimate: 1574,
  },
  {
    action_id: "a004", customer_id: "C0072", action_type: "proactive_support_call",
    reason: "Cancellation intent signal detected (1 cancel ticket, sentiment: -0.74). Schedule proactive support call.",
    priority: 4, status: "pending_approval", created_at: "2025-07-29 09:14:23",
    customer_name: "Customer 0072", customer_plan: "Pro", customer_mrr: 261.51,
    churn_score: 0.45, segment: "At-Risk", ltv_estimate: 3139,
  },
  {
    action_id: "a005", customer_id: "C0019", action_type: "send_reengagement_email",
    reason: "No activity for 44 days. Churn score: 41%. Re-engagement sequence needed.",
    priority: 3, status: "pending_approval", created_at: "2025-07-29 09:14:24",
    customer_name: "Customer 0019", customer_plan: "Free", customer_mrr: 0,
    churn_score: 0.41, segment: "Lost", ltv_estimate: 0,
  },
  {
    action_id: "a006", customer_id: "C0203", action_type: "send_loyalty_reward",
    reason: "Champion customer with 412 days tenure. Reward loyalty to reinforce retention.",
    priority: 2, status: "pending_approval", created_at: "2025-07-29 09:14:24",
    customer_name: "Customer 0203", customer_plan: "Enterprise", customer_mrr: 1100,
    churn_score: 0.02, segment: "Champion", ltv_estimate: 26400,
  },
];

const MOCK_STATS = {
  pending_approval: 6,
  by_priority: { Critical: 1, High: 3, Medium: 1, Low: 1 },
  approved_today: 4,
  rejected_today: 1,
  trust_mode: "full_review",
};

// ── Constants ─────────────────────────────────────────────────────────────────
const ACTION_ICONS = {
  escalate_to_csm:        "🚨",
  send_retention_email:   "📧",
  send_reengagement_email:"💌",
  send_upgrade_offer:     "⬆️",
  enroll_nurture_sequence:"🌱",
  proactive_support_call: "📞",
  send_loyalty_reward:    "🎁",
  send_health_checkin:    "🩺",
};

const PRIORITY_CONFIG = {
  5: { label: "Critical", color: "#EF4444", bg: "rgba(239,68,68,0.1)",   border: "rgba(239,68,68,0.3)"  },
  4: { label: "High",     color: "#F97316", bg: "rgba(249,115,22,0.1)",  border: "rgba(249,115,22,0.3)" },
  3: { label: "Medium",   color: "#EAB308", bg: "rgba(234,179,8,0.1)",   border: "rgba(234,179,8,0.3)"  },
  2: { label: "Low",      color: "#22C55E", bg: "rgba(34,197,94,0.1)",   border: "rgba(34,197,94,0.3)"  },
  1: { label: "Info",     color: "#6B7280", bg: "rgba(107,114,128,0.1)", border: "rgba(107,114,128,0.3)"},
};

const PLAN_COLORS = {
  Enterprise: "#A78BFA", Pro: "#60A5FA", Starter: "#34D399", Free: "#6B7280"
};

const TRUST_MODES = [
  { value: "full_review",   label: "Full Review",    desc: "Every action needs approval" },
  { value: "high_risk_only",label: "High Risk Only", desc: "Only P4/P5 need approval"    },
  { value: "autonomous",    label: "Autonomous",     desc: "All actions auto-execute"     },
];

// ── Sub-components ────────────────────────────────────────────────────────────
function StatCard({ label, value, color, icon }) {
  return (
    <div style={{ background: "var(--surface-1)", border: "0.5px solid var(--border)", borderRadius: 10, padding: "14px 18px" }}>
      <div style={{ fontSize: 11, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: ".07em", marginBottom: 6, display: "flex", alignItems: "center", gap: 6 }}>
        <span>{icon}</span>{label}
      </div>
      <div style={{ fontSize: 26, fontWeight: 500, color: color || "var(--text-primary)" }}>{value}</div>
    </div>
  );
}

function PriorityBadge({ priority }) {
  const cfg = PRIORITY_CONFIG[priority] || PRIORITY_CONFIG[1];
  return (
    <span style={{ background: cfg.bg, color: cfg.color, border: `0.5px solid ${cfg.border}`, borderRadius: 4, fontSize: 10, fontWeight: 600, padding: "2px 7px", letterSpacing: ".06em" }}>
      P{priority} {cfg.label}
    </span>
  );
}

function ActionTypeLabel({ type }) {
  return (
    <span style={{ fontSize: 12, color: "var(--text-secondary)", background: "var(--surface-0)", border: "0.5px solid var(--border)", borderRadius: 4, padding: "2px 8px" }}>
      {ACTION_ICONS[type] || "•"} {type.replace(/_/g, " ")}
    </span>
  );
}

function ScoreBar({ value, color }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
      <div style={{ flex: 1, height: 4, background: "var(--surface-0)", borderRadius: 2, overflow: "hidden" }}>
        <div style={{ width: `${Math.round(value * 100)}%`, height: "100%", background: color, borderRadius: 2 }} />
      </div>
      <span style={{ fontSize: 11, color: "var(--text-secondary)", minWidth: 32, textAlign: "right" }}>{Math.round(value * 100)}%</span>
    </div>
  );
}

// ── Action Card ───────────────────────────────────────────────────────────────
function ActionCard({ action, onApprove, onReject, onEdit }) {
  const [expanded, setExpanded]     = useState(false);
  const [editMode, setEditMode]     = useState(false);
  const [editedReason, setEditedReason] = useState(action.reason);
  const [rejectNote, setRejectNote] = useState("");
  const [showReject, setShowReject] = useState(false);

  const cfg = PRIORITY_CONFIG[action.priority] || PRIORITY_CONFIG[1];

  return (
    <div style={{
      background: "var(--surface-1)", border: `0.5px solid ${action.priority >= 4 ? cfg.border : "var(--border)"}`,
      borderRadius: 10, marginBottom: 10, overflow: "hidden",
      transition: "border-color .2s",
    }}>
      {/* Header row */}
      <div
        onClick={() => setExpanded(!expanded)}
        style={{ padding: "14px 18px", cursor: "pointer", display: "flex", alignItems: "flex-start", gap: 12 }}
      >
        {/* Priority indicator */}
        <div style={{ width: 36, height: 36, borderRadius: 8, background: cfg.bg, display: "flex", alignItems: "center", justifyContent: "center", fontSize: 16, flexShrink: 0, border: `0.5px solid ${cfg.border}` }}>
          {ACTION_ICONS[action.action_type] || "•"}
        </div>

        {/* Main info */}
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4, flexWrap: "wrap" }}>
            <span style={{ fontWeight: 500, fontSize: 13, color: "var(--text-primary)" }}>{action.customer_name}</span>
            <span style={{ fontSize: 11, color: PLAN_COLORS[action.customer_plan], background: `${PLAN_COLORS[action.customer_plan]}18`, border: `0.5px solid ${PLAN_COLORS[action.customer_plan]}44`, borderRadius: 4, padding: "1px 6px" }}>{action.customer_plan}</span>
            <PriorityBadge priority={action.priority} />
            <ActionTypeLabel type={action.action_type} />
          </div>
          <div style={{ fontSize: 12, color: "var(--text-secondary)", lineHeight: 1.5 }}>
            {action.reason.length > 110 && !expanded ? action.reason.slice(0, 110) + "..." : action.reason}
          </div>
        </div>

        {/* MRR + expand */}
        <div style={{ textAlign: "right", flexShrink: 0 }}>
          <div style={{ fontSize: 14, fontWeight: 500, color: "var(--text-success)" }}>${action.customer_mrr?.toFixed(0)}/mo</div>
          <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2 }}>LTV ${action.ltv_estimate?.toLocaleString()}</div>
          <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 4 }}>{expanded ? "▲ less" : "▼ more"}</div>
        </div>
      </div>

      {/* Expanded detail */}
      {expanded && (
        <div style={{ borderTop: "0.5px solid var(--border)", padding: "14px 18px" }}>
          {/* Scores */}
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginBottom: 14 }}>
            <div>
              <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 4 }}>Churn risk</div>
              <ScoreBar value={action.churn_score || 0} color={action.churn_score > 0.6 ? "#EF4444" : action.churn_score > 0.3 ? "#F97316" : "#22C55E"} />
            </div>
            <div>
              <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 4 }}>Segment</div>
              <div style={{ fontSize: 13, color: "var(--text-primary)" }}>{action.segment}</div>
            </div>
          </div>

          {/* Reason editor */}
          {editMode ? (
            <div style={{ marginBottom: 12 }}>
              <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 6 }}>Edit action reason before approving:</div>
              <textarea
                value={editedReason}
                onChange={e => setEditedReason(e.target.value)}
                style={{
                  width: "100%", minHeight: 80, background: "var(--surface-0)",
                  border: "0.5px solid var(--border-accent)", borderRadius: 6,
                  color: "var(--text-primary)", fontSize: 12, padding: "8px 10px",
                  resize: "vertical", fontFamily: "inherit", lineHeight: 1.5,
                }}
              />
            </div>
          ) : (
            <div style={{ fontSize: 12, color: "var(--text-secondary)", background: "var(--surface-0)", borderRadius: 6, padding: "8px 12px", marginBottom: 12, lineHeight: 1.6 }}>
              {action.reason}
            </div>
          )}

          {/* Reject note */}
          {showReject && (
            <div style={{ marginBottom: 12 }}>
              <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 6 }}>Rejection reason (optional):</div>
              <input
                value={rejectNote}
                onChange={e => setRejectNote(e.target.value)}
                placeholder="e.g. Customer already contacted manually"
                style={{
                  width: "100%", background: "var(--surface-0)",
                  border: "0.5px solid var(--border-danger, #EF4444)", borderRadius: 6,
                  color: "var(--text-primary)", fontSize: 12, padding: "7px 10px",
                  fontFamily: "inherit",
                }}
              />
            </div>
          )}

          {/* Action buttons */}
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <button
              onClick={() => onApprove(action.action_id, editMode ? editedReason : null)}
              style={{ padding: "7px 16px", background: "var(--fill-success, #22C55E)", color: "#fff", border: "none", borderRadius: 6, fontSize: 12, fontWeight: 500, cursor: "pointer" }}
            >
              ✅ Approve
            </button>

            {!editMode ? (
              <button
                onClick={() => setEditMode(true)}
                style={{ padding: "7px 16px", background: "var(--surface-0)", color: "var(--text-secondary)", border: "0.5px solid var(--border)", borderRadius: 6, fontSize: 12, cursor: "pointer" }}
              >
                ✏️ Edit & Approve
              </button>
            ) : (
              <button
                onClick={() => setEditMode(false)}
                style={{ padding: "7px 16px", background: "var(--surface-0)", color: "var(--text-muted)", border: "0.5px solid var(--border)", borderRadius: 6, fontSize: 12, cursor: "pointer" }}
              >
                Cancel Edit
              </button>
            )}

            {!showReject ? (
              <button
                onClick={() => setShowReject(true)}
                style={{ padding: "7px 16px", background: "rgba(239,68,68,0.1)", color: "#EF4444", border: "0.5px solid rgba(239,68,68,0.3)", borderRadius: 6, fontSize: 12, cursor: "pointer" }}
              >
                ❌ Reject
              </button>
            ) : (
              <>
                <button
                  onClick={() => onReject(action.action_id, rejectNote)}
                  style={{ padding: "7px 16px", background: "#EF4444", color: "#fff", border: "none", borderRadius: 6, fontSize: 12, fontWeight: 500, cursor: "pointer" }}
                >
                  Confirm Reject
                </button>
                <button
                  onClick={() => setShowReject(false)}
                  style={{ padding: "7px 16px", background: "var(--surface-0)", color: "var(--text-muted)", border: "0.5px solid var(--border)", borderRadius: 6, fontSize: 12, cursor: "pointer" }}
                >
                  Cancel
                </button>
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// ── Trust mode selector ───────────────────────────────────────────────────────
function TrustModeSelector({ current, onChange }) {
  return (
    <div style={{ background: "var(--surface-1)", border: "0.5px solid var(--border)", borderRadius: 10, padding: "16px 18px", marginBottom: 20 }}>
      <div style={{ fontSize: 11, fontWeight: 500, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: ".07em", marginBottom: 12 }}>
        Automation trust mode
      </div>
      <div style={{ display: "flex", gap: 8 }}>
        {TRUST_MODES.map(m => (
          <button
            key={m.value}
            onClick={() => onChange(m.value)}
            style={{
              flex: 1, padding: "10px 12px", borderRadius: 8, cursor: "pointer", textAlign: "center",
              background: current === m.value ? "var(--bg-accent, rgba(96,165,250,0.12))" : "var(--surface-0)",
              border: current === m.value ? "0.5px solid var(--border-accent, rgba(96,165,250,0.4))" : "0.5px solid var(--border)",
              transition: ".15s",
            }}
          >
            <div style={{ fontSize: 12, fontWeight: current === m.value ? 600 : 400, color: current === m.value ? "var(--text-accent)" : "var(--text-primary)", marginBottom: 3 }}>{m.label}</div>
            <div style={{ fontSize: 10, color: "var(--text-muted)" }}>{m.desc}</div>
          </button>
        ))}
      </div>
    </div>
  );
}

// ── Main App ──────────────────────────────────────────────────────────────────
export default function ApprovalQueue() {
  const [queue, setQueue]         = useState(MOCK_QUEUE);
  const [stats, setStats]         = useState(MOCK_STATS);
  const [trustMode, setTrustMode] = useState("full_review");
  const [toasts, setToasts]       = useState([]);
  const [filter, setFilter]       = useState("all");

  const addToast = (msg, type = "success") => {
    const id = Date.now();
    setToasts(t => [...t, { id, msg, type }]);
    setTimeout(() => setToasts(t => t.filter(x => x.id !== id)), 3500);
  };

  const handleApprove = (action_id, editedReason) => {
    setQueue(q => q.filter(a => a.action_id !== action_id));
    setStats(s => ({ ...s, pending_approval: s.pending_approval - 1, approved_today: s.approved_today + 1 }));
    addToast(editedReason ? "✅ Approved with edited reason" : "✅ Action approved — queued for execution", "success");
  };

  const handleReject = (action_id, note) => {
    setQueue(q => q.filter(a => a.action_id !== action_id));
    setStats(s => ({ ...s, pending_approval: s.pending_approval - 1, rejected_today: s.rejected_today + 1 }));
    addToast(`❌ Action rejected${note ? `: ${note}` : ""}`, "danger");
  };

  const handleApproveAll = () => {
    const count = queue.length;
    setQueue([]);
    setStats(s => ({ ...s, pending_approval: 0, approved_today: s.approved_today + count }));
    addToast(`✅ All ${count} actions approved`, "success");
  };

  const filteredQueue = filter === "all" ? queue :
    queue.filter(a => {
      if (filter === "critical") return a.priority >= 4;
      if (filter === "email")    return a.action_type.includes("email") || a.action_type.includes("nurture");
      if (filter === "calls")    return a.action_type.includes("call") || a.action_type.includes("csm");
      return true;
    });

  const FILTER_TABS = [
    { id: "all",      label: `All (${queue.length})` },
    { id: "critical", label: "🚨 Critical/High" },
    { id: "email",    label: "📧 Email actions" },
    { id: "calls",    label: "📞 Calls/CSM" },
  ];

  return (
    <div style={{ minHeight: "100vh", background: "var(--bg, #0B0F19)", color: "var(--text-primary, #F9FAFB)", fontFamily: "var(--font-sans, system-ui)", padding: "0 0 40px" }}>

      {/* Header */}
      <div style={{ borderBottom: "0.5px solid var(--border)", padding: "16px 24px", display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <div>
          <div style={{ fontSize: 16, fontWeight: 500 }}><span style={{ color: "var(--text-accent, #60A5FA)" }}>ACIA</span> · Human Approval Queue</div>
          <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2 }}>Review and approve AI-generated actions before execution</div>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          {queue.length > 0 && (
            <button
              onClick={handleApproveAll}
              style={{ padding: "7px 16px", background: "var(--fill-success, #22C55E)", color: "#fff", border: "none", borderRadius: 6, fontSize: 12, fontWeight: 500, cursor: "pointer" }}
            >
              ✅ Approve All ({queue.length})
            </button>
          )}
        </div>
      </div>

      <div style={{ padding: "20px 24px" }}>

        {/* Stats row */}
        <div style={{ display: "grid", gridTemplateColumns: "repeat(4,1fr)", gap: 12, marginBottom: 20 }}>
          <StatCard label="Pending review"  value={stats.pending_approval} color="var(--text-warning, #EAB308)" icon="⏳" />
          <StatCard label="Approved today"  value={stats.approved_today}   color="var(--text-success, #22C55E)" icon="✅" />
          <StatCard label="Rejected today"  value={stats.rejected_today}   color="var(--text-danger, #EF4444)"  icon="❌" />
          <StatCard label="Auto-execute in" value="3 hrs"                  color="var(--text-muted)"             icon="⏱️" />
        </div>

        {/* Trust mode selector */}
        <TrustModeSelector current={trustMode} onChange={setTrustMode} />

        {/* Priority breakdown */}
        {Object.keys(stats.by_priority).length > 0 && (
          <div style={{ display: "flex", gap: 8, marginBottom: 16, flexWrap: "wrap" }}>
            {Object.entries(stats.by_priority).map(([label, count]) => (
              <div key={label} style={{ background: "var(--surface-1)", border: "0.5px solid var(--border)", borderRadius: 6, padding: "4px 12px", fontSize: 12, color: "var(--text-secondary)" }}>
                <span style={{ color: label === "Critical" ? "#EF4444" : label === "High" ? "#F97316" : label === "Medium" ? "#EAB308" : "#22C55E" }}>{label}</span>: {count}
              </div>
            ))}
          </div>
        )}

        {/* Filter tabs */}
        <div style={{ display: "flex", gap: 4, marginBottom: 16, borderBottom: "0.5px solid var(--border)", paddingBottom: 0 }}>
          {FILTER_TABS.map(t => (
            <button
              key={t.id}
              onClick={() => setFilter(t.id)}
              style={{
                padding: "7px 14px", background: "transparent", border: "none",
                borderBottom: filter === t.id ? "2px solid var(--fill-accent, #60A5FA)" : "2px solid transparent",
                color: filter === t.id ? "var(--text-accent, #60A5FA)" : "var(--text-muted)",
                fontSize: 12, fontWeight: filter === t.id ? 600 : 400, cursor: "pointer",
                marginBottom: -0.5,
              }}
            >
              {t.label}
            </button>
          ))}
        </div>

        {/* Queue */}
        {filteredQueue.length === 0 ? (
          <div style={{ textAlign: "center", padding: "48px 0", color: "var(--text-muted)" }}>
            <div style={{ fontSize: 32, marginBottom: 12 }}>✅</div>
            <div style={{ fontSize: 14, color: "var(--text-secondary)" }}>No actions pending review</div>
            <div style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 6 }}>The agent will queue new actions on the next cycle</div>
          </div>
        ) : (
          filteredQueue.map(action => (
            <ActionCard
              key={action.action_id}
              action={action}
              onApprove={handleApprove}
              onReject={handleReject}
              onEdit={() => {}}
            />
          ))
        )}
      </div>

      {/* Toast notifications */}
      <div style={{ position: "fixed", bottom: 20, right: 20, display: "flex", flexDirection: "column", gap: 8, zIndex: 999 }}>
        {toasts.map(t => (
          <div key={t.id} style={{
            padding: "10px 16px", borderRadius: 8, fontSize: 13, fontWeight: 500,
            background: t.type === "success" ? "rgba(34,197,94,0.15)" : "rgba(239,68,68,0.15)",
            border: `0.5px solid ${t.type === "success" ? "rgba(34,197,94,0.4)" : "rgba(239,68,68,0.4)"}`,
            color: t.type === "success" ? "#22C55E" : "#EF4444",
            maxWidth: 320,
          }}>
            {t.msg}
          </div>
        ))}
      </div>
    </div>
  );
}
