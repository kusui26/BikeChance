/**
 * 文の頁（`/privacy`・`/support`）の中身の形（W10-07）。
 *
 * **文はデータに置き、頁の部品は並べるだけにする。** 文を読んで決めるのは利用者で、
 * 直すたびに部品を触らなくて済むようにする。数字（丸めの刻み・保持の日数・水平）は
 * 定数から組み、試験で突き合わせる。
 */

/** 箇条書きの 1 行。`term` があれば太字で前に置く（「現在地：…」の形）。 */
export type TextItem = {
  readonly term?: string;
  readonly text: string;
};

/** 頁の中のリンク。外へのリンクは `href` を `https://` で始める。 */
export type TextLink = {
  readonly label: string;
  readonly href: string;
};

/** 1 つの節。**段落 → 箇条書き → 注記 → リンク**の順に出す。 */
export type TextSection = {
  readonly heading: string;
  readonly paragraphs?: readonly string[];
  readonly items?: readonly TextItem[];
  /** 箇条書きの後に置く段落（「〜は送りません」のような締め）。 */
  readonly notes?: readonly string[];
  readonly links?: readonly TextLink[];
};
