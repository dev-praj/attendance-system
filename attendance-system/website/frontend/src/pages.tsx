import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Bar, BarChart, CartesianGrid, Cell, Line, LineChart, Pie, PieChart, ResponsiveContainer,
  Tooltip, XAxis, YAxis,
} from "recharts";
import { api, type ClassItem, type Student, type User } from "./api";

type Dashboard = {
  today: string;
  classes_today: ClassItem[];
  held_sessions: number;
  attendance_counts: Record<string, number>;
  overall_percent: number | null;
  below_threshold_count: number;
  students_needing_attention: { id: string; name: string; roll_no: string; percent: number }[];
  recent_events: { id: string; student: string; direction: string; timestamp: string; camera_id: string | null }[];
  attendance_trend: { date: string; label: string; attendance: number | null }[];
  class_averages: { name: string; attendance: number | null }[];
};
type ClassDetail = {
  class: ClassItem; students: Student[];
  sessions: { id: string; date: string; start_time: string; end_time: string; status: string }[];
  attendance_records: { session_id: string; student_id: string; status: string; override_reason: string | null }[];
};
type StudentDetail = {
  student: Student; attendance_percent: number | null;
  records: { session_id: string; date: string; status: string; first_in: string | null; last_out: string | null; minutes_present: number; override_reason: string | null }[];
  events: { direction: string; timestamp: string; score: number | null; camera_id: string | null }[];
  absent_dates: string[]; face_registered: boolean;
};
type SettingsData = {
  grace_period_minutes: number; late_counts_as_attended: boolean; low_attendance_threshold: number;
  face_match_threshold: number; academic_term: string; data_retention_days: number;
  store_profile_photo: boolean; formula: string;
};
const colors = ["#6ee7a7", "#f7c96a", "#f17c87", "#7d8aa3"];
const REQUIRED_POSES = ["CENTER", "LEFT", "RIGHT", "UP", "DOWN", "UP-LEFT", "UP-RIGHT", "DOWN-LEFT", "DOWN-RIGHT"];
const FACE_ENROLLMENT_TIMING = import.meta.env.DEV && import.meta.env.VITE_FACE_ENROLLMENT_TIMING === "1";

function PageHead({ title, description, action }: { title: string; description: string; action?: ReactNode }) {
  return <div className="page-head"><div><div className="eyebrow">Teacher workspace</div><h1>{title}</h1><div className="muted">{description}</div></div>{action}</div>;
}
function ErrorState({ error }: { error: unknown }) {
  const message = error instanceof Error ? error.message : typeof error === "string" ? error : "Something went wrong. Please try again.";
  return <div className="error-box">{message}</div>;
}
function Empty({ children }: { children: ReactNode }) { return <div className="empty">{children}</div>; }
function Loading() { return <div className="card muted" role="status">Loading…</div>; }
function Metric({ label, value, note }: { label: string; value: React.ReactNode; note?: string }) {
  return <article className="card"><div className="metric-label">{label}</div><div className="metric">{value}</div>{note && <div className="muted" style={{ fontSize: 12 }}>{note}</div>}</article>;
}
function useLoad<T>(key: string, url: string) {
  return useQuery({ queryKey: [key], queryFn: () => api<T>(url) });
}
function useRefresh() {
  const client = useQueryClient();
  return useCallback(() => client.invalidateQueries(), [client]);
}

export function LoginPage() {
  const navigate = useNavigate();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [csrfReady, setCsrfReady] = useState(false);
  const initializeCsrf = useCallback(async () => {
    setCsrfReady(false);
    setError("");
    try {
      await api<{ csrf_token: string }>("/auth/login");
      setCsrfReady(true);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not initialize secure sign-in.");
    }
  }, []);
  useEffect(() => {
    const expired = sessionStorage.getItem("attendance-session-expired");
    if (expired) { setMessage("Your session expired. Please sign in again."); sessionStorage.removeItem("attendance-session-expired"); }
    void initializeCsrf();
  }, [initializeCsrf]);
  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError(""); setBusy(true);
    const form = new FormData(event.currentTarget);
    try {
      await api("/auth/login", { method: "POST", body: JSON.stringify({ email: form.get("email"), password: form.get("password") }) });
      navigate("/", { replace: true });
      window.location.reload();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Sign-in failed."); }
    finally { setBusy(false); }
  };
  return <div className="login-screen"><form className="login-card" onSubmit={(event) => void submit(event)}>
    <div className="brand"><span className="brand-mark">C</span>Classroom attendance</div>
    <div className="eyebrow">Teacher access only</div><h1>Welcome back</h1><p className="muted">Sign in to manage your classes and attendance.</p>
    {message && <div className="badge warn">{message}</div>}{error && <ErrorState error={error} />}
    <label className="field">Email<input className="input" type="email" name="email" autoComplete="username" required /></label>
    <label className="field">Password<input className="input" type="password" name="password" autoComplete="current-password" required /></label>
    <button className="button" disabled={busy || !csrfReady} style={{ width: "100%", marginTop: 12 }}>
      {busy ? "Signing in…" : !csrfReady ? "Connecting…" : "Sign in"}
    </button>
    {!csrfReady && error && <button className="button secondary" type="button" onClick={() => void initializeCsrf()} style={{ width: "100%", marginTop: 8 }}>Retry connection</button>}
    <p className="login-help">Sign-in uses Supabase. Your email must also have an active teacher profile approved by an administrator. New installations must create the first administrator from the backend setup script.</p>
  </form></div>;
}

export function DashboardPage() {
  const { data, isLoading, error } = useLoad<Dashboard>("dashboard", "/dashboard");
  if (isLoading) return <Loading />;
  if (error || !data) return <ErrorState error={error} />;
  const counts = data.attendance_counts;
  const donut = ["present", "late", "absent"].map((key) => ({ name: key, value: counts[key] ?? 0 }));
  return <>
    <PageHead title="Good day" description={`Attendance overview · ${data.today}`} />
    <div className="grid grid-4">
      <Metric label="Held sessions" value={data.held_sessions} note="For today" />
      <Metric label="Overall attendance" value={data.overall_percent == null ? "—" : `${data.overall_percent}%`} />
      <Metric label="Students to check in on" value={data.below_threshold_count} note="Below your threshold" />
      <Metric label="Classes today" value={data.classes_today.length} />
    </div>
    <div className="grid grid-3" style={{ marginTop: 16 }}>
      <section className="card" style={{ gridColumn: "span 2" }}><h2>Attendance trend</h2>
        {data.attendance_trend.length ? <div style={{ height: 260 }}><ResponsiveContainer><LineChart data={data.attendance_trend}><CartesianGrid stroke="#293548" /><XAxis dataKey="label" stroke="#8995a8" /><YAxis domain={[0, 100]} stroke="#8995a8" /><Tooltip /><Line type="monotone" dataKey="attendance" stroke="#6ee7a7" strokeWidth={3} connectNulls /></LineChart></ResponsiveContainer></div> : <Empty>Your attendance trend will appear when sessions are held.</Empty>}
      </section>
      <section className="card"><h2>Session statuses</h2>
        {donut.some((item) => item.value > 0) ? <div style={{ height: 260 }}><ResponsiveContainer><PieChart><Pie data={donut} dataKey="value" nameKey="name" innerRadius={58} outerRadius={88}>{donut.map((entry, index) => <Cell key={entry.name} fill={colors[index]} />)}</Pie><Tooltip /></PieChart></ResponsiveContainer></div> : <Empty>No attendance recorded yet.</Empty>}
      </section>
    </div>
    <div className="grid grid-2" style={{ marginTop: 16 }}>
      <section className="card"><h2>Classes today</h2>{data.classes_today.length ? <div className="table-wrap"><table><thead><tr><th>Class</th><th>Code</th><th>Room</th></tr></thead><tbody>{data.classes_today.map((item) => <tr key={item.id}><td><Link to={`/classes/${item.id}`}>{item.name}</Link></td><td>{item.code}</td><td>{item.room || "—"}</td></tr>)}</tbody></table></div> : <Empty>No classes scheduled today.</Empty>}</section>
      <section className="card"><h2>Students needing attention</h2>{data.students_needing_attention.length ? data.students_needing_attention.map((student) => <p key={student.id}><Link to={`/students/${student.id}`}>{student.name}</Link> <span className="badge bad">{student.percent}%</span></p>) : <Empty>No students below the attendance threshold.</Empty>}</section>
    </div>
    <div className="grid grid-2" style={{ marginTop: 16 }}>
      <section className="card"><h2>Class comparison</h2>{data.class_averages.some((item) => item.attendance != null) ? <div style={{ height: 230 }}><ResponsiveContainer><BarChart data={data.class_averages}><CartesianGrid stroke="#293548" /><XAxis dataKey="name" stroke="#8995a8" /><YAxis domain={[0, 100]} stroke="#8995a8" /><Tooltip /><Bar dataKey="attendance" fill="#6ee7a7" radius={[6, 6, 0, 0]} /></BarChart></ResponsiveContainer></div> : <Empty>Class averages appear after sessions are held.</Empty>}</section>
      <section className="card"><h2>Recent entry and exit events</h2>{data.recent_events.length ? data.recent_events.map((event) => <p key={event.id}><span className={`badge ${event.direction === "in" ? "good" : "warn"}`}>{event.direction}</span> {event.student}<span className="muted"> · {new Date(event.timestamp).toLocaleString()}</span></p>) : <Empty>No recent events.</Empty>}</section>
    </div>
  </>;
}

