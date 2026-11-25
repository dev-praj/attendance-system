import { useEffect, useState } from "react";
import { Link, Navigate, Outlet, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, fetchCurrentUser, type User } from "./api";
import {
  AssignSubjectsPage, AuditPage, ClassesPage, ClassDetailPage, DashboardPage, DevicesPage, FaceEnrollmentPage, FaceVerificationPage,
  FacesPage, LoginPage, NotFoundPage, ReportsPage, SettingsPage, StudentDetailPage,
  StudentsPage, TeachersPage,
} from "./pages";

const links = [
  { to: "/", title: "Dashboard", mark: "◫" },
  { to: "/classes", title: "Classes", mark: "▦" },
  { to: "/students", title: "Students", mark: "◉" },
  { to: "/face-enrollment", title: "Register Face", mark: "◎" },
  { to: "/face-verification", title: "Scan / verify", mark: "◉" },
  { to: "/faces", title: "Registered Faces", mark: "◌" },
  { to: "/reports", title: "Reports", mark: "▤" },
  { to: "/settings", title: "Settings", mark: "⚙" },
];

function Shell({ user }: { user: User }) {
  const [mobileOpen, setMobileOpen] = useState(false);
  const location = useLocation();
  const navigate = useNavigate();
  const logout = async (everywhere = false) => {
    await api(`/auth/${everywhere ? "logout-everywhere" : "logout"}`, { method: "POST" });
    navigate("/login", { replace: true });
    window.location.reload();
  };
  const toggleTheme = () => {
    const light = document.body.classList.toggle("light");
    localStorage.setItem("attendance-theme", light ? "light" : "dark");
  };
  return (
    <div className="app-shell">
      <aside className={`sidebar ${mobileOpen ? "mobile-open" : ""}`}>
        <div className="brand"><span className="brand-mark">C</span><span>Classroom<span className="muted"> / attendance</span></span></div>
        <div className="nav-label">Workspace</div>
        {links.map((link) => (
          <Link className={`nav-link ${location.pathname === link.to ? "active" : ""}`} to={link.to} key={link.to} onClick={() => setMobileOpen(false)}>
            <span aria-hidden="true">{link.mark}</span>{link.title}
          </Link>
        ))}
        {user.role === "admin" && <>
          <div className="nav-label">Administration</div>
          <Link className={`nav-link ${location.pathname === "/assign-subjects" ? "active" : ""}`} to="/assign-subjects"><span>▦</span>Assign Subjects</Link>
          <Link className={`nav-link ${location.pathname === "/teachers" ? "active" : ""}`} to="/teachers"><span>♙</span>Teachers</Link>
          <Link className={`nav-link ${location.pathname === "/devices" ? "active" : ""}`} to="/devices"><span>⌘</span>Devices</Link>
          <Link className={`nav-link ${location.pathname === "/audit" ? "active" : ""}`} to="/audit"><span>☷</span>Audit log</Link>
        </>}
        <div className="nav-label">Account</div>
        <button className="nav-link" onClick={() => void logout(true)}><span>⇥</span>Log out everywhere</button>
      </aside>
      <div className="main-area">
        <header className="topbar">
          <div className="toolbar">
            <button className="button secondary mobile-menu" onClick={() => setMobileOpen((value) => !value)} aria-label="Toggle menu">☰</button>
            <span className="muted">Teacher workspace</span>
          </div>
          <div className="toolbar">
            <button className="button secondary" onClick={toggleTheme}>Toggle theme</button>
            <span>{user.name} <span className="badge">{user.role}</span></span>
            <button className="button secondary" onClick={() => void logout()}>Log out</button>
          </div>
        </header>
        <main className="content"><Outlet /></main>
      </div>
    </div>
  );
}

function Authenticated() {
  const auth = useQuery({ queryKey: ["me"], queryFn: fetchCurrentUser, retry: false });
  useEffect(() => {
    if (localStorage.getItem("attendance-theme") === "light") document.body.classList.add("light");
  }, []);
  if (auth.isLoading) return <div className="login-screen"><div className="muted">Loading your secure workspace…</div></div>;
  if (auth.error) return <div className="login-screen"><section className="login-card">
    <h1>Server unavailable</h1>
    <div className="error-box">The website loaded, but it cannot reach the attendance API.</div>
    <ol className="login-help">
      <li>In Supabase, open <strong>Project Settings → Database → Connect</strong> and select the <strong>Session pooler</strong> connection string (PostgreSQL URI, port 5432).</li>
      <li>Replace <code>DATABASE_URL</code> in <code>website/.env</code> with that URI, replacing its password placeholder locally. Do not paste the password into chat or frontend code.</li>
      <li>From the <code>website</code> folder, run <code>python run.py dev</code>. Fix any startup error in that terminal before retrying.</li>
    </ol>
    <p className="login-help">The project HTTPS URL (ending in <code>.supabase.co</code>) is not a PostgreSQL connection string. Sign-in and face features stay unavailable until the database-backed API starts.</p>
    <button className="button" onClick={() => window.location.reload()}>Retry connection</button>
  </section></div>;
  if (!auth.data) return <Navigate to="/login" replace />;
  return (
    <Routes>
      <Route element={<Shell user={auth.data} />}>
        <Route index element={<DashboardPage />} />
        <Route path="classes" element={<ClassesPage user={auth.data} />} />
        <Route path="classes/:classId" element={<ClassDetailPage user={auth.data} />} />
        <Route path="students" element={<StudentsPage />} />
        <Route path="students/:studentId" element={<StudentDetailPage />} />
        <Route path="face-enrollment" element={<FaceEnrollmentPage />} />
        {auth.data.role === "admin" && <Route path="assign-subjects" element={<AssignSubjectsPage />} />}
        <Route path="face-verification" element={<FaceVerificationPage />} />
        <Route path="faces" element={<FacesPage />} />
        <Route path="reports" element={<ReportsPage />} />
        <Route path="settings" element={<SettingsPage />} />
        {auth.data.role === "admin" && <>
          <Route path="teachers" element={<TeachersPage />} />
          <Route path="devices" element={<DevicesPage />} />
          <Route path="audit" element={<AuditPage />} />
        </>}
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  );
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="/*" element={<Authenticated />} />
    </Routes>
  );
}
