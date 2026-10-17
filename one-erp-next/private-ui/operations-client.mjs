/** Private authenticated operations API client. Never used by public demo UI. */
const ALLOWED = new Set(["sales", "purchases", "inventory", "returns"]);
export class OperationsApiError extends Error {
  constructor(status) { super("Operations request failed"); this.name = "OperationsApiError"; this.status = status; }
}
export async function readOperations({ baseUrl, module, token, fetchImpl = fetch, signal }) {
  if (!ALLOWED.has(module)) throw new TypeError("Unsupported operations module");
  const origin = new URL(baseUrl);
  if (origin.protocol !== "https:" || origin.username || origin.password ||
      origin.search || origin.hash || origin.pathname !== "/") {
    throw new TypeError("A trusted HTTPS API origin is required");
  }
  if (!/^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/.test(token || "")) {
    throw new TypeError("Missing verified session token");
  }
  const response = await fetchImpl(new URL("/api/v1/operations/" + module, origin), {
    method: "GET", mode: "cors", cache: "no-store", credentials: "omit",
    redirect: "error", signal,
    headers: { "Authorization": "Bearer " + token, "Accept": "application/json" },
  });
  if (!response.ok) throw new OperationsApiError(response.status);
  const type = response.headers.get("content-type") || "";
  if (!type.toLowerCase().includes("application/json")) throw new OperationsApiError(502);
  return response.json();
}