export function ClassesPage({ user }: { user: User }) {
  const { data, isLoading, error } = useLoad<ClassItem[]>("classes", "/classes");
  const teachersQuery = useQuery({
    queryKey: ["class-teachers"],
    queryFn: () => api<User[]>("/teachers"),
    enabled: user.role === "admin",
  });
  const refresh = useRefresh();
  const [message, setMessage] = useState("");
  const create = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const formElement = event.currentTarget;
    const form = new FormData(formElement);
    try {
      await api("/classes", { method: "POST", body: JSON.stringify({
        name: form.get("name"), code: form.get("code"), teacher_id: user.role === "admin" ? String(form.get("teacher_id") || "").trim() || undefined : undefined,
        room: form.get("room") || null, schedule_days: form.get("schedule_days") || "",
        schedule_start: form.get("schedule_start") || null, schedule_end: form.get("schedule_end") || null,
        academic_term: form.get("academic_term") || "",
      }) });
      setMessage("Class created."); formElement.reset(); refresh();
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : "Could not create class."); }
  };
  if (isLoading) return <Loading />;
  return <>
    <PageHead title="Classes" description="Create classes, manage rosters, and schedule sessions." />
    {error && <ErrorState error={error} />}{message && <div className="badge good">{message}</div>}
    <section className="card" style={{ marginBottom: 16 }}><h2>Create a class</h2><form onSubmit={(event) => void create(event)} className="grid grid-3">
      <label className="field">Class name<input name="name" className="input" required maxLength={120} /></label>
      <label className="field">Class code<input name="code" className="input" required maxLength={32} /></label>
      {user.role === "admin" && <label className="field">Teacher<select name="teacher_id" className="input" required defaultValue="">
        <option value="" disabled>Select a teacher</option>
        {teachersQuery.data?.filter((teacher) => teacher.active).map((teacher) => <option value={teacher.id} key={teacher.id}>{teacher.name} · {teacher.email}</option>)}
      </select></label>}
      <label className="field">Room<input name="room" className="input" /></label>
      <label className="field">Days<input name="schedule_days" placeholder="Mon, Wed" className="input" /></label>
      <label className="field">Term<input name="academic_term" className="input" /></label>
      <button className="button">Create class</button>
    </form></section>
    <section className="card"><h2>Your classes</h2>{data?.length ? <div className="table-wrap"><table><thead><tr><th>Name</th><th>Code</th><th>Room</th><th>Term</th><th /></tr></thead><tbody>{data.map((item) => <tr key={item.id}><td>{item.name}</td><td>{item.code}</td><td>{item.room || "—"}</td><td>{item.academic_term || "—"}</td><td><Link className="button secondary" to={`/classes/${item.id}`}>Open</Link></td></tr>)}</tbody></table></div> : <Empty>Create your first class to get started.</Empty>}</section>
  </>;
}

export function ClassDetailPage({ user }: { user: User }) {
  const { classId } = useParams();
  const { data, isLoading, error } = useLoad<ClassDetail>(`class-${classId}`, `/classes/${classId}/detail`);
  const refresh = useRefresh();
  const [message, setMessage] = useState("");
  const [rosterMessage, setRosterMessage] = useState("");
  if (isLoading) return <Loading />;
  if (error || !data) return <ErrorState error={error} />;
  const schedule = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    try {
      await api(`/classes/${classId}/sessions`, { method: "POST", body: JSON.stringify({
        session_date: form.get("date"), start_time: form.get("start"), end_time: form.get("end"),
        status: "scheduled", repeat_weeks: Number(form.get("weeks") || 1),
      }) });
      setMessage("Sessions scheduled."); refresh();
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : "Could not schedule sessions."); }
  };
  const bulkEnroll = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const formElement = event.currentTarget;
    const rolls = String(new FormData(formElement).get("roll_numbers") || "")
      .split(/[\s,;]+/).map((value) => value.trim()).filter(Boolean);
    try {
      const result = await api<{ added: number }>(`/classes/${classId}/students/bulk`, {
        method: "POST", body: JSON.stringify({ roll_numbers: rolls }),
      });
      setRosterMessage(`${result.added} students added.`);
      formElement.reset(); refresh();
    } catch (reason) { setRosterMessage(reason instanceof Error ? reason.message : "Could not update roster."); }
  };
  const setAttendance = async (sessionId: string, studentId: string, status: string) => {
    const reason = window.prompt("Required override reason:");
    if (!reason || reason.trim().length < 3) { refresh(); return; }
    try {
      await api(`/sessions/${sessionId}/students/${studentId}/override`, {
        method: "POST", body: JSON.stringify({ status, reason }),
      });
      setMessage("Attendance override saved."); refresh();
    } catch (error) { setMessage(error instanceof Error ? error.message : "Override failed."); }
  };
  return <>
    <PageHead title={data.class.name} description={`${data.class.code} · ${data.class.room || "No room set"}`} action={<Link className="button secondary" to="/classes">All classes</Link>} />
    <div className="grid grid-2">
      <section className="card"><h2>Schedule sessions</h2><form onSubmit={(event) => void schedule(event)}>
        <label className="field">Date<input className="input" type="date" name="date" required /></label>
        <div className="grid grid-2"><label className="field">Start<input className="input" type="time" name="start" required /></label><label className="field">End<input className="input" type="time" name="end" required /></label></div>
        <label className="field">Repeat weekly<input className="input" type="number" name="weeks" min="1" max="52" defaultValue="1" /></label>
        <button className="button">Create sessions</button>{message && <p className="muted">{message}</p>}
      </form></section>
      <section className="card"><h2>Class roster <span className="badge">{data.students.length} students</span></h2>
        {user.role === "admin" && <form onSubmit={(event) => void bulkEnroll(event)}><label className="field">Add students by roll number (comma or line separated)<textarea className="input" name="roll_numbers" rows={3} placeholder="R-001, R-002" required /></label><button className="button secondary">Add to class</button>{rosterMessage && <p className="muted">{rosterMessage}</p>}</form>}
        {data.students.length ? data.students.map((student) => <p key={student.id}><Link to={`/students/${student.id}`}>{student.name}</Link> <span className="muted">{student.roll_no}</span></p>) : <Empty>No students enrolled yet.</Empty>}
      </section>
    </div>
    <section className="card" style={{ marginTop: 16 }}><h2>Sessions</h2>{data.sessions.length ? <div className="table-wrap"><table><thead><tr><th>Date</th><th>Time</th><th>Status</th><th /></tr></thead><tbody>{data.sessions.map((session) => <tr key={session.id}><td>{session.date}</td><td>{session.start_time}–{session.end_time}</td><td><span className="badge">{session.status}</span></td><td className="toolbar">
      <button className="button secondary" onClick={() => void api(`/sessions/${session.id}`, { method: "PATCH", body: JSON.stringify({ status: "held" }) }).then(refresh).catch((reason: unknown) => setMessage(String(reason)))}>Mark held</button>
      <button className="button danger" onClick={() => void api(`/sessions/${session.id}`, { method: "PATCH", body: JSON.stringify({ status: "cancelled" }) }).then(refresh).catch((reason: unknown) => setMessage(String(reason)))}>Cancel</button>
    </td></tr>)}</tbody></table></div> : <Empty>No sessions scheduled.</Empty>}</section>
    {data.sessions.some((session) => session.status === "held") && <section className="card" style={{ marginTop: 16 }}><h2>Student-by-session attendance</h2><p className="login-help">Changing a status requires a reason. Excused sessions are excluded from attendance percentage.</p>
      <div className="table-wrap"><table><thead><tr><th>Student</th>{data.sessions.filter((session) => session.status === "held").map((session) => <th key={session.id}>{session.date}</th>)}</tr></thead><tbody>
        {data.students.map((student) => <tr key={student.id}><td>{student.name}</td>{data.sessions.filter((session) => session.status === "held").map((session) => {
          const record = data.attendance_records.find((item) => item.session_id === session.id && item.student_id === student.id);
          return <td key={session.id}><select className="input" aria-label={`${student.name} ${session.date} status`} value={record?.status ?? "absent"} onChange={(event) => void setAttendance(session.id, student.id, event.target.value)}>
            {["present", "late", "absent", "excused"].map((status) => <option key={status} value={status}>{status}</option>)}
          </select></td>;
        })}</tr>)}
      </tbody></table></div>{message && <p aria-live="polite">{message}</p>}
    </section>}
  </>;
}

