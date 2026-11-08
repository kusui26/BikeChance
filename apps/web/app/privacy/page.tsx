import type { Metadata } from "next";
import { ContactEmail, SectionView, TextPage } from "@/app/_components/text-page";
import { H2_STYLE } from "@/app/_components/styles";
import { readContactEmail } from "@/lib/site/contact";
import {
  PRIVACY_CONTACT_HEADING,
  PRIVACY_POLICY_LEAD,
  PRIVACY_POLICY_SECTIONS,
} from "@/lib/site/privacy-policy";

/**
 * プライバシーポリシー（W10-07、W10 プラン §4.7）。App Store Connect のプライバシーポリシーの URL。
 * **静的に作る**：問い合わせ先はビルドのときの `CONTACT_EMAIL`。
 */
export const metadata: Metadata = {
  title: "プライバシーポリシー",
  description: "BikeChance が扱う情報と、送る先、残る場所と期間。",
};

const contactEmail = readContactEmail(process.env);

const PrivacyPage = () => (
  <TextPage title="プライバシーポリシー" lead={PRIVACY_POLICY_LEAD}>
    {PRIVACY_POLICY_SECTIONS.map((section) => (
      <SectionView key={section.heading} section={section} level={2} />
    ))}
    <h2 style={H2_STYLE}>{PRIVACY_CONTACT_HEADING}</h2>
    <p>このポリシーについてのお問い合わせは、次のメールアドレスにお願いします。</p>
    <ContactEmail email={contactEmail} />
  </TextPage>
);

export default PrivacyPage;
