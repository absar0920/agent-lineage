import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { Navigate, NavLink, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { api } from "./api";
import { Spinner } from "./components/ui";
import Connections from "./pages/Connections";
import Conversations from "./pages/Conversations";
import DiffView from "./pages/DiffView";
import Login from "./pages/Login";
import RunView from "./pages/RunView";
import Runs from "./pages/Runs";
import Settings from "./pages/Settings";

export default function App() {
  const location = useLocation();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const me = useQuery({ queryKey: ["me"], queryFn: api.me, retry: false, staleTime: 60_000 });

  useEffect(() => {
    const onExpired = () => {
      qc.setQueryData(["me"], null);
      qc.removeQueries({ queryKey: ["me"] });
      navigate("/login", { state: { from: location.pathname + location.search } });
    };
    window.addEventListener("auth:expired", onExpired);
    return () => window.removeEventListener("auth:expired", onExpired);
  }, [qc, navigate, location]);

  if (location.pathname === "/login") return <Login />;
  if (me.isLoading) {
    return (
      <div className="center-screen">
        <Spinner label="Loading…" />
      </div>
    );
  }
  if (!me.data) return <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />;

  const logout = async () => {
    await api.logout().catch(() => undefined);
    qc.clear();
    navigate("/login");
  };

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <img src="/favicon.svg" alt="" width={26} height={26} />
          <span>agent-lineage</span>
        </div>
        <nav>
          <NavLink to="/conversations">
            <span className="nav-ico">💬</span>Conversations
          </NavLink>
          <NavLink to="/runs">
            <span className="nav-ico">⧉</span>Runs
          </NavLink>
          <NavLink to="/connections">
            <span className="nav-ico">⛓</span>Connections
          </NavLink>
          <NavLink to="/settings">
            <span className="nav-ico">⚙</span>Settings
          </NavLink>
        </nav>
        <div className="sidebar-foot">
          <div className="muted small ellipsis" title={me.data.email}>
            {me.data.email}
          </div>
          <button className="btn btn-ghost btn-sm" onClick={logout}>
            Sign out
          </button>
        </div>
      </aside>
      <main className="main">
        <Routes>
          <Route path="/" element={<Navigate to="/runs" replace />} />
          <Route path="/conversations" element={<Conversations />} />
          <Route path="/conversations/:id" element={<Conversations />} />
          <Route path="/runs" element={<Runs />} />
          <Route path="/runs/:id" element={<RunView />} />
          <Route path="/runs/:a/diff/:b" element={<DiffView />} />
          <Route path="/connections" element={<Connections />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="*" element={<Navigate to="/runs" replace />} />
        </Routes>
      </main>
    </div>
  );
}
