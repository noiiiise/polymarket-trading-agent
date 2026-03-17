// Cloudflare Worker: CLOB API Relay
// Forwards requests to Polymarket's CLOB API from a non-geoblocked region.
// Deploy: npx wrangler deploy

export default {
  async fetch(request) {
    const url = new URL(request.url);

    // Health check
    if (url.pathname === "/" || url.pathname === "/health") {
      return new Response(JSON.stringify({ status: "ok", relay: "clob-proxy" }), {
        headers: { "Content-Type": "application/json" },
      });
    }

    // Forward everything under /order, /auth, /cancel, etc. to CLOB
    const target = `https://clob.polymarket.com${url.pathname}${url.search}`;

    // Clone headers, removing host
    const headers = new Headers(request.headers);
    headers.delete("host");
    headers.delete("cf-connecting-ip");
    headers.delete("cf-ipcountry");

    const init = {
      method: request.method,
      headers,
    };

    // Forward body for POST/PUT
    if (request.method !== "GET" && request.method !== "HEAD") {
      init.body = await request.text();
    }

    try {
      const response = await fetch(target, init);
      const body = await response.text();

      return new Response(body, {
        status: response.status,
        statusText: response.statusText,
        headers: {
          "Content-Type": response.headers.get("Content-Type") || "application/json",
          "Access-Control-Allow-Origin": "*",
        },
      });
    } catch (err) {
      return new Response(JSON.stringify({ error: err.message }), {
        status: 502,
        headers: { "Content-Type": "application/json" },
      });
    }
  },
};
