import { useState } from "react";
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, PieChart, Pie, Cell, LineChart, Line, CartesianGrid, Legend } from "recharts";

// ── Data sourced from ACIA SQLite DB ─────────────────────────────────────────
const DB = {
  kpis: {
    totalCustomers: 500, activeCustomers: 422, churnedCustomers: 78,
    monthlyMRR: 76099.99, totalLTV: 1834609.85, hotConversions: 180,
    churnRate: 15.6, actionsQueued: 243, successRate: 45,
  },
  segments: [
    { name: "Champion",  count: 121, color: "#6EE7B7" },
    { name: "Loyal",     count: 77,  color: "#60A5FA" },
    { name: "At-Risk",   count: 124, color: "#FCD34D" },
    { name: "Prospect",  count: 105, color: "#A78BFA" },
    { name: "Lost",      count: 73,  color: "#F87171" },
  ],
  churnRisk: [
    { label: "Low (<30%)",    count: 419, color: "#6EE7B7" },
    { label: "Medium (30-60%)", count: 5, color: "#FCD34D" },
    { label: "High (>60%)",   count: 76,  color: "#F87171" },
  ],
  topChurnRisk: [
    { id: "C0288", plan: "Enterprise", mrr: 957.04, score: 0.478, segment: "Loyal" },
    { id: "C0356", plan: "Starter",    mrr: 65.44,  score: 0.372, segment: "Lost" },
    { id: "C0481", plan: "Pro",        mrr: 261.51, score: 0.290, segment: "Lost" },
    { id: "C0272", plan: "Pro",        mrr: 214.74, score: 0.282, segment: "Prospect" },
    { id: "C0212", plan: "Free",       mrr: 0,      score: 0.281, segment: "Lost" },
    { id: "C0259", plan: "Starter",    mrr: 53.72,  score: 0.196, segment: "Prospect" },
  ],
  actionOutcomes: [
    { outcome: "Issue Resolved",     count: 100 },
    { outcome: "No Answer",          count: 65 },
    { outcome: "Callback Scheduled", count: 28 },
    { outcome: "Escalated CSM",      count: 15 },
    { outcome: "No Response",        count: 12 },
    { outcome: "Opened Email",       count: 11 },
    { outcome: "Replied Positive",   count: 6 },
    { outcome: "Replied Negative",   count: 6 },
  ],
  recentActions: [
    { id: "C0001", type: "proactive_support_call", priority: 4, outcome: "issue_resolved" },
    { id: "C0002", type: "proactive_support_call", priority: 4, outcome: "no_answer" },
    { id: "C0004", type: "proactive_support_call", priority: 4, outcome: "callback_scheduled" },
    { id: "C0005", type: "proactive_support_call", priority: 4, outcome: "issue_resolved" },
    { id: "C0006", type: "proactive_support_call", priority: 4, outcome: "escalated_csm" },
    { id: "C0007", type: "send_health_checkin",    priority: 1, outcome: "replied_positive" },
    { id: "C0008", type: "send_health_checkin",    priority: 1, outcome: "no_response" },
    { id: "C0009", type: "proactive_support_call", priority: 4, outcome: "issue_resolved" },
  ],
  mrr_trend: [
    { month: "Feb", mrr: 68200 }, { month: "Mar", mrr: 71500 },
    { month: "Apr", mrr: 73800 }, { month: "May", mrr: 74900 },
    { month: "Jun", mrr: 75600 }, { month: "Jul", mrr: 76100 },
  ],
};

// ── Helpers ───────────────────────────────────────────────────────────────────
const fmt = {
  money: v => v >= 1e6 ? `$${(v/1e6).toFixed(2)}M` : v >= 1e3 ? `$${(v/1e3).toFixed(1)}K` : `$${v.toFixed(0)}`,
  pct:   v => `${v.toFixed(1)}%`,
  score: v => (v * 100).toFixed(0),
};

const PRIORITY_COLOR = { 5: "#EF4444", 4: "#F97316", 3: "#EAB308", 2: "#22C55E", 1: "#6B7280" };
const PRIORITY_LABEL = { 5: "CRITICAL", 4: "HIGH", 3: "MEDIUM", 2: "LOW", 1: "INFO" };

const PLAN_COLOR = { Enterprise: "#A78BFA", Pro: "#60A5FA", Starter: "#34D399", Free: "#9CA3AF" };

const OUTCOME_ICON = {
  issue_resolved: "✅", no_answer: "📵", callback_scheduled: "📅",
  escalated_csm: "🚨", no_response: "🔕", replied_positive: "😊",
  replied_negative: "😞", opened_only: "📧",
};

