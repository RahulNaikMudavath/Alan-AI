/* Same-origin API client. The dashboard is served by the API process. */

const RAW_BASE = typeof window !== "undefined" ? window.RECALLKIT_API_BASE || "" : "";
const BASE = String(RAW_BASE).replace(/\/$/, "");

export class ApiError extends Error {
  constructor(status, payload) {
    const detail = formatDetail(payload);
    super(detail || `Request failed with status ${status}`);
    this.name = "ApiError";
    this.status = status;
    this.payload = payload;
    this.detail = detail;
  }
}

function formatDetail(payload) {
  if (typeof payload === "string") return payload;
  if (!payload || typeof payload !== "object") return "";
  if (typeof payload.detail === "string") return payload.detail;
  if (Array.isArray(payload.detail)) {
    return payload.detail
      .map((item) => {
        const location = Array.isArray(item.loc) ? item.loc.join(".") : "request";
        return `${location}: ${item.msg || "invalid value"}`;
      })
      .join("; ");
  }
  return "";
}

function query(params) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params || {})) {
    if (value === null || value === undefined || value === "") continue;
    search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

async function request(path, options = {}) {
  const headers = { ...(options.body ? { "Content-Type": "application/json" } : {}) };
  const response = await fetch(`${BASE}${path}`, { ...options, headers });
  if (response.status === 204) return null;

  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json")
    ? await response.json()
    : await response.text();

  if (!response.ok) throw new ApiError(response.status, payload);
  return payload;
}

export const api = {
  health: () => request("/health"),
  runtime: () => request("/dashboard/api/runtime"),
  reports: () => request("/dashboard/api/reports"),
  report: (name) => request(`/dashboard/api/reports/${encodeURIComponent(name)}`),

  listMemories: ({ userId, q, kind, status, limit = 20, offset = 0 }) =>
    request(
      `/memories${query({
        user_id: userId,
        q,
        kind,
        status,
        limit,
        offset,
      })}`,
    ),
  getMemory: (memoryId, userId) =>
    request(`/memories/${encodeURIComponent(memoryId)}${query({ user_id: userId })}`),
  explain: (memoryId, userId) =>
    request(`/memories/${encodeURIComponent(memoryId)}/explain${query({ user_id: userId })}`),
  createMemory: (payload) =>
    request("/memories", { method: "POST", body: JSON.stringify(payload) }),
  deleteMemory: (memoryId, userId) =>
    request(`/memories/${encodeURIComponent(memoryId)}${query({ user_id: userId })}`, {
      method: "DELETE",
    }),
  supersede: (memoryId, userId, payload) =>
    request(
      `/memories/${encodeURIComponent(memoryId)}/supersede${query({ user_id: userId })}`,
      { method: "POST", body: JSON.stringify(payload) },
    ),
  search: (payload) =>
    request("/memories/search", { method: "POST", body: JSON.stringify(payload) }),

  conflicts: ({ userId, status = "open", limit = 20, offset = 0 }) =>
    request(
      `/memory-conflicts${query({
        user_id: userId,
        status,
        limit,
        offset,
      })}`,
    ),
  resolveConflict: (conflictId, payload) =>
    request(`/memory-conflicts/${encodeURIComponent(conflictId)}/resolve`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
};