import Link from "next/link";
import { NAV_STYLE } from "./styles";

/** 頁の行き来（W10-07）。App Store Connect に入れるのは `/support` と `/privacy`。 */
const SITE_LINKS = [
  { label: "BikeChance について", href: "/" },
  { label: "サポート", href: "/support" },
  { label: "プライバシーポリシー", href: "/privacy" },
] as const;

export const SiteLinks = () => (
  <nav aria-label="このサイトの頁" style={NAV_STYLE}>
    {SITE_LINKS.map((link) => (
      <Link key={link.href} href={link.href}>
        {link.label}
      </Link>
    ))}
  </nav>
);