// ── Sub-components ────────────────────────────────────────────────────────────
function KpiCard({ label, value, sub, accent, icon }) {
  return (
    <div style={{
      background: "rgba(255,255,255,0.04)", border: "1px solid rgba(255,255,255,0.09)",
      borderRadius: 12, padding: "20px 22px", display: "flex", flexDirection: "column", gap: 6,
    }}>
      <div style={{ fontSize: 12, color: "#6B7280", letterSpacing: "0.08em", textTransform: "uppercase", display:"flex", alignItems:"center", gap:6 }}>
        <span>{icon}</span>{label}
      </div>
      <div style={{ fontSize: 28, fontWeight: 700, color: accent || "#F9FAFB", lineHeight: 1.1 }}>{value}</div>
      {sub && <div style={{ fontSize: 12, color: "#9CA3AF" }}>{sub}</div>}
    </div>
  );
}

function SectionTitle({ children }) {
  return (
    <div style={{ fontSize: 11, fontWeight: 700, letterSpacing: "0.1em", textTransform: "uppercase", color: "#6B7280", marginBottom: 14 }}>
      {children}
    </div>
  );
}

function Badge({ label, color }) {
  return (
    <span style={{ background: color + "22", color, border: `1px solid ${color}44`, borderRadius: 4, fontSize: 10, fontWeight: 700, padding: "2px 7px", letterSpacing: "0.06em" }}>
      {label}
    </span>
  );
}

