import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  /* config options here */
  reactCompiler: true,
  async rewrites() {
    return [
      {
        source: "/api/job-search",
        destination: "https://jobscrappertelegrambot-production.up.railway.app/job-search",
      },
    ];
  },
};

export default nextConfig;