export function StudentsPage() {
  const [search, setSearch] = useState("");
  const query = useQuery({ queryKey: ["students", search], queryFn: () => api<Student[]>(`/students?q=${encodeURIComponent(search)}`) });
  return <>
    <PageHead title="Students" description="Search and review students in your classes." action={<input className="input" aria-label="Search students" placeholder="Search name or roll number" value={search} onChange={(event) => setSearch(event.target.value)} />} />
    {query.isLoading && <Loading />}{query.error && <ErrorState error={query.error} />}
    <section className="card table-wrap">{query.data?.length ? <table><thead><tr><th>Name</th><th>Roll no</th><th>Classes</th><th>Attendance</th><th>Face</th></tr></thead><tbody>{query.data.map((student) => <tr key={student.id}><td><Link to={`/students/${student.id}`}>{student.name}</Link></td><td>{student.roll_no}</td><td>{student.class_ids.length}</td><td>{student.attendance_percent == null ? "—" : `${student.attendance_percent}%`}</td><td><span className={`badge ${student.face_registered ? "good" : "warn"}`}>{student.face_registered ? "Registered" : "Not registered"}</span></td></tr>)}</tbody></table> : !query.isLoading && <Empty>No students found. Add students during face registration or demo seeding.</Empty>}</section>
  </>;
}

export function StudentDetailPage() {
  const { studentId } = useParams();
  const { data, isLoading, error } = useLoad<StudentDetail>(`student-${studentId}`, `/students/${studentId}`);
  if (isLoading) return <Loading />;
  if (error || !data) return <ErrorState error={error} />;
  const distribution = ["present", "late", "absent", "excused"].map((status) => ({
    name: status, value: data.records.filter((row) => row.status === status).length,
  }));
  return <>
    <PageHead title={data.student.name} description={`Roll number ${data.student.roll_no}`} action={<Link className="button secondary" to="/students">All students</Link>} />
    <div className="grid grid-3"><Metric label="Overall attendance" value={data.attendance_percent == null ? "—" : `${data.attendance_percent}%`} /><Metric label="Held sessions" value={data.records.length} /><Metric label="Face status" value={data.face_registered ? "Registered" : "Not registered"} /></div>
    <div className="grid grid-2" style={{ marginTop: 16 }}>
      <section className="card"><h2>Attendance history</h2>{data.records.length ? <div className="table-wrap"><table><thead><tr><th>Date</th><th>Status</th><th>First in</th><th>Minutes</th></tr></thead><tbody>{data.records.map((row) => <tr key={row.session_id}><td>{row.date}</td><td><span className={`badge ${row.status === "present" ? "good" : row.status === "late" ? "warn" : "bad"}`}>{row.status}</span></td><td>{row.first_in ? new Date(row.first_in).toLocaleTimeString() : "—"}</td><td>{row.minutes_present}</td></tr>)}</tbody></table></div> : <Empty>No held sessions yet.</Empty>}</section>
      <section className="card"><h2>Attendance trend</h2>{data.records.length ? <div style={{ height: 280 }}><ResponsiveContainer><LineChart data={[...data.records].reverse().map((row) => ({ date: row.date.slice(5), value: row.status === "present" ? 100 : row.status === "late" ? 70 : 0 }))}><CartesianGrid stroke="#293548" /><XAxis dataKey="date" stroke="#8995a8" /><YAxis domain={[0, 100]} stroke="#8995a8" /><Tooltip /><Line dataKey="value" stroke="#6ee7a7" strokeWidth={3} /></LineChart></ResponsiveContainer></div> : <Empty>Attendance trend appears after held sessions.</Empty>}</section>
      <section className="card"><h2>Status distribution</h2>{data.records.length ? <div style={{ height: 260 }}><ResponsiveContainer><PieChart><Pie data={distribution} dataKey="value" nameKey="name" outerRadius={90}>{distribution.map((item, index) => <Cell key={item.name} fill={colors[index]} />)}</Pie><Tooltip /></PieChart></ResponsiveContainer></div> : <Empty>No attendance records.</Empty>}</section>
      <section className="card"><h2>Absent dates</h2>{data.absent_dates.length ? data.absent_dates.map((day) => <span className="badge bad" key={day} style={{ margin: 4 }}>{day}</span>) : <Empty>No absences recorded.</Empty>}</section>
      <section className="card"><h2>Entry / exit history</h2>{data.events.length ? data.events.map((event, index) => <p key={`${event.timestamp}-${index}`}><span className="badge">{event.direction}</span> {new Date(event.timestamp).toLocaleString()} {event.camera_id && <span className="muted">· {event.camera_id}</span>}</p>) : <Empty>No door events recorded.</Empty>}</section>
    </div>
  </>;
}

