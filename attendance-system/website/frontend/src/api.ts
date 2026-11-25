export type User = { id: string; name: string; email: string; role: "admin" | "teacher"; active: boolean };
export type ClassItem = {
  id: string; name: string; code: string; teacher_id: string; room: string | null;
  schedule_days: string; schedule_start: string | null; schedule_end: string | null;
  academic_term: string; active: boolean;
};
export type Student = {
  id: string; name: string; roll_no: string; email: string | null; active: boolean;
  class_ids: string[]; attendance_percent: number | null; face_registered: boolean;
};

function csrfCookie(): string {
  return document.cookie.split("; ").find((part) => part.startsWith("csrf_token="))?.split("=").slice(1).join("=") ?? "";
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !(init.body instanceof FormData)) headers.set("Content-Type", "application/json");
  if (init.method && !["GET", "HEAD", "OPTIONS"].includes(init.method.toUpperCase())) {
    headers.set("X-CSRF-Token", decodeURIComponent(csrfCookie()));
  }
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      ...init,
      headers,
      credentials: "include",
    });
  } catch (error) {
    if (error instanceof TypeError) {
      throw new Error(
        "Cannot reach the attendance API. Start it with `python run.py dev` and make sure DATABASE_URL is the PostgreSQL connection URI from Supabase Connect, not the project HTTPS URL.",
        { cause: error },
      );
    }
    throw error;
  }
  if (!response.ok) {
    let message = response.status >= 500
      ? `The attendance API returned an error (${response.status}). Check the backend startup output and its PostgreSQL DATABASE_URL.`
      : `Request failed (${response.status})`;
    try {
      const body = await response.json() as { detail?: string };
      message = body.detail ?? message;
    } catch {
      // Keep a useful status message for non-JSON network errors.
    }
    throw new Error(message);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export async function fetchCurrentUser(): Promise<User | null> {
  try {
    return await api<User>("/auth/me");
  } catch (error) {
    if (error instanceof Error && error.message === "Please log in.") return null;
    if (error instanceof Error && error.message.includes("401")) return null;
    throw error;
  }
}
