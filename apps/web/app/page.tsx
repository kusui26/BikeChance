import { DataCredits } from "@/app/_components/data-credits";
import { SiteLinks } from "@/app/_components/site-links";
import { MAIN_STYLE, TITLE_STYLE } from "@/app/_components/styles";

const Home = () => (
  <main style={MAIN_STYLE}>
    <h1 style={TITLE_STYLE}>BikeChance</h1>
    <p style={{ marginTop: 0, fontSize: "1.05rem" }}>
      シェアサイクルのポートに着いたとき、自転車を借りられる／返せる確率を表示します。
    </p>
    <p style={{ color: "#555" }}>
      現在は開発中です。スマートフォン向け Web 版はこの場所に公開予定で、まずは iPhone
      アプリを先行して開発しています。
    </p>
    <DataCredits />
    <SiteLinks />
  </main>
);

export default Home;