export function FaceEnrollmentPage() {
  const videoRef = useRef<HTMLVideoElement>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const sessionIdRef = useRef("");
  const coverageDoneRef = useRef(false);
  const pausedRef = useRef(false);
  const scanStartedAt = useRef<number | null>(null);
  const [form, setForm] = useState({ name: "", roll_no: "", email: "", consent_note: "" });
  const [consent, setConsent] = useState(false);
  const [sessionId, setSessionId] = useState("");
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [currentPose, setCurrentPose] = useState("");
  const [measuredPose, setMeasuredPose] = useState<{ yaw: number; pitch: number } | null>(null);
  const [selfTestFrames, setSelfTestFrames] = useState(0);
  const [paused, setPaused] = useState(false);
  const [retryable, setRetryable] = useState(false);
  const [blocked, setBlocked] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [message, setMessage] = useState("Enter student details and record consent before starting.");
  const [error, setError] = useState("");
  const [active, setActive] = useState(false);
  const [coverageDone, setCoverageDone] = useState(false);
  const refresh = useRefresh();

  useEffect(() => { coverageDoneRef.current = coverageDone; }, [coverageDone]);
  useEffect(() => { sessionIdRef.current = sessionId; }, [sessionId]);
  useEffect(() => { pausedRef.current = paused; }, [paused]);
  useEffect(() => () => streamRef.current?.getTracks().forEach((track) => track.stop()), []);
  useEffect(() => {
    if (!active) return;
    let running = true;
    let inFlight = false;
    let consecutiveErrors = 0;
    let nextFrameAt = 0;
    let frameNumber = 0;
    let lastFrameAt = 0;
    let skippedInFlight = 0;
    let timer: number | undefined;
    let elapsedTimer: number | undefined;
    const start = async () => {
      if (!window.isSecureContext && !["localhost", "127.0.0.1"].includes(window.location.hostname)) {
        setError("Camera access requires localhost or a secure HTTPS page.");
        setActive(false);
        return;
      }
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "user", width: { ideal: 640 }, height: { ideal: 480 } }, audio: false });
        if (!running) { stream.getTracks().forEach((track) => track.stop()); return; }
        streamRef.current = stream;
        if (videoRef.current) { videoRef.current.srcObject = stream; await videoRef.current.play(); }
        timer = window.setInterval(() => {
          if (!running || pausedRef.current || !sessionIdRef.current ||
              Date.now() < nextFrameAt || !videoRef.current || !canvasRef.current ||
              videoRef.current.readyState < 2) return;
          if (inFlight) { skippedInFlight += 1; return; }
          inFlight = true;
          const captureAt = performance.now();
          const frameIntervalMs = lastFrameAt ? captureAt - lastFrameAt : 0;
          lastFrameAt = captureAt;
          frameNumber += 1;
          const video = videoRef.current;
          const canvas = canvasRef.current;
          canvas.width = 640; canvas.height = Math.round(640 * video.videoHeight / Math.max(1, video.videoWidth));
          const context = canvas.getContext("2d");
          if (!context) { inFlight = false; setError("Browser canvas is unavailable."); return; }
          // Preview mirroring is CSS only; detection always receives this original, unmirrored frame.
          context.drawImage(video, 0, 0, canvas.width, canvas.height);
          const encodeStartedAt = performance.now();
          const frame = canvas.toDataURL("image/jpeg", 0.72).split(",")[1];
          const jpegEncodeMs = performance.now() - encodeStartedAt;
          const requestStartedAt = performance.now();
          const requestSessionId = sessionIdRef.current;
          const endpoint = coverageDoneRef.current
            ? `/face/enrollments/${requestSessionId}/self-test`
            : `/face/enrollments/${requestSessionId}/frame`;
          const controller = new AbortController();
          const requestTimeout = window.setTimeout(() => controller.abort(), 8000);
          void api<{ status: string; message: string; counts?: Record<string, number>; complete?: boolean; reason?: string; yaw?: number; pitch?: number; self_test_frames?: number; possible_match?: { name: string; roll_no: string } }>(
            endpoint, { method: "POST", body: JSON.stringify({ frame }), signal: controller.signal },
          ).then((result) => {
            if (!running) return;
            if (FACE_ENROLLMENT_TIMING) {
              console.info("face_enrollment_frontend_timing", JSON.stringify({
                frame: frameNumber,
                frame_interval_ms: Number(frameIntervalMs.toFixed(3)),
                jpeg_encode_ms: Number(jpegEncodeMs.toFixed(3)),
                request_round_trip_ms: Number((performance.now() - requestStartedAt).toFixed(3)),
                skipped_in_flight: skippedInFlight,
              }));
              skippedInFlight = 0;
            }
            consecutiveErrors = 0;
            nextFrameAt = Date.now();
            setError("");
            setMessage(result.message);
            if (result.counts) {
              setCounts(result.counts);
              setCurrentPose(REQUIRED_POSES.find((pose) => (result.counts?.[pose] ?? 0) < 3) ?? "CENTER");
            }
            if (typeof result.self_test_frames === "number") setSelfTestFrames(result.self_test_frames);
            if (typeof result.yaw === "number" && typeof result.pitch === "number") {
              setMeasuredPose({ yaw: result.yaw, pitch: result.pitch });
            }
            if (result.status === "checking" && result.counts && REQUIRED_POSES.every((pose) => (result.counts?.[pose] ?? 0) >= 3)) {
              coverageDoneRef.current = true;
              setCoverageDone(true);
            }
            if (result.status === "complete") {
              setMessage("Face enrollment complete. The face map passed verification.");
              pausedRef.current = true;
              coverageDoneRef.current = false;
              sessionIdRef.current = "";
              setActive(false); setPaused(false); setCoverageDone(false); setSessionId("");
              streamRef.current?.getTracks().forEach((track) => track.stop());
              streamRef.current = null; refresh();
            } else if (result.status === "retry") {
              coverageDoneRef.current = false;
              setCoverageDone(false);
              setSelfTestFrames(0);
              setMessage(`${result.message} ${result.reason ?? ""}`);
            } else if (result.status === "failed" || result.status === "blocked") {
              pausedRef.current = true;
              sessionIdRef.current = "";
              setSessionId(""); setPaused(true);
              setRetryable(result.status === "failed"); setBlocked(result.status === "blocked");
              setError(result.status === "blocked" && result.possible_match
                ? `Possible duplicate: ${result.possible_match.name} (${result.possible_match.roll_no}).`
                : `${result.message} ${result.reason ?? ""}`);
            } else if (result.status === "guiding" || result.status === "calibrating") {
              setMeasuredPose(null);
            }
          }).catch((reason: unknown) => {
            if (!running) return;
            if (FACE_ENROLLMENT_TIMING) {
              console.info("face_enrollment_frontend_timing", JSON.stringify({
                frame: frameNumber,
                frame_interval_ms: Number(frameIntervalMs.toFixed(3)),
                jpeg_encode_ms: Number(jpegEncodeMs.toFixed(3)),
                request_round_trip_ms: Number((performance.now() - requestStartedAt).toFixed(3)),
                skipped_in_flight: skippedInFlight,
                failed: 1,
              }));
              skippedInFlight = 0;
            }
            consecutiveErrors += 1;
            nextFrameAt = Date.now() + Math.min(250 * (2 ** Math.min(consecutiveErrors - 1, 4)), 4000);
            const detail = reason instanceof Error ? reason.message : "Enrollment frame failed.";
            if (/Request failed \((404|410)\)/.test(detail)) {
              pausedRef.current = true;
              sessionIdRef.current = "";
              setSessionId(""); setPaused(true); setRetryable(true);
              setError("The scan session expired. The camera is still on; retry the scan to continue.");
            } else {
              setError(`${detail} The camera will keep scanning and retry shortly.`);
            }
          }).finally(() => {
            window.clearTimeout(requestTimeout);
            inFlight = false;
          });
        }, 125);
        elapsedTimer = window.setInterval(() => {
          if (scanStartedAt.current !== null) {
            setElapsed(Math.floor((Date.now() - scanStartedAt.current) / 1000));
          }
        }, 500);
      } catch (reason) {
        const detail = (reason instanceof Error || reason instanceof DOMException) && reason.message ? ` ${reason.message}` : "";
        setError(`Camera access failed. Allow camera permission for this browser, and use localhost or HTTPS.${detail}`);
        setActive(false);
      }
    };
    void start();
    return () => {
      running = false;
      if (timer) window.clearInterval(timer);
      if (elapsedTimer) window.clearInterval(elapsedTimer);
      streamRef.current?.getTracks().forEach((track) => track.stop());
      streamRef.current = null;
    };
  }, [active, refresh]);

  const beginSession = async () => {
    const result = await api<{ session_id: string; counts: Record<string, number>; message: string }>("/face/enrollments", {
      method: "POST", body: JSON.stringify({ name: form.name, roll_no: form.roll_no, email: form.email || null, consent, consent_note: form.consent_note || null }),
    });
    sessionIdRef.current = result.session_id;
    setSessionId(result.session_id); setCounts(result.counts); setCoverageDone(false);
    coverageDoneRef.current = false;
    setSelfTestFrames(0); setCurrentPose("CENTER"); setMeasuredPose(null);
    setPaused(false); pausedRef.current = false; setRetryable(false); setBlocked(false);
    setMessage(result.message);
  };
  const start = async (event: FormEvent) => {
    event.preventDefault(); setError(""); setMessage("Recording consent and starting enrollment…");
    try {
      await beginSession(); scanStartedAt.current = Date.now(); setElapsed(0); setActive(true);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Could not start enrollment."); }
  };
  const retryScan = async () => {
    setError(""); setMessage("Starting a fresh scan; the camera stays on.");
    try { await beginSession(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Could not restart enrollment."); }
  };
  const cancelScan = () => {
    const currentSession = sessionIdRef.current;
    pausedRef.current = true;
    coverageDoneRef.current = false;
    sessionIdRef.current = "";
    setPaused(false); setActive(false); setSessionId(""); setCoverageDone(false);
    if (currentSession) {
      void api(`/face/enrollments/${currentSession}`, { method: "DELETE" })
        .then(() => setMessage("Scan cancelled. No face enrollment was completed."))
        .catch((reason: unknown) => setError(reason instanceof Error ? reason.message : "Could not cancel enrollment."));
    }
  };
  const completedCount = REQUIRED_POSES.reduce((sum, pose) => sum + Math.min(3, counts[pose] ?? 0), 0);
  const progress = Math.round((completedCount / (REQUIRED_POSES.length * 3)) * 100);
  const targetPose = currentPose || REQUIRED_POSES.find((pose) => (counts[pose] ?? 0) < 3) || "CENTER";
  const targetLabel = blocked ? "Scan paused" : paused ? "Ready to retry" : coverageDone ? "Live verification" : "Live pose capture";
  const prompt = coverageDone
    ? `Live check · ${selfTestFrames}/5 frames`
    : `${targetPose} · ${message}`;
  return <>
    <PageHead title="Register face" description="A measured, nine-pose scan with encrypted face templates and later subject assignment." />
    {error && <ErrorState error={error} />}
    <div className="enrollment-layout">
      <form className="enrollment-panel enrollment-form" onSubmit={(event) => void start(event)}>
        <div className="enrollment-eyebrow">STUDENT PROFILE</div>
        <h2>Student and consent</h2>
        <p className="enrollment-copy">Register the face first. An administrator can assign classes later.</p>
        <label className="field">Student name<input className="input" required maxLength={120} disabled={active} value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} /></label>
        <label className="field">SAN / USN<input className="input" required maxLength={64} disabled={active} value={form.roll_no} onChange={(event) => setForm({ ...form, roll_no: event.target.value })} /></label>
        <label className="field">Email <span className="muted">Optional</span><input className="input" type="email" disabled={active} value={form.email} onChange={(event) => setForm({ ...form, email: event.target.value })} /></label>
        <label className="field">Consent note<input className="input" maxLength={1000} disabled={active} value={form.consent_note} onChange={(event) => setForm({ ...form, consent_note: event.target.value })} /></label>
        <label className="enrollment-consent"><input type="checkbox" required disabled={active} checked={consent} onChange={(event) => setConsent(event.target.checked)} /><span>The student has given consent for face data to be used for attendance. Consent time and recording teacher will be saved.</span></label>
        <button className="button enrollment-start" disabled={active || !consent}>{active ? paused ? "Scan paused" : "Scan in progress" : "Start face scan"}</button>
        {active && <button type="button" className="button secondary enrollment-cancel" onClick={cancelScan}>Cancel scan</button>}
        {retryable && active && <button type="button" className="button enrollment-start" onClick={() => void retryScan()}>Retry scan</button>}
        <p className="enrollment-privacy">Only live camera frames are sent for processing. Photos and video are not saved.</p>
      </form>

      <section className="enrollment-panel enrollment-stage" aria-label="Live face enrollment scan">
        <div className="enrollment-stage-head">
          <div><div className="enrollment-eyebrow">LIVE ENROLLMENT</div><h2>{targetLabel}</h2></div>
          <span className={`enrollment-state ${active && !paused ? "is-live" : blocked ? "is-blocked" : ""}`}><i />{active ? paused ? "Camera on · paused" : "Camera on" : "Camera off"}</span>
        </div>
        <div className="enrollment-camera-wrap">
          <video ref={videoRef} className="enrollment-video" playsInline muted aria-label="Mirrored live face camera preview" />
          <svg className="enrollment-face-map" viewBox="0 0 100 100" role="img" aria-label={`Measured face pose ${measuredPose ? `yaw ${measuredPose.yaw.toFixed(1)} degrees, pitch ${measuredPose.pitch.toFixed(1)} degrees` : "not yet available"}`}>
            <circle className="face-map-center-ring" cx="50" cy="50" r="38" />
            {Array.from({ length: 64 }, (_, tick) => {
              const groupIndex = Math.floor(tick / 8);
              const groupPose = ["RIGHT", "DOWN-RIGHT", "DOWN", "DOWN-LEFT", "LEFT", "UP-LEFT", "UP", "UP-RIGHT"][groupIndex];
              const groupProgress = Math.round(((counts[groupPose] ?? 0) / 3) * 8);
              const angle = (tick * Math.PI) / 32 - Math.PI / 2;
              const inner = 44; const outer = 48;
              const target = targetPose === groupPose && groupProgress < 8;
              return <line key={tick} className={`face-map-tick ${tick % 8 < groupProgress ? "is-filled" : ""} ${target ? "is-target" : ""}`} x1={50 + Math.cos(angle) * inner} y1={50 + Math.sin(angle) * inner} x2={50 + Math.cos(angle) * outer} y2={50 + Math.sin(angle) * outer} />;
            })}
            <circle className={`face-map-center ${counts.CENTER >= 3 ? "is-filled" : ""}`} cx="50" cy="50" r="1.8" />
            {measuredPose && <circle className="face-map-direction" cx={Math.max(28, Math.min(72, 50 - measuredPose.yaw * 0.48))} cy={Math.max(28, Math.min(72, 50 + measuredPose.pitch * 0.55))} r="2.1" />}
          </svg>
          <canvas ref={canvasRef} hidden />
          {!active && <div className="enrollment-camera-idle"><span>◎</span><small>Live face map appears here</small></div>}
        </div>
        <div className="enrollment-prompt-row"><div><div className="enrollment-eyebrow">CURRENT GUIDANCE</div><p aria-live="polite">{prompt}</p></div><div className="enrollment-clock"><strong>{String(Math.floor(elapsed / 60)).padStart(2, "0")}:{String(elapsed % 60).padStart(2, "0")}</strong><small>60 sec target</small></div></div>
        <div className="enrollment-progress-track" role="progressbar" aria-valuemin={0} aria-valuemax={27} aria-valuenow={completedCount}><span style={{ width: `${progress}%` }} /></div>
        <div className="enrollment-coverage-list">{REQUIRED_POSES.map((pose) => {
          const count = counts[pose] ?? 0;
          return <div key={pose} className={`enrollment-coverage ${count >= 3 ? "is-complete" : pose === targetPose && active ? "is-current" : ""}`}><span className="enrollment-coverage-mark">{count >= 3 ? "✓" : "·"}</span><span>{pose === "CENTER" ? "Center" : pose.replace("-", " · ")}</span><span>{count}/3</span></div>;
        })}</div>
        <p className="enrollment-footnote">Keep your face inside the circle and follow each prompt. The camera stays on through retries; it closes after a verified scan or when you cancel. Aim for 60 seconds in clear light; timing varies with the camera, network, and pose movement.</p>
      </section>
    </div>
  </>;
}

type StudentSubjectLookup = {
  student: { id: string; name: string; san: string; email: string | null; active: boolean };
  face_enrolled: boolean;
  subjects: ClassItem[];
};

export function AssignSubjectsPage() {
  const [san, setSan] = useState("");
  const [student, setStudent] = useState<StudentSubjectLookup | null>(null);
  const [selectedClassIds, setSelectedClassIds] = useState<string[]>([]);
  const [searching, setSearching] = useState(false);
  const [saving, setSaving] = useState(false);
  const [notFound, setNotFound] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const classesQuery = useLoad<ClassItem[]>("classes", "/classes");

  const lookup = async (event: FormEvent) => {
    event.preventDefault();
    if (searching || saving) return;
    setError(""); setMessage(""); setNotFound(false); setStudent(null); setSelectedClassIds([]);
    if (!san.trim()) { setError("Enter a SAN/USN to find a student."); return; }
    setSearching(true);
    try {
      const result = await api<StudentSubjectLookup>(`/students/lookup?san=${encodeURIComponent(san)}`);
      setStudent(result);
      setSan(result.student.san);
    } catch (reason) {
      const detail = reason instanceof Error ? reason.message : "Student lookup failed.";
      if (detail === "Student not found for this SAN/USN.") setNotFound(true);
      else setError(detail);
    } finally {
      setSearching(false);
    }
  };

  const assign = async (event: FormEvent) => {
    event.preventDefault();
    if (!student || !selectedClassIds.length || saving || searching) return;
    setSaving(true); setError(""); setMessage("");
    try {
      const result = await api<StudentSubjectLookup>("/students/subjects", {
        method: "POST",
        body: JSON.stringify({ san: student.student.san, class_ids: selectedClassIds }),
      });
      setStudent(result); setSelectedClassIds([]); setMessage("Subjects assigned.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Subject assignment failed.");
    } finally {
      setSaving(false);
    }
  };

  const unassign = async (classId: string) => {
    if (!student || saving || searching) return;
    setSaving(true); setError(""); setMessage("");
    try {
      const result = await api<StudentSubjectLookup>(
        `/students/subjects?san=${encodeURIComponent(student.student.san)}&class_id=${encodeURIComponent(classId)}`,
        { method: "DELETE" },
      );
      setStudent(result); setMessage("Subject removed.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Subject removal failed.");
    } finally {
      setSaving(false);
    }
  };

  const availableClasses = (classesQuery.data ?? []).filter(
    (item) => !student?.subjects.some((subject) => subject.id === item.id),
  );

  return <>
    <PageHead title="Assign Subjects" description="Find a student by SAN/USN and manage their class assignments." />
    {error && <ErrorState error={error} />}
    {message && <p aria-live="polite">{message}</p>}
    <section className="card">
      <h2>Find student</h2>
      <form className="toolbar" onSubmit={(event) => void lookup(event)}>
        <label className="field" style={{ flex: 1 }}>SAN/USN
          <input className="input" required maxLength={64} value={san} onChange={(event) => setSan(event.target.value)} />
        </label>
        <button className="button" type="submit" disabled={searching || saving}>{searching ? "Searching…" : "Search"}</button>
      </form>
      {notFound && <p>Student not found. <Link to="/face-enrollment">Register the student first.</Link></p>}
    </section>
    {student && <section className="card" style={{ marginTop: 16 }}>
      <h2>{student.student.name}</h2>
      <p className="muted">SAN/USN: {student.student.san} · Face enrollment: {student.face_enrolled ? "Complete" : "Not complete"}</p>
      <h3>Current subjects</h3>
      {student.subjects.length ? <ul>{student.subjects.map((subject) => <li key={subject.id}>
        {subject.name} · {subject.code}{" "}
        <button className="button danger" type="button" disabled={saving || searching} onClick={() => void unassign(subject.id)}>Remove</button>
      </li>)}</ul> : <Empty>This student has no subjects assigned.</Empty>}
      <form onSubmit={(event) => void assign(event)}>
        <label className="field">Add subjects
          {classesQuery.isLoading ? <Loading /> : classesQuery.error ? <ErrorState error={classesQuery.error} /> : availableClasses.length ? <select
            className="input" multiple value={selectedClassIds} disabled={saving || searching}
            onChange={(event) => setSelectedClassIds(Array.from(event.target.selectedOptions, (option) => option.value))}
          >{availableClasses.map((item) => <option value={item.id} key={item.id}>{item.name} · {item.code}</option>)}</select> : <Empty>No other classes are available.</Empty>}
        </label>
        <button className="button" type="submit" disabled={saving || searching || !selectedClassIds.length}>{saving ? "Saving…" : "Save assignments"}</button>
      </form>
    </section>}
  </>;
}

type FaceVerificationResult = {
  accepted: boolean;
  clear_scan: boolean;
  message: string;
  threshold: number;
  good_frames: number;
  frame_issues?: { frame: number; reason: string }[];
  frame_results?: {
    frame: number;
    scores: { name: string; roll_no: string; score: number }[];
    vote: string | null;
    best_score: number;
    margin: number | null;
    reason: string;
  }[];
  votes?: { name: string; roll_no: string; votes: number }[];
  duplicate_event?: boolean;
};

const VERIFY_FRAME_COUNT = 8;
const VERIFY_CAPTURE_TIMEOUT_MS = 5000;

export function FaceVerificationPage() {
  const videoRef = useRef<HTMLVideoElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const [direction, setDirection] = useState<"in" | "out">("in");
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState(0);
  const [error, setError] = useState("");
  const [result, setResult] = useState<FaceVerificationResult | null>(null);

  useEffect(() => () => streamRef.current?.getTracks().forEach((track) => track.stop()), []);

  const scan = async () => {
    setError("");
    setResult(null);
    setProgress(0);
    if (!window.isSecureContext && !["localhost", "127.0.0.1"].includes(window.location.hostname)) {
      setError("Camera access requires HTTPS when the website is deployed.");
      return;
    }
    if (!navigator.mediaDevices?.getUserMedia) {
      setError("This browser does not support camera access. Open the site in a current browser over HTTPS.");
      return;
    }

    setBusy(true);
    let captureStarted = 0;
    const frames: string[] = [];
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: "user", width: { ideal: 640 }, height: { ideal: 480 } },
        audio: false,
      });
      streamRef.current = stream;
      const video = videoRef.current;
      const canvas = canvasRef.current;
      if (!video || !canvas) throw new Error("The camera preview could not be initialized. Reload and retry.");
      video.srcObject = stream;
      await video.play();

      const cameraReadyDeadline = performance.now() + 10_000;
      while (video.readyState < 2 && performance.now() < cameraReadyDeadline) {
        await new Promise((resolve) => window.setTimeout(resolve, 50));
      }
      if (video.readyState < 2 || !video.videoWidth || !video.videoHeight) {
        throw new Error("The camera started but did not provide a video frame. Check that it is not in use by another app.");
      }

      captureStarted = performance.now();
      const context = canvas.getContext("2d");
      if (!context) throw new Error("Browser image capture is unavailable.");
      canvas.width = 480;
      canvas.height = Math.round(480 * video.videoHeight / video.videoWidth);
      for (let index = 0; index < VERIFY_FRAME_COUNT; index += 1) {
        if (performance.now() - captureStarted >= VERIFY_CAPTURE_TIMEOUT_MS) {
          throw new Error("Can't get a clear scan within 5 seconds. Try again with steady lighting.");
        }
        context.drawImage(video, 0, 0, canvas.width, canvas.height);
        frames.push(canvas.toDataURL("image/jpeg", 0.65).split(",")[1] ?? "");
        setProgress(index + 1);
        if (index + 1 < VERIFY_FRAME_COUNT) await new Promise((resolve) => window.setTimeout(resolve, 300));
      }
      if (performance.now() - captureStarted > VERIFY_CAPTURE_TIMEOUT_MS) {
        throw new Error("Can't get a clear scan within 5 seconds. Try again.");
      }

      stream.getTracks().forEach((track) => track.stop());
      streamRef.current = null;
      video.srcObject = null;
      const response = await api<FaceVerificationResult>("/face/verify", {
        method: "POST",
        body: JSON.stringify({ frames, direction, camera_id: "teacher-webcam" }),
      });
      setResult(response);
    } catch (reason) {
      if (reason instanceof DOMException) {
        if (reason.name === "NotAllowedError" || reason.name === "SecurityError") {
          setError("Camera permission was denied. Allow camera access for this site in your browser settings.");
        } else if (reason.name === "NotFoundError" || reason.name === "DevicesNotFoundError") {
          setError("No camera was found. Connect a webcam and retry.");
        } else if (reason.name === "NotReadableError" || reason.name === "TrackStartError") {
          setError("The camera is unavailable or in use by another app. Close other camera apps and retry.");
        } else {
          setError(reason.message || "Camera access failed. Check browser permissions and retry.");
        }
      } else {
        setError(reason instanceof Error ? reason.message : "Face verification failed. Please retry.");
      }
    } finally {
      streamRef.current?.getTracks().forEach((track) => track.stop());
      streamRef.current = null;
      if (videoRef.current) videoRef.current.srcObject = null;
      setBusy(false);
    }
  };

  return <>
    <PageHead title="Scan and verify" description="Verify a face against consented enrollments and record a check-in or check-out when accepted." />
    {error && <ErrorState error={error} />}
    <div className="grid grid-2">
      <section className="card">
        <h2>Attendance scan</h2>
        <label className="field">Attendance direction
          <select className="input" value={direction} disabled={busy} onChange={(event) => setDirection(event.target.value as "in" | "out")}>
            <option value="in">Check in</option><option value="out">Check out</option>
          </select>
        </label>
        <button className="button" type="button" disabled={busy} onClick={() => void scan()}>
          {busy ? `Capturing ${progress}/${VERIFY_FRAME_COUNT} frames…` : "Start face scan"}
        </button>
        <p className="login-help">The browser captures eight frames and sends them to the authenticated backend for YuNet/SFace matching. Only an accepted scan records an attendance event. Frames are not saved. This is not liveness or anti-spoofing.</p>
        <div style={{ display: "grid", placeItems: "center", margin: "16px 0" }}>
          <video ref={videoRef} className="face-preview" playsInline muted aria-label="Mirrored verification camera preview" />
          <canvas ref={canvasRef} hidden />
        </div>
      </section>
      <section className="card" aria-live="polite">
        <h2>Verification result</h2>
        {!result && !busy && <Empty>Start a scan to verify an identity.</Empty>}
        {busy && <p role="status">Capturing frames ({progress}/{VERIFY_FRAME_COUNT})…</p>}
        {result && <>
          <div className={`badge ${result.accepted ? "good" : "bad"}`}>{result.accepted ? "SAME PERSON" : "NOT RECOGNIZED"}</div>
          <p>{result.message}</p>
          <p className="login-help">{result.good_frames}/8 clear frames · cosine threshold {result.threshold.toFixed(2)}
            {result.duplicate_event ? " · matching check-in/out was already recorded recently" : ""}
          </p>
          {result.votes && <div className="table-wrap"><table><thead><tr><th>Person</th><th>Roll no</th><th>Votes</th></tr></thead><tbody>
            {result.votes.map((vote) => <tr key={vote.roll_no}><td>{vote.name}</td><td>{vote.roll_no}</td><td>{vote.votes}/8</td></tr>)}
          </tbody></table></div>}
          {result.frame_issues?.map((issue) => <p className="login-help" key={issue.frame}>Frame {issue.frame}: {issue.reason.replaceAll("_", " ")}</p>)}
          {result.frame_results?.map((frame) => <div key={frame.frame} className="verification-frame">
            <strong>Frame {frame.frame} · {frame.vote ? `vote: ${frame.vote}` : frame.reason}</strong>
            <span className="muted">Best {frame.best_score.toFixed(3)} · margin {frame.margin == null ? "n/a" : frame.margin.toFixed(3)}</span>
            {frame.scores.map((score) => <span className="muted" key={score.roll_no}>{score.name}: cosine {score.score.toFixed(3)}</span>)}
          </div>)}
        </>}
      </section>
    </div>
  </>;
}

