import type { Metadata } from "next";
import { ODPT_NOTICE_LINES } from "@bikechance/shared";
import { DataCredits } from "@/app/_components/data-credits";
import { ContactEmail, SectionView, TextPage } from "@/app/_components/text-page";
import { H2_STYLE } from "@/app/_components/styles";
import { readContactEmail } from "@/lib/site/contact";
import { SUPPORT_FAQ, SUPPORT_LEAD } from "@/lib/site/support";

/**
 * サポート（W10-07、W10 プラン §4.7）。App Store Connect のサポートの URL。
 * **お問い合わせは ODPT の通知文（開発者ガイドライン 3.1）そのまま**で、問い合わせ先は
 * アプリの「データについて」と同じ `CONTACT_EMAIL`（ビルドのとき）。
 */
export const metadata: Metadata = {
  title: "サポート",
  description: "BikeChance のよくある質問とお問い合わせ先。",
};

const contactEmail = readContactEmail(process.env);

const SupportPage = () => (
  <TextPage title="サポート" lead={SUPPORT_LEAD}>
    <h2 style={H2_STYLE}>よくある質問</h2>
    {SUPPORT_FAQ.map((section) => (
      <SectionView key={section.heading} section={section} level={3} />
    ))}
    <h2 style={H2_STYLE}>お問い合わせ</h2>
    {ODPT_NOTICE_LINES.map((line) => (
      <p key={line}>{line}</p>
    ))}
    <ContactEmail email={contactEmail} />
    <DataCredits />
  </TextPage>
);

export default SupportPage;
