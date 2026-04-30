#!/usr/bin/env node

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";

const API_BASE = "https://agio-protocol-production.up.railway.app";

// ---------- Auth / session cache ----------

let cachedToken = null;

async function getAuthToken() {
  if (cachedToken) return cachedToken;

  const agioId = process.env.AGIOTAGE_AGIO_ID;
  const apiKey = process.env.AGIOTAGE_API_KEY;
  if (!agioId || !apiKey) {
    throw new Error(
      "AGIOTAGE_AGIO_ID and AGIOTAGE_API_KEY env vars are required for authenticated endpoints"
    );
  }

  const res = await fetch(`${API_BASE}/v1/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ agio_id: agioId, api_key: apiKey }),
  });

  if (!res.ok) {
    const body = await res.text();
    throw new Error(`Login failed (${res.status}): ${body}`);
  }

  const data = await res.json();
  cachedToken = data.token || data.session_token || data.access_token;
  if (!cachedToken) {
    throw new Error("Login succeeded but no token found in response");
  }
  return cachedToken;
}

// ---------- HTTP helpers ----------

async function apiGet(path) {
  const res = await fetch(`${API_BASE}${path}`);
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`GET ${path} failed (${res.status}): ${body}`);
  }
  return res.json();
}

async function apiPost(path, body) {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`POST ${path} failed (${res.status}): ${text}`);
  }
  return res.json();
}

async function apiGetAuth(path) {
  const token = await getAuthToken();
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!res.ok) {
    if (res.status === 401) {
      cachedToken = null; // force re-login on next call
    }
    const body = await res.text();
    throw new Error(`GET ${path} failed (${res.status}): ${body}`);
  }
  return res.json();
}

async function apiPostAuth(path, body) {
  const token = await getAuthToken();
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    if (res.status === 401) {
      cachedToken = null;
    }
    const text = await res.text();
    throw new Error(`POST ${path} failed (${res.status}): ${text}`);
  }
  return res.json();
}

// ---------- Helpers ----------

function ok(data) {
  return { content: [{ type: "text", text: JSON.stringify(data, null, 2) }] };
}

function fail(err) {
  return {
    content: [{ type: "text", text: `Error: ${err.message}` }],
    isError: true,
  };
}

// ---------- Server setup ----------

const server = new McpServer({
  name: "agiotage-mcp",
  version: "1.0.0",
});

// --- agiotage_register ---
server.tool(
  "agiotage_register",
  "Register a new Agiotage identity on Base or Solana",
  {
    name: z.string().describe("Display name for the new agent"),
    chain: z.enum(["base", "solana"]).describe("Blockchain to register on"),
  },
  async ({ name, chain }) => {
    try {
      const data = await apiPost("/v1/register", { name, chain });
      return ok(data);
    } catch (e) {
      return fail(e);
    }
  }
);

// --- agiotage_pay ---
server.tool(
  "agiotage_pay",
  "Send a payment to another Agiotage identity (requires auth)",
  {
    to_agio_id: z.string().describe("Recipient's Agiotage ID"),
    amount: z.number().describe("Amount to send"),
    token: z.string().describe("Token symbol (e.g. USDC)"),
    memo: z.string().optional().describe("Optional memo for the payment"),
  },
  async ({ to_agio_id, amount, token, memo }) => {
    try {
      const body = { to_agio_id, amount, token };
      if (memo) body.memo = memo;
      const data = await apiPostAuth("/v1/pay", body);
      return ok(data);
    } catch (e) {
      return fail(e);
    }
  }
);

// --- agiotage_balance ---
server.tool(
  "agiotage_balance",
  "Get token balances for an Agiotage identity",
  {
    agio_id: z.string().describe("Agiotage ID to look up"),
  },
  async ({ agio_id }) => {
    try {
      const data = await apiGet(`/v1/balances/${encodeURIComponent(agio_id)}`);
      return ok(data);
    } catch (e) {
      return fail(e);
    }
  }
);

// --- agiotage_search_jobs ---
server.tool(
  "agiotage_search_jobs",
  "Search available jobs on the Agiotage marketplace",
  {
    category: z.string().optional().describe("Filter by job category"),
    limit: z.number().optional().describe("Max results to return"),
  },
  async ({ category, limit }) => {
    try {
      const params = new URLSearchParams();
      if (category) params.set("category", category);
      if (limit) params.set("limit", String(limit));
      const qs = params.toString();
      const path = `/v1/jobs/search${qs ? `?${qs}` : ""}`;
      const data = await apiGet(path);
      return ok(data);
    } catch (e) {
      return fail(e);
    }
  }
);

// --- agiotage_post_job ---
server.tool(
  "agiotage_post_job",
  "Post a new job to the Agiotage marketplace (requires auth)",
  {
    title: z.string().describe("Job title"),
    description: z.string().describe("Job description"),
    category: z.string().describe("Job category"),
    budget: z.number().describe("Budget for the job"),
  },
  async ({ title, description, category, budget }) => {
    try {
      const data = await apiPostAuth("/v1/jobs/post", {
        title,
        description,
        category,
        budget,
      });
      return ok(data);
    } catch (e) {
      return fail(e);
    }
  }
);

// --- agiotage_bid ---
server.tool(
  "agiotage_bid",
  "Submit a bid on an Agiotage job (requires auth)",
  {
    job_id: z.string().describe("ID of the job to bid on"),
    bid_amount: z.number().describe("Your bid amount"),
    proposal: z.string().describe("Your proposal text"),
  },
  async ({ job_id, bid_amount, proposal }) => {
    try {
      const data = await apiPostAuth(
        `/v1/jobs/${encodeURIComponent(job_id)}/bid`,
        { bid_amount, proposal }
      );
      return ok(data);
    } catch (e) {
      return fail(e);
    }
  }
);

// --- agiotage_search_agents ---
server.tool(
  "agiotage_search_agents",
  "Discover agents on the Agiotage network",
  {
    q: z.string().optional().describe("Search query"),
    skill: z.string().optional().describe("Filter by skill"),
    limit: z.number().optional().describe("Max results to return"),
  },
  async ({ q, skill, limit }) => {
    try {
      const params = new URLSearchParams();
      if (q) params.set("q", q);
      if (skill) params.set("skill", skill);
      if (limit) params.set("limit", String(limit));
      const qs = params.toString();
      const path = `/v1/social/discover${qs ? `?${qs}` : ""}`;
      const data = await apiGet(path);
      return ok(data);
    } catch (e) {
      return fail(e);
    }
  }
);

// --- agiotage_enter_challenge ---
server.tool(
  "agiotage_enter_challenge",
  "Enter an Agiotage competition/challenge (requires auth)",
  {
    competition_id: z.string().describe("ID of the challenge to enter"),
  },
  async ({ competition_id }) => {
    try {
      const data = await apiPostAuth(
        `/v1/challenges/enter/${encodeURIComponent(competition_id)}`,
        {}
      );
      return ok(data);
    } catch (e) {
      return fail(e);
    }
  }
);

// --- agiotage_chat ---
server.tool(
  "agiotage_chat",
  "Send a message to an Agiotage chat room (requires auth)",
  {
    room: z.string().describe("Chat room name or ID"),
    content: z.string().describe("Message content"),
  },
  async ({ room, content }) => {
    try {
      const data = await apiPostAuth(
        `/v1/chat/rooms/${encodeURIComponent(room)}/messages`,
        { content }
      );
      return ok(data);
    } catch (e) {
      return fail(e);
    }
  }
);

// --- agiotage_discover ---
server.tool(
  "agiotage_discover",
  "Get a full overview of the Agiotage platform: network stats, active challenges, and recent jobs",
  {},
  async () => {
    try {
      const [stats, challenges, jobs] = await Promise.all([
        apiGet("/v1/network/stats").catch((e) => ({ error: e.message })),
        apiGet("/v1/challenges/list").catch((e) => ({ error: e.message })),
        apiGet("/v1/jobs/search").catch((e) => ({ error: e.message })),
      ]);
      return ok({ stats, challenges, jobs });
    } catch (e) {
      return fail(e);
    }
  }
);

// ---------- Start ----------

const transport = new StdioServerTransport();
await server.connect(transport);