export function FacesPage() {
  const query = useLoad<{ student_id: string; name: string; roll_no: string; enrolled_at: string; pose_counts: Record<string, number>; embedding_count: number; quality: number }[]>("faces", "/faces");
  const [confirmation, setConfirmation] = useState("");
  const refresh = useRefresh();
  const remove = async (studentId: string) => {
    try { await api(`/students/${studentId}/face?confirm=${encodeURIComponent(confirmation)}`, { method: "DELETE" }); setConfirmation(""); refresh(); }
    catch (reason) { window.alert(reason instanceof Error ? reason.message : "Delete failed."); }
  };
  return <>
    <PageHead title="Registered faces" description="Encrypted biometric records linked to consenting students." />
    <div className="card" style={{ marginBottom: 16 }}><label className="field">Type a student's roll number to confirm deletion<input className="input" value={confirmation} onChange={(event) => setConfirmation(event.target.value)} /></label></div>
    {query.isLoading && <Loading />}{query.error && <ErrorState error={query.error} />}
    <section className="card table-wrap">{query.data?.length ? <table><thead><tr><th>Student</th><th>Roll no</th><th>Enrolled</th><th>Poses</th><th>Vectors</th><th>Quality</th><th>Action</th></tr></thead><tbody>{query.data.map((row) => <tr key={row.student_id}><td>{row.name}</td><td>{row.roll_no}</td><td>{new Date(row.enrolled_at).toLocaleDateString()}</td><td>{Object.values(row.pose_counts).filter((count) => count === 3).length}/9</td><td>{row.embedding_count}</td><td>{row.quality.toFixed(2)}</td><td><button className="button danger" onClick={() => void remove(row.student_id)} disabled={!confirmation}>Delete face data</button></td></tr>)}</tbody></table> : !query.isLoading && <Empty>No face data is registered.</Empty>}</section>
  </>;
}

