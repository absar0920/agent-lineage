import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, type Conversation, type Customer } from "../api";
import { Empty, ErrorPanel, Spinner, relTime, shortId } from "../components/ui";

export default function Conversations() {
  const { id } = useParams();
  const navigate = useNavigate();
  const convs = useQuery({ queryKey: ["conversations"], queryFn: api.conversations, refetchInterval: 15_000 });
  const current = convs.data?.find((c) => c.id === id);
  const composing = !id;

  return (
    <div className="conv-layout">
      <aside className="conv-list">
        <div className="conv-list-head">
          <h2>Conversations</h2>
          <button className="btn btn-primary btn-sm" onClick={() => navigate("/conversations")}>
            + New
          </button>
        </div>
        {convs.error && <ErrorPanel error={convs.error} />}
        {convs.isLoading && <Spinner />}
        {convs.data?.length === 0 && <div className="muted pad">No conversations yet.</div>}
        {convs.data?.map((c) => {
          const last = c.messages[c.messages.length - 1];
          return (
            <Link key={c.id} to={`/conversations/${c.id}`} className={`conv-item ${c.id === id ? "active" : ""}`}>
              <div className="conv-item-top">
                <strong className="ellipsis">{c.customer_name}</strong>
                <span className="muted small">{relTime(last?.created_at ?? c.created_at)}</span>
              </div>
              <div className="muted small mono">{c.ticket_id}</div>
              <div className="muted small ellipsis">{last?.content ?? ""}</div>
            </Link>
          );
        })}
      </aside>
      <section className="conv-main">
        {composing ? (
          <NewConversation onStarted={(cid) => navigate(`/conversations/${cid}`)} />
        ) : !convs.data ? (
          <Spinner />
        ) : current ? (
          <Thread conv={current} />
        ) : (
          <Empty title="Conversation not found" />
        )}
      </section>
    </div>
  );
}

function Thread({ conv }: { conv: Conversation }) {
  const qc = useQueryClient();
  const [text, setText] = useState("");
  const endRef = useRef<HTMLDivElement>(null);
  const send = useMutation({
    mutationFn: (message: string) => api.sendMessage(conv.id, message),
    onSuccess: () => {
      setText("");
      void qc.invalidateQueries({ queryKey: ["conversations"] });
    },
  });
  useEffect(() => {
    // Block body on purpose: newer browsers return a Promise from scrollIntoView, and React
    // would treat any returned value as a cleanup function.
    endRef.current?.scrollIntoView({ block: "end" });
  }, [conv.messages.length, send.isPending]);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (text.trim()) send.mutate(text.trim());
  };

  return (
    <div className="thread">
      <header className="thread-head">
        <div>
          <h2>{conv.customer_name}</h2>
          <div className="muted small">
            <span className="mono">{conv.customer_id}</span> · {conv.customer_email} · ticket <span className="mono">{conv.ticket_id}</span>
          </div>
        </div>
      </header>
      <div className="messages">
        {conv.messages.map((m, i) => (
          <div key={i} className={`msg msg-${m.role}`}>
            <div className="msg-bubble">{m.content}</div>
            <div className="msg-meta muted small">
              {m.role === "customer" ? "Customer" : "Agent"} · {relTime(m.created_at)}
              {m.run_id && (
                <>
                  {" · "}
                  <Link to={`/runs/${m.run_id}`} className="mono">
                    run {shortId(m.run_id)} →
                  </Link>
                </>
              )}
            </div>
          </div>
        ))}
        {send.isPending && (
          <div className="msg msg-customer pending">
            <div className="msg-bubble">{send.variables}</div>
          </div>
        )}
        {send.isPending && (
          <div className="msg msg-assistant">
            <Spinner label="Agent is working…" />
          </div>
        )}
        <div ref={endRef} />
      </div>
      {send.error && <ErrorPanel error={send.error} />}
      <form className="composer" onSubmit={submit}>
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="Reply as the customer…"
          rows={2}
          maxLength={4000}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) submit(e);
          }}
        />
        <button className="btn btn-primary" disabled={send.isPending || !text.trim()}>
          Send
        </button>
      </form>
    </div>
  );
}

function NewConversation({ onStarted }: { onStarted: (id: string) => void }) {
  const qc = useQueryClient();
  const [query, setQuery] = useState("");
  const [debounced, setDebounced] = useState("");
  const [customer, setCustomer] = useState<Customer | null>(null);
  const [message, setMessage] = useState("");
  useEffect(() => {
    const t = setTimeout(() => setDebounced(query), 200);
    return () => clearTimeout(t);
  }, [query]);
  const customers = useQuery({
    queryKey: ["customers", debounced],
    queryFn: () => api.customers(debounced),
    enabled: !customer && debounced.length >= 2,
  });
  const start = useMutation({
    mutationFn: () => api.startConversation(customer!.email, message.trim()),
    onSuccess: (res) => {
      void qc.invalidateQueries({ queryKey: ["conversations"] });
      onStarted(res.conversation_id);
    },
  });

  return (
    <div className="new-conv">
      <h2>New conversation</h2>
      <p className="muted">Send a message on behalf of a customer. The agent answers using the connected store, helpdesk and payments systems, and every step is captured.</p>
      <label className="field">
        <span>Customer</span>
        {customer ? (
          <div className="picked">
            <strong>{customer.name}</strong> <span className="muted">{customer.email}</span> <span className="mono muted">{customer.id}</span>
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => setCustomer(null)}>
              Change
            </button>
          </div>
        ) : (
          <div className="typeahead">
            <input placeholder="Search by name, email or customer ID" value={query} onChange={(e) => setQuery(e.target.value)} autoFocus />
            {customers.data && customers.data.length > 0 && (
              <ul className="typeahead-list">
                {customers.data.map((c) => (
                  <li key={c.id}>
                    <button type="button" onClick={() => setCustomer(c)}>
                      <strong>{c.name}</strong> <span className="muted">{c.email}</span> <span className="mono muted small">{c.id}</span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
            {customers.data?.length === 0 && <div className="muted small">No matching customers.</div>}
          </div>
        )}
      </label>
      <label className="field">
        <span>Message</span>
        <textarea rows={4} value={message} onChange={(e) => setMessage(e.target.value)} maxLength={4000}
          placeholder="e.g. The trail shoes from my latest order are too small, can I get a refund?" />
      </label>
      {start.error && <ErrorPanel error={start.error} />}
      <button className="btn btn-primary" disabled={!customer || !message.trim() || start.isPending} onClick={() => start.mutate()}>
        {start.isPending ? "Agent is working…" : "Start conversation"}
      </button>
    </div>
  );
}
