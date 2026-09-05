import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // ワークスペースのソースをそのまま取り込む（ビルド成果物を持たない設計）
  transpilePackages: ["@bikechance/shared"],
};

export default nextConfig;
