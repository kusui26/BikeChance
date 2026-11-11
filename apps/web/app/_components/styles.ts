/**
 * 頁の見た目（W10-07）。**最初の頁（`app/page.tsx`）の書き方をそのまま定数にした**ので、
 * `/privacy`・`/support` も同じ幅・同じ字の大きさで並ぶ。
 */
import type { CSSProperties } from "react";

export const MAIN_STYLE: CSSProperties = {
  maxWidth: 720,
  margin: "0 auto",
  padding: "3rem 1.25rem",
};

export const TITLE_STYLE: CSSProperties = { fontSize: "1.75rem", marginBottom: "0.5rem" };

export const H2_STYLE: CSSProperties = { fontSize: "1.1rem", marginTop: "2.5rem" };

export const H3_STYLE: CSSProperties = { fontSize: "0.95rem", marginTop: "1.5rem" };

/** クレジット・免責のような、本文より控えめに出す文。 */
export const MUTED_STYLE: CSSProperties = { color: "#555", fontSize: "0.9rem" };

export const LIST_STYLE: CSSProperties = { paddingLeft: "1.2rem" };

export const MUTED_LIST_STYLE: CSSProperties = { ...MUTED_STYLE, ...LIST_STYLE };

/** 頁の行き来のリンクの並び（頁の下）。 */
export const NAV_STYLE: CSSProperties = {
  display: "flex",
  flexWrap: "wrap",
  gap: "0.5rem 1.25rem",
  marginTop: "3rem",
  paddingTop: "1rem",
  borderTop: "1px solid #ddd",
  fontSize: "0.9rem",
};