export function ReportsPage() {
  const query = useLoad<{ students: Student[]; classes: ClassItem[] }>("reports", "/reports");
  return <>
    <PageHead title="Reports" description="Review attendance, low-attendance students, and export a CSV." action={<a className="button" href="/api/reports/export.csv">Export CSV</a>} />
    {query.isLoading && <Loading />}{query.error && <ErrorState error={query.error} />}
    <div className="grid grid-3">{query.data && <Metric label="Classes" value={query.data.classes.length} />}{query.data && <Metric label="Students" value={query.data.students.length} />}{query.data && <Metric label="Below 75%" value={query.data.students.filter((item) => item.attendance_percent != null && item.attendance_percent < 75).length} />}</div>
    <section className="card table-wrap" style={{ marginTop: 16 }}>{query.data?.students.length ? <table><thead><tr><th>Student</th><th>Roll no</th><th>Attendance %</th><th>Flag</th></tr></thead><tbody>{query.data.students.map((student) => <tr key={student.id}><td><Link to={`/students/${student.id}`}>{student.name}</Link></td><td>{student.roll_no}</td><td>{student.attendance_percent ?? "—"}</td><td>{student.attendance_percent != null && student.attendance_percent < 75 ? <span className="badge bad">Needs attention</span> : <span className="badge good">On track</span>}</td></tr>)}</tbody></table> : !query.isLoading && <Empty>There is no report data yet. You can seed clearly marked demo records from the backend.</Empty>}</section>
  </>;
}

