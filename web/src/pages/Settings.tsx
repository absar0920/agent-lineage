import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, type FormEvent } from "react";
import { api } from "../api";
import { Badge, CopyButton, ErrorPanel, Spinner, dateTime, relTime, useToast } from "../components/ui";

export default function Settings() {
  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>Settings</h1>
          <p className="muted">Retention, ingest API keys and operator accounts.</p>
        </div>
      </header>
      <Retention />
      <ApiKeys />
      <Users />
    </div>
  );
}

function Retention() {
  const q = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const [days, setDays] = useState("");
  const toast = useToast();
  const qc = useQueryClient();
  useEffect(() => {
    if (q.data) setDays(String(q.data.retention_days));
  }, [q.data]);
  const save = useMutation({
    mutationFn: () => api.saveSettings(Number(days)),
    onSuccess: () => {
      toast.ok("Retention updated");
      void qc.invalidateQueries({ queryKey: ["settings"] });
    },
    onError: toast.error,
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    save.mutate();
  };
  return (
    <section className="card section">
      <h3>Data retention</h3>
      <p className="muted">Runs older than this are deleted automatically. Pinned runs are always kept.</p>
      {q.error && <ErrorPanel error={q.error} />}
      <form className="inline-form" onSubmit={submit}>
        <input type="number" min={1} max={3650} value={days} onChange={(e) => setDays(e.target.value)} aria-label="Retention days" />
        <span className="muted">days</span>
        <button className="btn btn-primary btn-sm" disabled={save.isPending || !days || Number(days) === q.data?.retention_days}>
          Save
        </button>
      </form>
    </section>
  );
}

function ApiKeys() {
  const q = useQuery({ queryKey: ["api-keys"], queryFn: api.apiKeys });
  const [name, setName] = useState("");
  const [created, setCreated] = useState<{ name: string; key: string } | null>(null);
  const toast = useToast();
  const qc = useQueryClient();
  const create = useMutation({
    mutationFn: () => api.createApiKey(name.trim()),
    onSuccess: (res) => {
      setCreated({ name: res.name, key: res.key });
      setName("");
      void qc.invalidateQueries({ queryKey: ["api-keys"] });
    },
    onError: toast.error,
  });
  const revoke = useMutation({
    mutationFn: (id: string) => api.revokeApiKey(id),
    onSuccess: () => {
      toast.ok("Key revoked");
      void qc.invalidateQueries({ queryKey: ["api-keys"] });
    },
    onError: toast.error,
  });
  return (
    <section className="card section">
      <h3>Ingest API keys</h3>
      <p className="muted">
        Instrumented agents send runs with <span className="mono">Authorization: Bearer &lt;key&gt;</span>. Keys are stored hashed.
      </p>
      {created && (
        <div className="new-key">
          <div>
            <strong>{created.name}</strong>: copy this key now. It won't be shown again.
          </div>
          <div className="key-row">
            <code className="mono">{created.key}</code>
            <CopyButton text={created.key} />
            <button className="btn btn-ghost btn-sm" onClick={() => setCreated(null)}>
              Done
            </button>
          </div>
        </div>
      )}
      <form
        className="inline-form"
        onSubmit={(e) => {
          e.preventDefault();
          if (name.trim()) create.mutate();
        }}
      >
        <input placeholder="Key name, e.g. production agent" value={name} onChange={(e) => setName(e.target.value)} maxLength={80} />
        <button className="btn btn-primary btn-sm" disabled={!name.trim() || create.isPending}>
          Create key
        </button>
      </form>
      {q.error && <ErrorPanel error={q.error} />}
      {q.isLoading && <Spinner />}
      {q.data && (
        <table className="table compact">
          <thead>
            <tr>
              <th>Name</th>
              <th>Prefix</th>
              <th>Created</th>
              <th>Last used</th>
              <th>Status</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {q.data.map((k) => (
              <tr key={k.id}>
                <td>{k.name}</td>
                <td className="mono">{k.prefix}…</td>
                <td title={dateTime(k.created_at)}>{relTime(k.created_at)}</td>
                <td>{relTime(k.last_used_at)}</td>
                <td>{k.revoked_at ? <Badge tone="error">revoked</Badge> : <Badge tone="ok">active</Badge>}</td>
                <td>
                  {!k.revoked_at && (
                    <button
                      className="btn btn-danger btn-sm"
                      disabled={revoke.isPending}
                      onClick={() => {
                        if (confirm(`Revoke key "${k.name}"? Agents using it will stop sending runs.`)) revoke.mutate(k.id);
                      }}
                    >
                      Revoke
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function Users() {
  const q = useQuery({ queryKey: ["users"], queryFn: api.users });
  const [form, setForm] = useState({ email: "", password: "", role: "viewer" });
  const toast = useToast();
  const qc = useQueryClient();
  const create = useMutation({
    mutationFn: () => api.createUser(form.email, form.password, form.role),
    onSuccess: (u) => {
      toast.ok(`Added ${u.email}`);
      setForm({ email: "", password: "", role: "viewer" });
      void qc.invalidateQueries({ queryKey: ["users"] });
    },
    onError: toast.error,
  });
  return (
    <section className="card section">
      <h3>Users</h3>
      <p className="muted">Admins can change settings and manage keys; viewers can investigate runs.</p>
      <form
        className="inline-form"
        onSubmit={(e) => {
          e.preventDefault();
          create.mutate();
        }}
      >
        <input type="email" placeholder="Email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} required />
        <input
          type="password"
          placeholder="Password (12+ characters)"
          minLength={12}
          value={form.password}
          onChange={(e) => setForm({ ...form, password: e.target.value })}
          required
          autoComplete="new-password"
        />
        <select value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value })} aria-label="Role">
          <option value="viewer">Viewer</option>
          <option value="admin">Admin</option>
        </select>
        <button className="btn btn-primary btn-sm" disabled={create.isPending}>
          Add user
        </button>
      </form>
      {q.error && <ErrorPanel error={q.error} />}
      {q.data && (
        <table className="table compact">
          <thead>
            <tr>
              <th>Email</th>
              <th>Role</th>
              <th>Added</th>
            </tr>
          </thead>
          <tbody>
            {q.data.map((u) => (
              <tr key={u.id}>
                <td>{u.email}</td>
                <td>
                  <Badge tone={u.role === "admin" ? "violet" : "neutral"}>{u.role}</Badge>
                </td>
                <td>{relTime(u.created_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
