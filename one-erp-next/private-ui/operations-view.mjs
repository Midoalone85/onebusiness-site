import { readOperations } from "./operations-client.mjs";

/** Controlled private view state. No demo data and no browser token persistence. */
export function createOperationsView({ baseUrl, getToken, fetchImpl }) {
  let revision = 0;
  let controller = null;
  let state = { status: "idle", module: null, data: null, error: null };
  const snapshot = () => ({ ...state });
  return {
    snapshot,
    async load(module) {
      const current = ++revision;
      controller?.abort();
      controller = new AbortController();
      state = { status: "loading", module, data: null, error: null };
      try {
        const token = await getToken();
        if (current !== revision || controller.signal.aborted) return snapshot();
        const data = await readOperations({ baseUrl, module, token, fetchImpl, signal: controller.signal });
        if (current === revision && !controller.signal.aborted) {
          state = { status: "ready", module, data, error: null };
        }
      } catch (error) {
        if (current === revision && !controller.signal.aborted) {
          state = { status: "error", module, data: null,
            error: error?.status === 401 || error?.status === 403 ? "unauthorized" : "unavailable" };
        }
      }
      return snapshot();
    },
    clear() {
      ++revision;
      controller?.abort();
      state = { status: "idle", module: null, data: null, error: null };
      return snapshot();
    },
  };
}
