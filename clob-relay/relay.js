#!/usr/bin/env node
// === Polymarket CLOB Relay ===
// Run this on your local machine (outside US) to relay trade orders.
// Usage: npx bun relay.js   (or: node relay.js)
// Then paste the URL it prints into Vibecode's ENV tab as CLOB_PROXY_URL

const PORT = 8787;

const server = Bun?.serve?.({
  port: PORT,
  async fetch(request) {
    const url = new URL(request.url);

    if (url.pathname === "/" || url.pathname === "/health") {
      return new Response(JSON.stringify({ status: "ok", relay: "local-clob-proxy" }), {
        headers: { "Content-Type": "application/json" },
      });
    }

    // Forward to Polymarket CLOB API
    const target = `https://clob.polymarket.com${url.pathname}${url.search}`;
    const headers = new Headers(request.headers);
    headers.delete("host");

    const init = { method: request.method, headers };
    if (request.method !== "GET" && request.method !== "HEAD") {
      init.body = await request.text();
    }

    try {
      const res = await fetch(target, init);
      const body = await res.text();
      return new Response(body, {
        status: res.status,
        headers: { "Content-Type": res.headers.get("Content-Type") || "application/json" },
      });
    } catch (err) {
      return new Response(JSON.stringify({ error: err.message }), { status: 502 });
    }
  },
}) ?? require("http").createServer(async (req, res) => {
  // Node.js fallback
  const https = require("https");
  const url = new URL(req.url, `http://localhost:${PORT}`);

  if (url.pathname === "/" || url.pathname === "/health") {
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ status: "ok" }));
    return;
  }

  const target = `https://clob.polymarket.com${url.pathname}${url.search}`;
  let body = "";
  req.on("data", (chunk) => body += chunk);
  req.on("end", () => {
    const parsed = new URL(target);
    const headers = { ...req.headers };
    delete headers.host;
    if (body) headers["content-length"] = Buffer.byteLength(body);

    const proxyReq = https.request(parsed, { method: req.method, headers }, (proxyRes) => {
      res.writeHead(proxyRes.statusCode, { "Content-Type": proxyRes.headers["content-type"] || "application/json" });
      proxyRes.pipe(res);
    });
    proxyReq.on("error", (err) => {
      res.writeHead(502);
      res.end(JSON.stringify({ error: err.message }));
    });
    if (body) proxyReq.write(body);
    proxyReq.end();
  });
}).listen(PORT);

console.log(`\n🔗 CLOB Relay running on http://localhost:${PORT}`);
console.log(`\nTo expose this to the internet, run in another terminal:`);
console.log(`  npx localtunnel --port ${PORT}`);
console.log(`\nThen paste the URL into Vibecode ENV tab as CLOB_PROXY_URL\n`);
