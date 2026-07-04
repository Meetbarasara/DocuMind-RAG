import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Emit a self-contained server at .next/standalone/server.js so the Docker
  // image ships only the traced runtime files (no node_modules install needed).
  // See node_modules/next/dist/docs/.../config/.../output.md.
  output: "standalone",
};

export default nextConfig;
