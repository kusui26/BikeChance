#!/usr/bin/env python3
"""総務省「全国地方公共団体コード」の xlsx から市区町村コードの表を作る（W3 プラン §5.7）。

**Python なのは、xlsx が zip + XML で、標準ライブラリだけで読めるから。** Node で
同じことをすると zip を読むパッケージが要る。**新しいパッケージを足さないために
ここだけ Python にした**（ほかの `scripts/` は TypeScript / mjs）。

出力は 2 つ。

  --out <path>   supabase/seed/muni_codes.csv       出典の原本（人が読んでレビューする）
  --sql          マイグレーションの insert 文を標準出力へ

使い方（表が変わったとき＝市町村合併があったときだけ）:
  python3 scripts/gen-muni-codes.py --out supabase/seed/muni_codes.csv
  python3 scripts/gen-muni-codes.py --sql >> supabase/migrations/<ts>_00XX_muni_codes.sql

**ルビを落とすのを忘れない。** xlsx の共有文字列はふりがなを `<rPh>` に持っており、
素朴に `<t>` を全部つなぐと「相模原市サガミハラシ」になる。実際に一度踏んだ。
"""

import argparse
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Final

#: 総務省「全国地方公共団体コード」の一覧表（都道府県コード及び市区町村コード）。
#: 政令指定都市の区は 2 枚目のシートにある。
SOURCE_URL: Final[str] = "https://www.soumu.go.jp/main_content/000925835.xlsx"
NS: Final[str] = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def shared_strings(archive: zipfile.ZipFile) -> list[str]:
    """共有文字列。**`<rPh>`（ふりがな）の中の `<t>` は取らない。**"""
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    out: list[str] = []
    for si in root.findall(f"{NS}si"):
        parts: list[str] = []
        for child in si:
            if child.tag == f"{NS}rPh":
                continue  # ふりがな。本文ではない
            parts.extend(t.text or "" for t in child.iter(f"{NS}t"))
            if child.tag == f"{NS}t" and child.text:
                pass  # 上の iter が自分自身を拾うので二重に足さない
        out.append("".join(parts))
    return out


def read_rows(archive: zipfile.ZipFile, sheet: int) -> list[dict[str, str]]:
    strings = shared_strings(archive)
    root = ET.fromstring(archive.read(f"xl/worksheets/sheet{sheet}.xml"))
    rows: list[dict[str, str]] = []
    for row in root.iter(f"{NS}row"):
        cells: dict[str, str] = {}
        for cell in row.findall(f"{NS}c"):
            column = re.sub(r"\d+", "", cell.get("r") or "")
            value = cell.find(f"{NS}v")
            if value is not None and value.text:
                cells[column] = strings[int(value.text)] if cell.get("t") == "s" else value.text
            else:
                cells[column] = ""
        rows.append(cells)
    return rows


def collect(body: bytes) -> list[tuple[int, int, str, str]]:
    """`(muni_code, pref_code, pref_name, muni_name)` を重複なく集める。

    団体コードは 6 桁（5 桁＋検査数字）。**保存するのは 5 桁**で、先頭 2 桁が都道府県。
    都道府県だけの行（市区町村名が空）は落とす。
    """
    import io

    archive = zipfile.ZipFile(io.BytesIO(body))
    seen: set[tuple[str, str]] = set()
    out: list[tuple[int, int, str, str]] = []
    for sheet in (1, 2):
        for row in read_rows(archive, sheet):
            code, pref, muni = row.get("A", ""), row.get("B", ""), row.get("C", "")
            if not re.fullmatch(r"\d{6}", code) or not pref or not muni:
                continue
            if (pref, muni) in seen:
                continue
            seen.add((pref, muni))
            out.append((int(code[:5]), int(code[:2]), pref, muni))
    return sorted(out)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, help="CSV の書き出し先")
    parser.add_argument("--sql", action="store_true", help="insert 文を標準出力へ")
    args = parser.parse_args()

    request = urllib.request.Request(SOURCE_URL, headers={"User-Agent": "BikeChance/0.1"})
    with urllib.request.urlopen(request, timeout=60) as received:
        body = received.read()
        modified = received.headers.get("Last-Modified", "(不明)")
    rows = collect(body)
    if len(rows) < 1_500:
        raise SystemExit(f"行が少なすぎます: {len(rows)}")

    if args.out is not None:
        lines = [
            "# 全国地方公共団体コード（総務省）",
            f"# 出典: {SOURCE_URL}  last-modified: {modified}",
            "# 生成: python3 scripts/gen-muni-codes.py --out supabase/seed/muni_codes.csv",
            "# **手で編集しない。** 政令指定都市の区も含む。muni_code は 5 桁（検査数字を除く）",
            "muni_code,pref_code,pref_name,muni_name",
            *(f"{code},{pref},{pref_name},{muni_name}" for code, pref, pref_name, muni_name in rows),
        ]
        args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"{args.out} に {len(rows)} 件を書きました（last-modified: {modified}）", file=sys.stderr)

    if args.sql:
        print("insert into public.muni_codes (muni_code, pref_code, pref_name, muni_name) values")
        body_lines = [
            f"  ({code}, {pref}, '{pref_name}', '{muni_name}')"
            for code, pref, pref_name, muni_name in rows
        ]
        print(",\n".join(body_lines) + ";")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