export function SettingsPage() {
  const query = useLoad<SettingsData>("settings", "/settings");
  const refresh = useRefresh();
  const [message, setMessage] = useState("");
  const save = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    try {
      await api("/settings", { method: "PUT", body: JSON.stringify({
        grace_period_minutes: Number(form.get("grace")), late_counts_as_attended: form.get("late") === "on",
        low_attendance_threshold: Number(form.get("threshold")),
        face_match_threshold: Number(form.get("face-threshold")), academic_term: form.get("term"),
        data_retention_days: Number(form.get("retention")), store_profile_photo: false,
      }) });
      setMessage("Settings saved."); refresh();
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : "Save failed."); }
  };
  if (query.isLoading) return <Loading />;
  if (query.error || !query.data) return <ErrorState error={query.error} />;
  const setting = query.data;
  return <>
    <PageHead title="Settings" description="Configure attendance rules and data retention." />
    <form className="card" onSubmit={(event) => void save(event)} style={{ maxWidth: 720 }}>
      <label className="field">Grace period in minutes<input className="input" name="grace" type="number" min="0" max="180" defaultValue={setting.grace_period_minutes} /></label>
      <label className="field" style={{ display: "flex", flexDirection: "row" }}><input name="late" type="checkbox" defaultChecked={setting.late_counts_as_attended} /> Count late attendance as attended</label>
      <label className="field">Low attendance threshold (%)<input className="input" name="threshold" type="number" min="0" max="100" step="0.5" defaultValue={setting.low_attendance_threshold} /></label>
      <label className="field">Face verification cosine threshold<input className="input" name="face-threshold" type="number" min="0" max="1" step="0.01" defaultValue={setting.face_match_threshold} /></label>
      <label className="field">Academic term<input className="input" name="term" defaultValue={setting.academic_term} maxLength={80} /></label>
      <label className="field">Event retention days<input className="input" name="retention" type="number" min="1" max="3650" defaultValue={setting.data_retention_days} /></label>
      <p className="login-help">Attendance % formula: {setting.formula}. Face verification requires at least 6 of 8 frame votes and a 0.05 identity margin. Profile photos are disabled in this build.</p>
      <div className="toolbar"><button className="button">Save settings</button><button type="button" className="button danger" onClick={() => void api("/settings/purge", { method: "POST" }).then(() => setMessage("Old events purged.")).catch((reason: unknown) => setMessage(String(reason)))}>Purge old events</button></div>
      {message && <p aria-live="polite">{message}</p>}
    </form>
  </>;
}

