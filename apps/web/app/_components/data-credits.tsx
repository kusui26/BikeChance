import {
  ATTRIBUTIONS,
  CREDIT_PREFACE,
  DATA_SOURCES,
  FORECAST_DISCLAIMER,
  formatCredit,
} from "@bikechance/shared";
import { H2_STYLE, H3_STYLE, MUTED_LIST_STYLE, MUTED_STYLE } from "./styles";

/**
 * 「データについて」（CC BY 4.0 のクレジット・そのほかの出典・予測の免責）。
 * **最初の頁とサポートの頁で同じものを出す**（開発プラン §3.7。文言は `@bikechance/shared`）。
 */
export const DataCredits = () => (
  <section>
    <h2 style={H2_STYLE}>データについて</h2>
    <p style={MUTED_STYLE}>{CREDIT_PREFACE}</p>
    <CreditList />
    <h3 style={H3_STYLE}>そのほかの出典</h3>
    <SourceList />
    <p style={MUTED_STYLE}>{FORECAST_DISCLAIMER}</p>
  </section>
);

const CreditList = () => (
  <ul style={MUTED_LIST_STYLE}>
    {ATTRIBUTIONS.map((attribution) => (
      <li key={attribution.system_id}>{formatCredit(attribution)}</li>
    ))}
  </ul>
);

const SourceList = () => (
  <ul style={MUTED_LIST_STYLE}>
    {DATA_SOURCES.map((source) => (
      <li key={source.id}>
        {source.text}（<a href={source.url}>{source.link_label}</a>）
      </li>
    ))}
  </ul>
);
