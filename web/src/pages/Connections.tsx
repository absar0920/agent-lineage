import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { api, type Connector, type ConnectorTest } from "../api";
import { Badge, ErrorPanel, Modal, Spinner, relTime, useToast } from "../components/ui";

const KIND_LABEL: Record<string, string> = { commerce: "Commerce", helpdesk: "Helpdesk", payments: "Payments", shipping: "Shipping" };

const FIELDS: Record<string, { key: string; label: string; secret?: boolean; placeholder?: string }[]> = {
  commerce: [
    { key: "store_url", label: "Store URL", placeholder: "https://your-store.example" },
    { key: "api_key", label: "API key", secret: true },
    { key: "api_secret", label: "API secret", secret: true },
  ],
  helpdesk: [
    { key: "subdomain", label: "Account subdomain", placeholder: "yourcompany" },
    { key: "api_token", label: "API token", secret: true },
  ],
  payments: [{ key: "secret_key", label: "Secret key", secret: true }],
  shipping: [
    { key: "api_key", label: "API key", secret: true },
    { key: "api_secret", label: "API secret", secret: true },
  ],
};

export default function Connections() {
  const q = useQuery({ queryKey: ["connectors"], queryFn: api.connectors });
  const [configuring, setConfiguring] = useState<Connector | null>(null);
  const [saved, setSaved] = useState<Set<string>>(new Set());

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>Connections</h1>
          <p className="muted">Systems the support agent reads from and writes to. Every connector call is captured in runs.</p>
        </div>
      </header>
      {q.error && <ErrorPanel error={q.error} />}
      {q.isLoading && <Spinner label="Loading connectors…" />}
      {q.data && (
        <>
          <h3 className="section-title">Connected</h3>
          <div className="cards">
            {q.data.filter((c) => c.connected).map((c) => (
              <ConnectorCard key={c.name} c={c} />
            ))}
          </div>
          <h3 className="section-title">Available</h3>
          <div className="cards">
            {q.data.filter((c) => !c.connected).map((c) => (
              <ConnectorCard key={c.name} c={c} saved={saved.has(c.name)} onConnect={() => setConfiguring(c)} />
            ))}
          </div>
        </>
      )}
      {configuring && (
        <ConnectModal
          c={configuring}
          onClose={() => setConfiguring(null)}
          onSaved={() => {
            setSaved((s) => new Set(s).add(configuring.name));
            setConfiguring(null);
          }}
        />
      )}
    </div>
  );
}

function ConnectorCard({ c, saved, onConnect }: { c: Connector; saved?: boolean; onConnect?: () => void }) {
  const [result, setResult] = useState<ConnectorTest | null>(null);
  const toast = useToast();
  const test = useMutation({ mutationFn: () => api.testConnector(c.name), onSuccess: setResult, onError: toast.error });
  return (
    <div className={`card connector ${c.connected ? "connected" : ""}`}>
      <div className="connector-head">
        <div className={`logo logo-${c.name}`}>{c.display_name[0]}</div>
        <div>
          <div className="connector-name">{c.display_name}</div>
          <div className="muted small">{KIND_LABEL[c.kind] ?? c.kind}</div>
        </div>
        <div className="spacer" />
        {c.connected ? <Badge tone="ok">● Connected</Badge> : saved ? <Badge tone="info">Saved</Badge> : <Badge>Not connected</Badge>}
      </div>
      {c.connected ? (
        <>
          <div className="connector-meta">
            <Badge tone="violet" title="This connection uses the built-in mock adapter backed by the store database">
              adapter · {c.adapter}
            </Badge>
            <span className="muted small">Last sync {relTime(c.last_sync_at)}</span>
          </div>
          <dl className="records">
            {Object.entries(c.records).map(([k, v]) => (
              <div key={k}>
                <dt>{k.replace(/_/g, " ")}</dt>
                <dd>{v.toLocaleString()}</dd>
              </div>
            ))}
          </dl>
          <div className="connector-actions">
            <button className="btn btn-sm" onClick={() => test.mutate()} disabled={test.isPending}>
              {test.isPending ? "Testing…" : "Test connection"}
            </button>
            {result && (
              <span className={`small ${result.ok ? "ok-text" : "error-text"}`}>
                {result.ok ? `✓ ${result.message}${result.latency_ms !== undefined ? ` · ${result.latency_ms} ms` : ""}` : `✕ ${result.message}`}
              </span>
            )}
          </div>
        </>
      ) : (
        <div className="connector-actions">
          {saved ? (
            <span className="muted small">Saved. The adapter for {c.display_name} is coming soon; the connection activates automatically once it ships.</span>
          ) : (
            <button className="btn btn-sm" onClick={onConnect}>
              Connect
            </button>
          )}
        </div>
      )}
    </div>
  );
}

function ConnectModal({ c, onClose, onSaved }: { c: Connector; onClose: () => void; onSaved: () => void }) {
  const fields = FIELDS[c.kind] ?? FIELDS.commerce;
  const [values, setValues] = useState<Record<string, string>>({});
  const toast = useToast();
  const qc = useQueryClient();
  const save = useMutation({
    mutationFn: () => api.configureConnector(c.name, values),
    onSuccess: (res) => {
      toast.ok(res.enabled ? `${c.display_name} connected` : `${c.display_name} settings saved`);
      void qc.invalidateQueries({ queryKey: ["connectors"] });
      onSaved();
    },
    onError: toast.error,
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    save.mutate();
  };
  return (
    <Modal title={`Connect ${c.display_name}`} onClose={onClose}>
      <form onSubmit={submit}>
        {fields.map((f) => (
          <label key={f.key} className="field">
            <span>{f.label}</span>
            <input
              type={f.secret ? "password" : "text"}
              autoComplete="off"
              placeholder={f.placeholder}
              value={values[f.key] ?? ""}
              onChange={(e) => setValues({ ...values, [f.key]: e.target.value })}
              required
            />
          </label>
        ))}
        <p className="muted small">Credentials are stored server-side and never shown again.</p>
        <div className="modal-actions">
          <button type="button" className="btn" onClick={onClose}>
            Cancel
          </button>
          <button className="btn btn-primary" disabled={save.isPending}>
            {save.isPending ? "Saving…" : "Save"}
          </button>
        </div>
      </form>
    </Modal>
  );
}
