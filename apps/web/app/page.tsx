import { ATTRIBUTIONS, FORECAST_DISCLAIMER, formatCredit } from "@bikechance/shared";

const Home = () => (
  <main style={{ maxWidth: 720, margin: "0 auto", padding: "3rem 1.25rem" }}>
    <h1 style={{ fontSize: "1.75rem", marginBottom: "0.5rem" }}>BikeChance</h1>
    <p style={{ marginTop: 0, fontSize: "1.05rem" }}>
      シェアサイクルのポートに着いたとき、自転車を借りられる／返せる確率を表示します。
    </p>
    <p style={{ color: "#555" }}>
      現在は開発中です。スマートフォン向け Web 版はこの場所に公開予定で、まずは iPhone
      アプリを先行して開発しています。
    </p>

    <h2 style={{ fontSize: "1.1rem", marginTop: "2.5rem" }}>データについて</h2>
    <p style={{ color: "#555", fontSize: "0.9rem" }}>
      このアプリは、以下の著作物を改変して利用しています。
    </p>
    <ul style={{ color: "#555", fontSize: "0.9rem", paddingLeft: "1.2rem" }}>
      {ATTRIBUTIONS.map((attribution) => (
        <li key={attribution.system_id}>{formatCredit(attribution)}</li>
      ))}
    </ul>
    <p style={{ color: "#555", fontSize: "0.9rem" }}>{FORECAST_DISCLAIMER}</p>
  </main>
);

export default Home;
