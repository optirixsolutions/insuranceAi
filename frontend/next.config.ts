import type { NextConfig } from "next";

// In docker compose, Nginx routes /api to FastAPI on the same origin.
// For `pnpm dev` without Nginx, set API_PROXY_TARGET=http://localhost:8000.
const apiProxyTarget = process.env.API_PROXY_TARGET;

const nextConfig: NextConfig = {
  output: "standalone",
  poweredByHeader: false,
  reactStrictMode: true,
  // Static assets bypass proxy.ts, so their security headers are set here (found by the ZAP baseline).
  async headers() {
    return [
      {
        source: "/_next/static/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Cross-Origin-Resource-Policy", value: "same-origin" },
          { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
        ],
      },
    ];
  },
  async rewrites() {
    if (!apiProxyTarget) return [];
    return [{ source: "/api/:path*", destination: `${apiProxyTarget}/api/:path*` }];
  },
};

export default nextConfig;
