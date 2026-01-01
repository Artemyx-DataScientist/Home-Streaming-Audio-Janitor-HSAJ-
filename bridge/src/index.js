import http from "node:http";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const RoonApi = require("node-roon-api");
const RoonApiStatus = require("node-roon-api-status");

/**
 * @typedef {"connected" | "disconnected"} RoonConnectionState
 */

/**
 * @typedef {Object} HealthResponse
 * @property {"ok"} status
 * @property {RoonConnectionState} roon
 */

/**
 * @param {RoonConnectionState} roonStatus
 * @returns {HealthResponse}
 */
const buildHealthResponse = (roonStatus) => ({ status: "ok", roon: roonStatus });

/**
 * @param {{ status: RoonConnectionState }} roonConnection
 * @returns {{ roon: unknown, statusService: unknown }}
 */
const createRoonClient = (roonConnection) => {
  /** @type {ReturnType<typeof RoonApiStatus>} */
  let statusService;

  const roon = new RoonApi({
    extension_id: "com.hsaj.bridge",
    display_name: "HSAJ Bridge",
    display_version: "0.1.0",
    publisher: "HSAJ",
    email: "opensource@hsaj.local",
    website: "https://example.com",
    core_paired: (core) => {
      roonConnection.status = "connected";
      statusService.set_status("OK", `Connected to ${core.display_name}`);
      console.log(`Connected to Roon Core: ${core.display_name} (${core.core_id})`);
    },
    core_unpaired: () => {
      roonConnection.status = "disconnected";
      statusService.set_status("Disconnected", "Waiting for Roon Core");
      console.warn("Lost connection to Roon Core. Waiting for re-connection...");
    },
  });

  statusService = new RoonApiStatus(roon);
  roon.init_services({ provided_services: [statusService] });
  statusService.set_status("Initializing", "Searching for Roon Core");
  roon.start_discovery();

  return { roon, statusService };
};

/**
 * @param {number} port
 * @param {() => HealthResponse} healthProvider
 * @returns {http.Server}
 */
const startHealthServer = (port, healthProvider) =>
  http.createServer((req, res) => {
    const url = req.url ? new URL(req.url, `http://${req.headers.host}`) : null;

    if (req.method === "GET" && url?.pathname === "/health") {
      const response = healthProvider();
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify(response));
      return;
    }

    res.writeHead(404, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ message: "Not Found" }));
  }).listen(port, () => {
    console.log(`Health endpoint listening on :${port}/health`);
  });

/**
 * Стартует dev-экземпляр bridge и подключает Roon.
 * @returns {void}
 */
const startBridge = () => {
  const port = Number.parseInt(process.env.BRIDGE_PORT ?? process.env.PORT ?? "8080", 10);
  const roonConnection = { status: /** @type {RoonConnectionState} */ ("disconnected") };

  createRoonClient(roonConnection);
  startHealthServer(port, () => buildHealthResponse(roonConnection.status));

  console.log("HSAJ bridge dev server started. Waiting for external event sources...");
};

startBridge();