// ── Tab content ───────────────────────────────────────────────────────────────
function OverviewTab() {
  const s = DB.kpis;
  const totalSeg = DB.segments.reduce((a,b) => a + b.count, 0);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 28 }}>
      {/* KPI row */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(3,1fr)", gap: 14 }}>
        <KpiCard label="Active Customers" value={s.activeCustomers.toLocaleString()} sub={`${s.churnedCustomers} churned (${fmt.pct(s.churnRate)})`} icon="👥" accent="#60A5FA" />
        <KpiCard label="Monthly MRR"      value={fmt.money(s.monthlyMRR)} sub="Active accounts only" icon="💰" accent="#6EE7B7" />
        <KpiCard label="Estimated LTV"    value={fmt.money(s.totalLTV)}   sub="Across all customers" icon="📈" accent="#A78BFA" />
        <KpiCard label="High-Risk Churn"  value={DB.churnRisk[2].count}   sub="Score > 60% — urgent" icon="🚨" accent="#F87171" />
        <KpiCard label="Hot Conversions"  value={s.hotConversions}        sub="Score > 70% — upsell now" icon="🔥" accent="#FCD34D" />
        <KpiCard label="Actions Executed" value={s.actionsQueued}         sub={`${s.successRate}% success rate`} icon="⚡" accent="#34D399" />
      </div>

      {/* MRR trend + Segment pie */}
      <div style={{ display: "grid", gridTemplateColumns: "1.4fr 1fr", gap: 18 }}>
        <div style={{ background: "rgba(255,255,255,0.03)", border: "1px solid rgba(255,255,255,0.08)", borderRadius: 12, padding: 20 }}>
          <SectionTitle>MRR Trend (6 months)</SectionTitle>
          <ResponsiveContainer width="100%" height={180}>
            <LineChart data={DB.mrr_trend} margin={{ top: 4, right: 10, left: 0, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.06)" />
              <XAxis dataKey="month" tick={{ fill: "#6B7280", fontSize: 11 }} axisLine={false} tickLine={false} />
              <YAxis tickFormatter={v => `$${(v/1000).toFixed(0)}K`} tick={{ fill: "#6B7280", fontSize: 10 }} axisLine={false} tickLine={false} width={46} />
              <Tooltip formatter={v => [fmt.money(v), "MRR"]} contentStyle={{ background: "#111827", border: "1px solid #374151", borderRadius: 8, fontSize: 12 }} />
              <Line type="monotone" dataKey="mrr" stroke="#60A5FA" strokeWidth={2.5} dot={{ fill: "#60A5FA", r: 4 }} />
            </LineChart>
          </ResponsiveContainer>
        </div>

        <div style={{ background: "rgba(255,255,255,0.03)", border: "1px solid rgba(255,255,255,0.08)", borderRadius: 12, padding: 20 }}>
          <SectionTitle>Customer Segments</SectionTitle>
          <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
            <PieChart width={120} height={120}>
              <Pie data={DB.segments} dataKey="count" cx={55} cy={55} innerRadius={32} outerRadius={55} paddingAngle={2} stroke="none">
                {DB.segments.map((s, i) => <Cell key={i} fill={s.color} />)}
              </Pie>
            </PieChart>
            <div style={{ display: "flex", flexDirection: "column", gap: 7, flex: 1 }}>
              {DB.segments.map(s => (
                <div key={s.name} style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                    <div style={{ width: 8, height: 8, borderRadius: 2, background: s.color }} />
                    <span style={{ fontSize: 12, color: "#D1D5DB" }}>{s.name}</span>
                  </div>
                  <span style={{ fontSize: 12, color: "#9CA3AF" }}>{s.count}</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function ChurnTab() {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 24 }}>
      {/* Risk buckets */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(3,1fr)", gap: 14 }}>
        {DB.churnRisk.map(r => (
          <div key={r.label} style={{ background: "rgba(255,255,255,0.03)", border: `1px solid ${r.color}33`, borderRadius: 12, padding: 20, textAlign: "center" }}>
            <div style={{ fontSize: 36, fontWeight: 800, color: r.color }}>{r.count}</div>
            <div style={{ fontSize: 12, color: "#9CA3AF", marginTop: 4 }}>{r.label}</div>
          </div>
        ))}
      </div>

      {/* Top at-risk table */}
      <div style={{ background: "rgba(255,255,255,0.03)", border: "1px solid rgba(255,255,255,0.08)", borderRadius: 12, padding: 20 }}>
        <SectionTitle>Highest Churn Risk — Active Customers</SectionTitle>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
          <thead>
            <tr style={{ borderBottom: "1px solid rgba(255,255,255,0.08)" }}>
              {["Customer", "Plan", "MRR", "Churn Score", "Segment", "Action"].map(h => (
                <th key={h} style={{ padding: "8px 10px", color: "#6B7280", fontWeight: 600, textAlign: "left", fontSize: 11, textTransform: "uppercase", letterSpacing: "0.06em" }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {DB.topChurnRisk.map((r, i) => (
              <tr key={r.id} style={{ borderBottom: "1px solid rgba(255,255,255,0.05)" }}>
                <td style={{ padding: "10px 10px", color: "#F9FAFB", fontWeight: 600 }}>{r.id}</td>
                <td style={{ padding: "10px 10px" }}><Badge label={r.plan} color={PLAN_COLOR[r.plan] || "#9CA3AF"} /></td>
                <td style={{ padding: "10px 10px", color: "#6EE7B7" }}>{fmt.money(r.mrr)}</td>
                <td style={{ padding: "10px 10px" }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <div style={{ flex: 1, height: 6, background: "rgba(255,255,255,0.08)", borderRadius: 3 }}>
                      <div style={{ width: `${r.score * 100}%`, height: "100%", background: r.score > 0.6 ? "#EF4444" : r.score > 0.3 ? "#F97316" : "#6EE7B7", borderRadius: 3 }} />
                    </div>
                    <span style={{ color: r.score > 0.6 ? "#F87171" : r.score > 0.3 ? "#FCD34D" : "#6EE7B7", fontWeight: 700, minWidth: 36, fontSize: 12 }}>{fmt.pct(r.score * 100)}</span>
                  </div>
                </td>
                <td style={{ padding: "10px 10px", color: "#9CA3AF" }}>{r.segment}</td>
                <td style={{ padding: "10px 10px" }}>
                  <span style={{ fontSize: 11, color: "#60A5FA", cursor: "pointer" }}>
                    {r.score > 0.4 ? "🚨 Escalate CSM" : r.score > 0.3 ? "📧 Retention Email" : "🩺 Health Check"}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function ActionsTab() {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 24 }}>
      {/* Outcome bar chart */}
      <div style={{ background: "rgba(255,255,255,0.03)", border: "1px solid rgba(255,255,255,0.08)", borderRadius: 12, padding: 20 }}>
        <SectionTitle>Execution Outcomes (Last Cycle)</SectionTitle>
        <ResponsiveContainer width="100%" height={200}>
          <BarChart data={DB.actionOutcomes} layout="vertical" margin={{ top: 0, right: 20, left: 10, bottom: 0 }}>
            <XAxis type="number" tick={{ fill: "#6B7280", fontSize: 11 }} axisLine={false} tickLine={false} />
            <YAxis type="category" dataKey="outcome" tick={{ fill: "#D1D5DB", fontSize: 11 }} axisLine={false} tickLine={false} width={140} />
            <Tooltip contentStyle={{ background: "#111827", border: "1px solid #374151", borderRadius: 8, fontSize: 12 }} />
            <Bar dataKey="count" fill="#60A5FA" radius={[0, 4, 4, 0]}>
              {DB.actionOutcomes.map((e, i) => (
                <Cell key={i} fill={
                  ["Issue Resolved","Replied Positive","Callback Scheduled"].includes(e.outcome) ? "#6EE7B7" :
                  ["No Answer","No Response"].includes(e.outcome) ? "#6B7280" :
                  e.outcome === "Escalated CSM" ? "#F87171" : "#60A5FA"
                } />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>

      {/* Recent action log */}
      <div style={{ background: "rgba(255,255,255,0.03)", border: "1px solid rgba(255,255,255,0.08)", borderRadius: 12, padding: 20 }}>
        <SectionTitle>Recent Action Log</SectionTitle>
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          {DB.recentActions.map((a, i) => (
            <div key={i} style={{ display: "flex", alignItems: "center", gap: 12, padding: "10px 14px", background: "rgba(255,255,255,0.02)", borderRadius: 8, border: "1px solid rgba(255,255,255,0.05)" }}>
              <div style={{ width: 32, height: 32, borderRadius: 6, background: (PRIORITY_COLOR[a.priority] || "#6B7280") + "22", display: "flex", alignItems: "center", justifyContent: "center", fontSize: 14, border: `1px solid ${PRIORITY_COLOR[a.priority]}44` }}>
                {OUTCOME_ICON[a.outcome] || "•"}
              </div>
              <div style={{ flex: 1 }}>
                <div style={{ fontSize: 13, color: "#F9FAFB", fontWeight: 600 }}>{a.id}</div>
                <div style={{ fontSize: 11, color: "#6B7280", marginTop: 2 }}>{a.type.replace(/_/g, " ")}</div>
              </div>
              <Badge label={PRIORITY_LABEL[a.priority]} color={PRIORITY_COLOR[a.priority]} />
              <div style={{ fontSize: 11, color: "#9CA3AF", minWidth: 90, textAlign: "right" }}>{a.outcome.replace(/_/g, " ")}</div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function PipelineTab() {
  const steps = [
    { n: "1", label: "Data Ingestion",       desc: "500 customers · 5 tables · 28 features", status: "done",    icon: "🗄️" },
    { n: "2", label: "ML Models",             desc: "Churn · Conversion · Segmentation",      status: "done",    icon: "🧠" },
    { n: "3", label: "Rule Engine",           desc: "8 rules · 281 candidates fired",          status: "done",    icon: "⚙️" },
    { n: "4", label: "LLM Planner",           desc: "Claude API · conflict resolution",        status: "done",    icon: "🤖" },
    { n: "5", label: "Action Scheduler",      desc: "Priority queue · cooldown logic",         status: "done",    icon: "📋" },
    { n: "6", label: "Executor + Templates",  desc: "243 actions · 8 outcome types",           status: "done",    icon: "⚡" },
    { n: "7", label: "Feedback Loop",         desc: "Health score updates · retraining flags", status: "done",    icon: "🔄" },
    { n: "8", label: "Dashboard",             desc: "You are here",                            status: "active",  icon: "📊" },
  ];

  const modelStats = [
    { name: "Churn Predictor",      algo: "Random Forest",    auc: "0.9985", features: 28 },
    { name: "Conversion Scorer",    algo: "Logistic Reg.",    auc: "1.0000", features: 27 },
    { name: "Segment Classifier",   algo: "KMeans (k=5)",     auc: "Sil. 0.23", features: 6 },
  ];

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 24 }}>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 18 }}>
        {/* Pipeline steps */}
        <div style={{ background: "rgba(255,255,255,0.03)", border: "1px solid rgba(255,255,255,0.08)", borderRadius: 12, padding: 20 }}>
          <SectionTitle>Pipeline Steps</SectionTitle>
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {steps.map((s) => (
              <div key={s.n} style={{ display: "flex", alignItems: "flex-start", gap: 12, padding: "10px 12px", borderRadius: 8, background: s.status === "active" ? "rgba(96,165,250,0.08)" : "transparent", border: s.status === "active" ? "1px solid rgba(96,165,250,0.25)" : "1px solid transparent" }}>
                <div style={{ width: 28, height: 28, borderRadius: 6, background: s.status === "done" ? "#6EE7B722" : "#60A5FA22", display: "flex", alignItems: "center", justifyContent: "center", fontSize: 13, flexShrink: 0 }}>
                  {s.status === "done" ? "✅" : s.icon}
                </div>
                <div>
                  <div style={{ fontSize: 13, color: "#F9FAFB", fontWeight: 600 }}>{s.label}</div>
                  <div style={{ fontSize: 11, color: "#6B7280", marginTop: 2 }}>{s.desc}</div>
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* Model stats */}
        <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          <div style={{ background: "rgba(255,255,255,0.03)", border: "1px solid rgba(255,255,255,0.08)", borderRadius: 12, padding: 20 }}>
            <SectionTitle>Trained Models</SectionTitle>
            <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
              {modelStats.map(m => (
                <div key={m.name} style={{ padding: "12px 14px", background: "rgba(255,255,255,0.03)", borderRadius: 8, border: "1px solid rgba(255,255,255,0.06)" }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                    <div style={{ fontSize: 13, color: "#F9FAFB", fontWeight: 600 }}>{m.name}</div>
                    <span style={{ fontSize: 11, color: "#6EE7B7", fontWeight: 700 }}>AUC {m.auc}</span>
                  </div>
                  <div style={{ fontSize: 11, color: "#6B7280", marginTop: 4 }}>{m.algo} · {m.features} features</div>
                </div>
              ))}
            </div>
          </div>

          <div style={{ background: "rgba(255,255,255,0.03)", border: "1px solid rgba(255,255,255,0.08)", borderRadius: 12, padding: 20 }}>
            <SectionTitle>Data Summary</SectionTitle>
            {[
              ["Customers", "500"],
              ["Transactions", "~4,200"],
              ["Engagement Events", "~13,000"],
              ["Support Tickets", "~1,200"],
              ["Email Records", "~3,300"],
              ["DB Tables", "7"],
            ].map(([k, v]) => (
              <div key={k} style={{ display: "flex", justifyContent: "space-between", padding: "6px 0", borderBottom: "1px solid rgba(255,255,255,0.05)", fontSize: 12 }}>
                <span style={{ color: "#9CA3AF" }}>{k}</span>
                <span style={{ color: "#F9FAFB", fontWeight: 600 }}>{v}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

// ── Main App ──────────────────────────────────────────────────────────────────
const TABS = [
  { id: "overview",  label: "Overview",  icon: "📊" },
  { id: "churn",     label: "Churn Risk",icon: "🚨" },
  { id: "actions",   label: "Actions",   icon: "⚡" },
  { id: "pipeline",  label: "Pipeline",  icon: "🔬" },
];

export default function ACIADashboard() {
  const [tab, setTab] = useState("overview");

  const content = {
    overview: <OverviewTab />,
    churn:    <ChurnTab />,
    actions:  <ActionsTab />,
    pipeline: <PipelineTab />,
  };

  return (
    <div style={{
      minHeight: "100vh", background: "#0B0F19", color: "#F9FAFB",
      fontFamily: "'Inter', system-ui, -apple-system, sans-serif",
      padding: "0 0 40px",
    }}>
      {/* Header */}
      <div style={{ borderBottom: "1px solid rgba(255,255,255,0.08)", padding: "18px 28px", display: "flex", alignItems: "center", justifyContent: "space-between", background: "rgba(0,0,0,0.3)" }}>
        <div>
          <div style={{ fontSize: 18, fontWeight: 800, letterSpacing: "-0.02em", color: "#F9FAFB" }}>
            <span style={{ color: "#60A5FA" }}>ACIA</span> · Autonomous CRM Intelligence Agent
          </div>
          <div style={{ fontSize: 11, color: "#6B7280", marginTop: 2 }}>Powered by ML + Claude API · Last cycle: just now</div>
        </div>
        <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
          <div style={{ width: 8, height: 8, borderRadius: "50%", background: "#6EE7B7", boxShadow: "0 0 8px #6EE7B7" }} />
          <span style={{ fontSize: 12, color: "#6EE7B7", fontWeight: 600 }}>Agent Active</span>
        </div>
      </div>

      {/* Tabs */}
      <div style={{ display: "flex", gap: 4, padding: "16px 28px 0", borderBottom: "1px solid rgba(255,255,255,0.06)" }}>
        {TABS.map(t => (
          <button key={t.id} onClick={() => setTab(t.id)} style={{
            background: tab === t.id ? "rgba(96,165,250,0.12)" : "transparent",
            border: tab === t.id ? "1px solid rgba(96,165,250,0.35)" : "1px solid transparent",
            color: tab === t.id ? "#60A5FA" : "#6B7280",
            borderRadius: "8px 8px 0 0", padding: "8px 18px", cursor: "pointer",
            fontSize: 13, fontWeight: tab === t.id ? 700 : 400, display: "flex", alignItems: "center", gap: 6,
            transition: "all 0.15s",
          }}>
            <span>{t.icon}</span>{t.label}
          </button>
        ))}
      </div>

      {/* Content */}
      <div style={{ padding: "24px 28px" }}>
        {content[tab]}
      </div>
    </div>
  );
}
