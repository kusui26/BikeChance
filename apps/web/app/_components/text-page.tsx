import Link from "next/link";
import type { ReactNode } from "react";
import { CONTACT_PENDING_TEXT } from "@/lib/site/contact";
import type { TextItem, TextLink, TextSection } from "@/lib/site/text";
import { SiteLinks } from "./site-links";
import { H2_STYLE, H3_STYLE, LIST_STYLE, MAIN_STYLE, TITLE_STYLE } from "./styles";

type TextPageProps = {
  readonly title: string;
  readonly lead: readonly string[];
  readonly children: ReactNode;
};

/** 文の頁の枠（W10-07）：題・前書き・本文・頁の行き来。 */
export const TextPage = ({ title, lead, children }: TextPageProps) => (
  <main style={MAIN_STYLE}>
    <p style={{ margin: 0 }}>
      <Link href="/">BikeChance</Link>
    </p>
    <h1 style={TITLE_STYLE}>{title}</h1>
    <Paragraphs lines={lead} />
    {children}
    <SiteLinks />
  </main>
);

type SectionProps = {
  readonly section: TextSection;
  /** 見出しの段。プライバシーポリシーの節は 2、よくある質問の 1 問は 3。 */
  readonly level: 2 | 3;
};

/** 1 つの節。**段落 → 箇条書き → 注記 → リンク**の順（`TextSection` の決まり）。 */
export const SectionView = ({ section, level }: SectionProps) => (
  <section>
    <Heading level={level}>{section.heading}</Heading>
    <Paragraphs lines={section.paragraphs} />
    <Items items={section.items} />
    <Paragraphs lines={section.notes} />
    <Links links={section.links} />
  </section>
);

/** 問い合わせ先。**ビルドに無ければ「準備中」**と出す（`readContactEmail`）。 */
export const ContactEmail = ({ email }: { readonly email: string | null }) =>
  email === null ? (
    <p>{CONTACT_PENDING_TEXT}</p>
  ) : (
    <p>
      <a href={`mailto:${email}`}>{email}</a>
    </p>
  );

const Heading = ({ level, children }: { readonly level: 2 | 3; readonly children: ReactNode }) =>
  level === 2 ? <h2 style={H2_STYLE}>{children}</h2> : <h3 style={H3_STYLE}>{children}</h3>;

const Paragraphs = ({ lines }: { readonly lines: readonly string[] | undefined }) => (
  <>
    {(lines ?? []).map((line) => (
      <p key={line}>{line}</p>
    ))}
  </>
);

const Items = ({ items }: { readonly items: readonly TextItem[] | undefined }) =>
  items === undefined ? null : (
    <ul style={LIST_STYLE}>
      {items.map((item) => (
        <li key={item.text}>
          <ItemText item={item} />
        </li>
      ))}
    </ul>
  );

const ItemText = ({ item }: { readonly item: TextItem }) =>
  item.term === undefined ? (
    <>{item.text}</>
  ) : (
    <>
      <strong>{item.term}</strong>：{item.text}
    </>
  );

const Links = ({ links }: { readonly links: readonly TextLink[] | undefined }) =>
  links === undefined ? null : (
    <ul style={LIST_STYLE}>
      {links.map((link) => (
        <li key={link.href}>
          <LinkView link={link} />
        </li>
      ))}
    </ul>
  );

/** 頁の中は `Link`、外は素の `a`。 */
const LinkView = ({ link }: { readonly link: TextLink }) =>
  link.href.startsWith("/") ? (
    <Link href={link.href}>{link.label}</Link>
  ) : (
    <a href={link.href}>{link.label}</a>
  );