export function TeachersPage() {
  const query = useLoad<{ id: string; name: string; email: string; role: string; active: boolean }[]>("teachers", "/teachers");
  const refresh = useRefresh();
  const [message, setMessage] = useState("");
  const add = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const formElement = event.currentTarget;
    const form = new FormData(formElement);
    try {
      await api("/teachers", { method: "POST", body: JSON.stringify({ name: form.get("name"), email: form.get("email"), password: form.get("password"), role: "teacher" }) });
      formElement.reset(); setMessage("Teacher created."); refresh();
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : "Could not create teacher."); }
  };
  return <>
    <PageHead title="Teachers" description="Administrator-only account management." />
    <section className="card" style={{ marginBottom: 16 }}><h2>Create teacher</h2><form className="grid grid-2" onSubmit={(event) => void add(event)}>
      <label className="field">Name<input className="input" name="name" required /></label><label className="field">Email<input className="input" name="email" type="email" required /></label>
      <label className="field">Temporary password<input className="input" name="password" type="password" minLength={10} required /></label><button className="button">Create account</button></form>{message && <p>{message}</p>}</section>
    {query.isLoading && <Loading />}{query.error && <ErrorState error={query.error} />}
    <section className="card table-wrap">{query.data?.length ? <table><thead><tr><th>Name</th><th>Email</th><th>Role</th><th>Status</th><th /></tr></thead><tbody>{query.data.map((teacher) => <tr key={teacher.id}><td>{teacher.name}</td><td>{teacher.email}</td><td>{teacher.role}</td><td>{teacher.active ? "Active" : "Inactive"}</td><td><button className="button secondary" onClick={() => void api(`/teachers/${teacher.id}/active?active=${!teacher.active}`, { method: "PATCH" }).then(refresh).catch((reason: unknown) => setMessage(String(reason)))}>{teacher.active ? "Deactivate" : "Activate"}</button></td></tr>)}</tbody></table> : null}</section>
  </>;
}

export function DevicesPage() {
  const query = useLoad<{ id: string; name: string; active: boolean; created_at: string }[]>("devices", "/devices");
  const refresh = useRefresh();
  const [key, setKey] = useState("");
  const [error, setError] = useState("");
  const create = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const name = new FormData(event.currentTarget).get("name");
    try { const result = await api<{ api_key: string }>("/devices", { method: "POST", body: JSON.stringify({ name }) }); setKey(result.api_key); refresh(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Could not create device."); }
  };
  return <>
    <PageHead title="Devices" description="Door-camera API credentials. Keys are shown once; store them securely." />
    {error && <ErrorState error={error} />}{key && <div className="error-box">Copy this API key now: <code>{key}</code></div>}
    <form className="card toolbar" onSubmit={(event) => void create(event)}><input className="input" name="name" placeholder="Device name" required /><button className="button">Create device</button></form>
    {query.isLoading && <Loading />}{query.error && <ErrorState error={query.error} />}
    <section className="card table-wrap" style={{ marginTop: 16 }}>{query.data?.length ? <table><thead><tr><th>Name</th><th>Status</th><th>Created</th></tr></thead><tbody>{query.data.map((item) => <tr key={item.id}><td>{item.name}</td><td>{item.active ? "Active" : "Inactive"}</td><td>{new Date(item.created_at).toLocaleDateString()}</td></tr>)}</tbody></table> : !query.isLoading && <Empty>No devices created.</Empty>}</section>
    <p className="login-help">Device embeddings are decrypted only for device-authenticated local matching. Protect the device key and use HTTPS in production; compromised devices expose biometric templates.</p>
  </>;
}

export function AuditPage() {
  const query = useLoad<{ id: string; actor: string | null; action: string; entity_type: string | null; entity_id: string | null; detail: string; ip_address: string | null; created_at: string }[]>("audit", "/audit");
  return <>
    <PageHead title="Audit log" description="Administrative history of sign-ins, changes, enrollment, and exports." />
    {query.isLoading && <Loading />}{query.error && <ErrorState error={query.error} />}
    <section className="card table-wrap">{query.data?.length ? <table><thead><tr><th>Time</th><th>Actor</th><th>Action</th><th>Entity</th><th>Details</th></tr></thead><tbody>{query.data.map((row) => <tr key={row.id}><td>{new Date(row.created_at).toLocaleString()}</td><td>{row.actor || "Unknown"}</td><td>{row.action}</td><td>{row.entity_type} {row.entity_id}</td><td>{row.detail}</td></tr>)}</tbody></table> : !query.isLoading && <Empty>No audit activity yet.</Empty>}</section>
  </>;
}

export function NotFoundPage() {
  return <div className="card"><h1>Page not found</h1><p className="muted">That page does not exist in this teacher workspace.</p><Link className="button" to="/">Go to dashboard</Link></div>;
}
