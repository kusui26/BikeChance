import type { Metadata } from "next";
import type { ReactNode } from "react";

export const metadata: Metadata = {
  title: "BikeChance",
  description: "シェアサイクルのポートに着いたとき、借りられる／返せる確率を表示します。",
};

const RootLayout = ({ children }: { children: ReactNode }) => (
  <html lang="ja">
    <body
      style={{
        margin: 0,
        fontFamily:
          "system-ui, -apple-system, 'Hiragino Kaku Gothic ProN', 'Noto Sans JP', sans-serif",
        lineHeight: 1.7,
        color: "#1a1a1a",
        background: "#fafafa",
      }}
    >
      {children}
    </body>
  </html>
);

export default RootLayout;
