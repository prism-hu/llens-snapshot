#!/usr/bin/env python3
"""DPC (diagnosis procedure combination) electronic fee table -> nodes under
`data/doc/診療報酬/DPC点数表R08/**`.

Input is `cache/dpc/` as left by `scripts/fetch_dpc.py` (the xlsx dumped to JSON plus the
notification texts). **Runs on the standard library only** (openpyxl lives on the fetch side).

Design (primary source: `docs/cage/21_dpc.md`):

  - **1 node = 1 classification (MDC 2 digits + classification code 4 digits, 504 of them)**.
    One directory level per MDC
  - A classification node is ordered so that **reading it alone lets you build the 14-digit
    code and look up the points**:
      1. Name, MDC, 6 digits + **how to assemble the 14 digits for this classification**
         (only the axes in use; unused axes are x)
      2. Target diseases (ICD). If many, child node `対象傷病` (ICD_SPLIT)
      3. Branch definitions (surgery K code -> digits 9-10 / procedure 1 -> digit 11 /
         procedure 2 -> digit 12 / defined comorbidity -> digit 13 / condition, age, severity).
         If many surgeries, child node `手術の定義` (SURGERY_SPLIT)
      4. Fee table (every 14-digit code of this classification. Stay days I/II/III and points
         I/II/III. Non-bundled rows are stated explicitly)
      5. Caveats (before coefficients, beyond stay day III is fee-for-service, final call is
         the billing office and the attending physician, version)
  - Auxiliary nodes sit directly under the section with a `00_` prefix (how to read /
    fee-for-service surgery codes / dummy codes / hospital coefficients / notes-of-attention
    notification / participation-procedure notification)
  - The original xlsx is served as-is under `data/static/dpc/` (for pandas in the sandbox).
    build copies it and removes old versions. verify checks the sha256

usage: uv run python scripts/build_dpc.py     (run from cage/. Idempotent)
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from textfix import DOC_BASE, doc_ref, safe_segment, static_ref  # noqa: E402

CACHE = Path("cache/dpc")
CATEGORY = Path("data/doc/診療報酬")
SECTION_NAME = "DPC点数表R08"
SECTION = CATEGORY / SECTION_NAME
SEC_DOC = f"/診療報酬/{SECTION_NAME}"  # node identifier (internal). Use leaf() / ddir() for the canonical form in bodies
STATIC_DIR = Path("data/static/dpc")

TENSUHYO = "診断群分類 (DPC) 電子点数表 (令和8年度診療報酬改定)"
PUBLISHER = "厚生労働省"
APPLIES_FROM = "2026-06-01"
SOURCE = "https://www.mhlw.go.jp/stf/newpage_67729.html"
KEISU_STATIC = "keisu_beppyo_R08.xlsx"  # annex tables of the coefficient notification (MHLW Notification No. 99 of 2026)

# Large blocks go to child nodes (same idea as CODE_TABLE_SPLIT in the medical fee schedule)
ICD_SPLIT = 50  # more target-disease (ICD) rows than this -> `対象傷病` child node
SURGERY_SPLIT = 100  # more surgery (K code) rows than this -> `手術の定義` child node
TSUCHI_SPLIT = 20_000  # notification body longer than this -> one child node per "第N" chapter

ICD_CHILD = "対象傷病"
SURGERY_CHILD = "手術の定義"

# Auxiliary nodes (directly under the section). `00_` so they sort before the MDCs
N_YOMIKATA = "00_DPCの読み方"
N_DEKIDAKA = "00_出来高算定手術等コード"
N_DUMMY = "00_ダミーコード一覧"
N_KEISU = "00_北大病院の係数"
N_RYUI = "00_DPC留意事項通知"
N_SANKA = "00_DPC参加手続き通知"

HOSPITAL = "北海道大学病院"


def leaf(path: str) -> str:
    """Leaf node path -> canonical form for bodies `{{DOC_BASE}}/…/<name>.md`."""
    return doc_ref(path, is_dir=False)


def ddir(path: str) -> str:
    """Directory node path -> canonical form for bodies `{{DOC_BASE}}/…/`."""
    return doc_ref(path, is_dir=True)


def aux(name: str) -> str:
    """Body form of a leaf auxiliary node (`00_…`) directly under the section. The notes-of-attention
    notification may be split, so use D.ryui_ref for it."""
    return leaf(f"{SEC_DOC}/{name}")


# Neighbouring section: the DPC annex of the Q&A interpretations (built by `build_gigi_kaishaku.py`).
# Without it only the section-level pointer is emitted
QA_DPC_DOC = "/診療報酬/疑義解釈R08/DPC"
QA_DPC_DIR = Path("data/doc") / QA_DPC_DOC.lstrip("/")

DIGIT_AXES = [
    ("1〜2", "MDC"),
    ("3〜6", "分類コード"),
    ("7", "病態等分類"),
    ("8", "年齢・出生時体重等"),
    ("9〜10", "手術"),
    ("11", "手術・処置等１"),
    ("12", "手術・処置等２"),
    ("13", "定義副傷病"),
    ("14", "重症度等"),
]

CODE_RE = re.compile(r"^\d{2}[0-9x]{4}[0-9A-Zx]{8}$")
MDC_RE = re.compile(r"^\d{2}$")
BUNRUI_RE = re.compile(r"^[0-9]{3}[0-9x]$")

# ---------------------------------------------------------------------------
# Loading the originals
# ---------------------------------------------------------------------------


def load_json(name: str) -> dict[str, list[list[str]]]:
    return json.loads((CACHE / name).read_text(encoding="utf-8"))


def sheet(S: dict, prefix: str) -> list[list[str]]:
    """Look up a sheet by name prefix (trailing text varies, e.g. '６）手術')."""
    for k, v in S.items():
        if k.startswith(prefix):
            return v
    sys.exit(f"sheet not found: {prefix!r} (have {list(S)})")


def keyed(rows: list[list[str]]) -> dict[str, list[list[str]]]:
    """Group only rows with MDC 2 digits + classification code 4 digits by the 6-digit key (drops header rows)."""
    out: dict[str, list[list[str]]] = defaultdict(list)
    for r in rows:
        if MDC_RE.match(r[0]) and BUNRUI_RE.match(r[1]):
            out[r[0] + r[1]].append(r)
    return out


def _find_col(rows: list[list[str]], text: str) -> int | None:
    for h in rows[:4]:
        for i, c in enumerate(h):
            if c.replace("\n", "") == text:
                return i
    return None


HENKOU = {"1": "抹消", "2": "復活", "3": "新規", "5": "変更", "9": "廃止"}


def valid_note(r: list[str], col: int) -> str:
    """Note appended to rows whose change category / validity period (start, end) differs from the
    defaults (0 / applies-from date / 99999999). In the 2026 edition, 29 high-cost drug rows of the
    fee-for-service surgery codes and 3 rows of procedure 2 take effect 2026-08-13. Mixed in
    silently they would read as "effective June 1"."""
    v, st, en = (r[col] if col < len(r) else ""), (r[col + 1] if col + 1 < len(r) else ""), (r[col + 2] if col + 2 < len(r) else "")
    parts = []
    if v not in ("", "0"):
        parts.append(f"変更区分 {v} {HENKOU.get(v, '')}".strip())
    if st and st != APPLIES_FROM.replace("-", ""):
        parts.append(f"{st[:4]}-{st[4:6]}-{st[6:]} から")
    if en and en != "99999999":
        parts.append(f"{en[:4]}-{en[4:6]}-{en[6:]} まで")
    return f" **({'、'.join(parts)})**" if parts else ""


def assert_header(rows: list[list[str]], must: dict[int, str], name: str) -> None:
    """Check that header columns are where we expect. **Cells are read by column index**, so
    stop here if the original's columns moved (never silently read another column)."""
    heads = rows[:4]
    for col, text in must.items():
        if not any(len(h) > col and text in h[col] for h in heads):
            sys.exit(f"{name}: header of column {col} lacks {text!r} (column layout of the original changed)")


class Data:
    def __init__(self) -> None:
        S = load_json("dpc.json")
        self.manifest = json.loads((CACHE / "manifest.json").read_text(encoding="utf-8"))
        self.mdc = {
            r[0]: r[1] for r in sheet(S, "１）ＭＤＣ名称") if MDC_RE.match(r[0]) and r[1]
        }
        s2 = sheet(S, "２）分類名称")
        assert_header(s2, {2: "名称", 3: "変更区分"}, "２）分類名称")
        self.bunrui = {r[0] + r[1]: r for r in s2 if MDC_RE.match(r[0]) and BUNRUI_RE.match(r[1])}
        s3 = sheet(S, "３）病態等分類")
        assert_header(s3, {2: "対応", 4: "年齢", 6: "院内肺炎"}, "３）病態等分類")
        self.byotai = keyed(s3)
        s4 = sheet(S, "４）ＩＣＤ")
        assert_header(s4, {2: "ICD名称", 3: "ICD", 4: "変更区分"}, "４）ＩＣＤ")
        self.icd = keyed(s4)
        s5 = sheet(S, "５）年齢")
        assert_header(s5, {2: "条件区分", 3: "条件名", 4: "条件1", 21: "変更区分"}, "５）年齢")
        self.age = keyed(s5)
        s6 = sheet(S, "６）手術")
        assert_header(s6, {3: "手術フラグ", 5: "対応", 6: "手術１", 7: "Ｋコード", 16: "変更区分"}, "６）手術")
        self.surg = keyed(s6)
        s7 = sheet(S, "７）手術・処置等１")
        assert_header(s7, {2: "対応", 3: "処置1", 4: "組み合わせ", 5: "処置等（１）", 9: "変更区分"}, "７）")
        self.p1 = keyed(s7)
        s8 = sheet(S, "８）手術・処置等２")
        assert_header(s8, {2: "対応", 3: "処置2", 4: "処置等(1)", 8: "変更区分"}, "８）")
        self.p2 = keyed(s8)
        s9 = sheet(S, "９）定義副傷病名")
        assert_header(s9, {2: "対応", 3: "定義副傷病", 4: "ICD名称", 5: "ICD", 6: "変更区分"}, "９）")
        self.fuku = keyed(s9)
        self.sev1 = keyed(sheet(S, "10－1）"))
        self.sev2 = keyed(sheet(S, "10－2）"))
        self.sev3 = keyed(sheet(S, "10－3）"))
        self.sev4 = keyed(sheet(S, "10－4）"))
        s11 = sheet(S, "11）診断群分類点数表")
        assert_header(s11, {1: "番号", 2: "診断群分類番号", 3: "傷病名", 4: "手術名", 9: "入院日", 12: "点数", 15: "変更"}, "11）")
        self.points_rows = [r for r in s11 if CODE_RE.match(r[2])]
        self.points: dict[str, list[list[str]]] = defaultdict(list)
        for r in self.points_rows:
            self.points[r[2][:6]].append(r)
        s12 = sheet(S, "12）変換テーブル")
        assert_header(s12, {1: "診断群分類番号", 2: "包括フラグ", 14: "手術", 17: "定義副傷病"}, "12）")
        self.conv = [r for r in s12 if CODE_RE.match(r[1])]
        self.hokatsu: dict[str, str] = {}
        for r in self.conv:
            prev = self.hokatsu.setdefault(r[1], r[2])
            if prev != r[2]:
                sys.exit(f"conversion table: bundling flag of {r[1]} differs between rows")
        s13 = sheet(S, "13)出来高算定手術等コード")
        assert_header(s13, {1: "手術コード", 2: "手術コード名称", 3: "変更区分"}, "13)")
        # **Some rows have an empty surgery code** (MHLW-designated high-cost drugs; name only) — keep them
        self.dekidaka = [r for r in s13 if r[0] in ("00", "01") and (r[1] or r[2])]
        s14 = sheet(S, "14）CCPM対応")
        self.ccpm = keyed(s14)
        sd = sheet(S, "ダミーコード一覧")
        self.dummy = [r for r in sd if re.fullmatch(r"[0-9A-Z]{4}", r[2]) and r[3]]
        self.zentei = sheet(S, "ＤＰＣ電子点数表の前提条件")
        K = load_json("keisu_beppyo.json")
        self.keisu = {k: v for k, v in K.items()}
        # Change category / validity period (columns common to all sheets). Column index differs per
        # sheet, so locate it from the header. Collect rows that are not 0 (new 3 / changed 5 /
        # deleted 1 / abolished 9) and rows whose start date differs from the applies-from date
        # = material for "classifications to re-read" after a revision/correction
        # (`docs/cage/21_dpc.md`, "re-fetch procedure")
        self.changed: dict[str, Counter] = defaultdict(Counter)  # classification -> {sheet:category: rows}
        self.changed_sheets: Counter = Counter()  # sheet -> number of non-0 rows
        for name, rows in S.items():
            col = _find_col(rows, "変更区分")
            if col is None:
                continue
            for r in rows:
                v = r[col] if col < len(r) else ""
                if v in ("", "0") or "変更" in v:
                    continue
                self.changed_sheets[name] += 1
                key = r[2][:6] if name.startswith("11）") else (r[0] + r[1] if MDC_RE.match(r[0]) and BUNRUI_RE.match(r[1]) else "-")
                self.changed[key][f"{name[:12]}:{v}"] += 1

        # Whether the notes-of-attention notification splits into per-"第N" child nodes (= whether
        # `00_DPC留意事項通知` is a directory or a leaf). This decides how classification / how-to-read
        # nodes reference the notification, so it must be known before writing any node
        self.ryui_is_dir = ryui_parts()[2] > TSUCHI_SPLIT

        # Reverse-index the classification paths referenced by the neighbouring section (DPC annex
        # of the Q&A interpretations): classification key -> Q&A node paths
        self.qa_by_key: dict[str, list[str]] = defaultdict(list)
        self.qa_nodes = 0
        self.qa_note = ""
        if QA_DPC_DIR.exists():
            # Match the identifier part inside the canonical form (`{{DOC_BASE}}/…/<6 digits>_<name>.md` or `…/`)
            pat = re.compile(re.escape(SEC_DOC) + r"/[^/`\s]+/(\d{2}[0-9]{3}[0-9x])_[^/`\s]*")
            for md in sorted(QA_DPC_DIR.glob("*.md")):
                if md.name == "_index.md":
                    m = re.search(r"収録時点: \*\*(.+?)\*\*", md.read_text(encoding="utf-8"))
                    self.qa_note = m.group(1) if m else ""
                    continue
                self.qa_nodes += 1
                doc = f"{QA_DPC_DOC}/{md.stem}"
                for key in sorted({m.group(1) for m in pat.finditer(md.read_text(encoding="utf-8"))}):
                    self.qa_by_key[key].append(doc)
        else:
            print(f"  ! {QA_DPC_DIR} missing. Q&A pointers fall back to the section-level one (build the neighbouring section, then rerun)", file=sys.stderr)

    # version
    @property
    def version(self) -> dict:
        return self.manifest["dpc_version"]

    @property
    def retrieved(self) -> str:
        return self.manifest["retrieved_at"]

    @property
    def static_xlsx(self) -> str:
        return f"dpc_{self.version['updated'].replace('-', '')}.xlsx"

    @property
    def static_url(self) -> str:
        return static_ref(f"dpc/{self.static_xlsx}")

    @property
    def keisu_url(self) -> str:
        return static_ref(f"dpc/{KEISU_STATIC}")

    @property
    def ryui_ref(self) -> str:
        """Body form of the notes-of-attention notification node (directory if split, else leaf)."""
        p = f"{SEC_DOC}/{N_RYUI}"
        return ddir(p) if self.ryui_is_dir else leaf(p)

    def version_label(self) -> str:
        v = self.version
        return f"{v['label']}{v['note']} = {v['rank']} (更新 {v['updated']})"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FS_BAD = re.compile(r'[/\\:*?"<>|\x00-\x1f]')


def fs_safe(s: str) -> str:
    # The name becomes a URL path: whitespace -> `_`, URL-breaking ASCII -> full-width (textfix.safe_segment)
    return safe_segment(_FS_BAD.sub("_", s).strip())


def yaml_str(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def md_cell(s: str) -> str:
    return s.replace("|", "｜").replace("\n", " ").strip()


def z2h(s: str) -> str:
    """Full-width alphanumerics to half-width (normalises K codes and digits; names untouched)."""
    return s.translate(str.maketrans("０１２３４５６７８９ＡＢＣＤＥＦ", "0123456789ABCDEF"))


CHILD_FIELDS = [
    "fields:",
    "  mdc: str",
    "  bunrui: str",
    "  section: str",
    "  source?: str",
    "  retrieved_at?: date",
]


def front_matter(
    D: Data, title: str, mdc: str, bunrui: str, section: str, *, child_fields: bool
) -> list[str]:
    """Front matter shared by classification nodes, MDC directories and auxiliary nodes. Directory
    nodes add `fields:` to declare the closed schema of their direct children (`01_brief.md`)."""
    fm = [
        "---",
        f"title: {yaml_str(title)}",
        f"mdc: {yaml_str(mdc)}",
        f"bunrui: {yaml_str(bunrui)}",
        f"section: {yaml_str(section)}",
        f"source: {SOURCE}",
        f"retrieved_at: {yaml_str(D.retrieved)}",
    ]
    if child_fields:
        fm += CHILD_FIELDS
    return fm + ["---", ""]


def table(head: list[str], rows: list[list[str]]) -> list[str]:
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(md_cell(c) for c in r) + " |" for r in rows]
    return out + [""]


UNITS = {
    "年齢": "歳",
    "月齢": "か月",
    "出生時体重": "g",
    "妊娠週数": "週",
    "分娩時出血量": "mL",
}


def fmt_range(name: str, lo: str, hi: str) -> str:
    """Turn a range like '000'-'015' into the "under 15 years" form. Minimum (000...) and maximum
    (999...) denote open ends, per the original's convention (preconditions sheet)."""
    unit = UNITS.get(name, "")
    head = "" if name in UNITS else f"{name} "

    def num(v: str) -> str:
        return str(int(v)) if v.isdigit() else v

    lo_min = lo == "" or set(lo) <= {"0"}
    hi_max = hi == "" or set(hi) <= {"9"} or hi == "100000"
    if lo_min and hi_max:
        return f"{head}すべて"
    if lo_min:
        return f"{head}{num(hi)}{unit}未満"
    if hi_max:
        return f"{head}{num(lo)}{unit}以上"
    return f"{head}{num(lo)}{unit}以上{num(hi)}{unit}未満"


# ---------------------------------------------------------------------------
# Classification nodes
# ---------------------------------------------------------------------------


def digit_values(codes: list[str]) -> list[set[str]]:
    """Set of values seen at each of the 14 digit positions (0..13)."""
    out = [set() for _ in range(14)]
    for c in codes:
        for i, ch in enumerate(c):
            out[i].add(ch)
    return out


def names_by_value(rows: list[list[str]], pos: slice, col: int) -> dict[str, list[str]]:
    """From fee-table rows: digit value -> candidate row names (in order of appearance, deduplicated)."""
    out: dict[str, list[str]] = {}
    for r in rows:
        v = r[2][pos]
        lst = out.setdefault(v, [])
        if r[col] and r[col] not in lst:
            lst.append(r[col])
    return out


def used(vals: set[str]) -> bool:
    return any(v != "x" for v in vals)


def axis_summary(D: Data, key: str, rows: list[list[str]]) -> tuple[list[str], dict]:
    """The "assembling the 14-digit code" table. Returns (table lines, which axes are used)."""
    codes = [r[2] for r in rows]
    dv = digit_values(codes)
    mdc, bun = key[:2], key[2:]
    info = {
        "byotai": used(dv[6]),
        "age": used(dv[7]),
        "surg": used(dv[8]) or used(dv[9]),
        "p1": used(dv[10]),
        "p2": used(dv[11]),
        "fuku": used(dv[12]),
        "sev": used(dv[13]),
    }
    surg_names = names_by_value(rows, slice(8, 10), 4)
    p1_names = names_by_value(rows, slice(10, 11), 5)
    p2_names = names_by_value(rows, slice(11, 12), 6)
    fuku_names = names_by_value(rows, slice(12, 13), 7)
    sev_names = names_by_value(rows, slice(13, 14), 8)

    def vals(names: dict[str, list[str]], defined: list[str] | None = None) -> str:
        parts = []
        for v in sorted(names, key=lambda x: (x == "x", x)):
            if v == "x":
                continue
            label = " / ".join(names[v]) if names[v] else ""
            parts.append(f"`{v}` = {label}".rstrip(" =") if label else f"`{v}`")
        s = " / ".join(parts)
        if "x" in names and len(names) > 1:
            s += "。`x` の行はこの軸で分岐しない (他の軸の組み合わせで決まる)"
        return s

    def unused(what: str, n_def: int) -> str:
        base = "この分類では使わない → **`x`**"
        if n_def:
            base += f" (定義テーブルに{what}の定義は {n_def} 行あるが、点数表のどの行もこの桁で分岐しない)"
        return base

    # condition classification (digit 7)
    if info["byotai"]:
        parts = []
        for r in D.byotai.get(key, []):
            cond = fmt_range("年齢", r[4], r[5])
            if r[7]:
                cond += f"かつ{r[7]}"
            parts.append(f"`{r[2]}` = {cond}")
        byotai = " / ".join(parts)
    else:
        byotai = unused("病態等分類", len(D.byotai.get(key, [])))
    # age / birth weight etc. (digit 8)
    if info["age"]:
        parts = []
        for r in D.age.get(key, []):
            name = r[3]
            for i in range(5):
                lo, hi, v = r[4 + 3 * i], r[5 + 3 * i], r[6 + 3 * i]
                if v == "":
                    continue
                parts.append(f"`{v}` = {fmt_range(name, lo, hi)}")
        age = " / ".join(parts)
    else:
        age = unused("年齢等", len(D.age.get(key, [])))
    # surgery (digits 9-10)
    if info["surg"]:
        surg = vals(surg_names) + "。K コードとの対応は下の「手術」の表"
    else:
        surg = unused("手術", len([r for r in D.surg.get(key, []) if not r[7].startswith("KKK")]))
    p1 = vals(p1_names) + "。中身は下の「手術・処置等１」の表" if info["p1"] else unused("処置等１", len(D.p1.get(key, [])))
    p2 = vals(p2_names) + "。中身は下の「手術・処置等２」の表" if info["p2"] else unused("処置等２", len(D.p2.get(key, [])))
    fuku = vals(fuku_names) + "。対象の ICD は下の「定義副傷病」の表" if info["fuku"] else unused("定義副傷病", len(D.fuku.get(key, [])))
    n_sev = sum(len(t.get(key, [])) for t in (D.sev1, D.sev2, D.sev3, D.sev4))
    sev = vals(sev_names) + "。条件は下の「重症度等」の表" if info["sev"] else unused("重症度等", n_sev)

    rows_out = [
        ["1〜2", "MDC", f"`{mdc}` ({D.mdc.get(mdc, '')})"],
        ["3〜6", "分類コード", f"`{bun}`" + ("（末尾の x は分類コード自体の一部）" if "x" in bun else "")],
        ["7", "病態等分類", byotai],
        ["8", "年齢・出生時体重等", age],
        ["9〜10", "手術", surg],
        ["11", "手術・処置等１", p1],
        ["12", "手術・処置等２", p2],
        ["13", "定義副傷病", fuku],
        ["14", "重症度等", sev],
    ]
    return table(["桁", "軸", "この分類での値"], rows_out), info


def example_line(rows: list[list[str]]) -> str:
    """Pick one row with points and demonstrate how to read the digits in a single line."""
    cand = [r for r in rows if r[12] not in ("", "-")] or rows
    # prefer rows with more branching (fewer x)
    r = min(cand, key=lambda r: (r[2].count("x"), int(r[1]) if r[1].isdigit() else 0))
    c = r[2]
    parts = [f"MDC `{c[:2]}` + 分類 `{c[2:6]}`"]
    if c[6] != "x":
        parts.append(f"病態等分類 `{c[6]}`")
    if c[7] != "x":
        parts.append(f"年齢等 `{c[7]}`")
    if c[8:10] != "xx":
        parts.append(f"手術 `{c[8:10]}` ({r[4]})")
    if c[10] != "x":
        parts.append(f"処置等１ `{c[10]}` ({r[5]})")
    if c[11] != "x":
        parts.append(f"処置等２ `{c[11]}` ({r[6]})")
    if c[12] != "x":
        parts.append(f"定義副傷病 `{c[12]}` ({r[7]})")
    if c[13] != "x":
        parts.append(f"重症度等 `{c[13]}` ({r[8]})")
    pts = (
        f"入院期間Ⅰは {r[9]} 日目まで {r[12]} 点/日、Ⅱは {r[10]} 日目まで {r[13]} 点/日、"
        f"Ⅲは {r[11]} 日目まで {r[14]} 点/日"
        if r[12] not in ("", "-")
        else "この行は包括対象外 (医科点数表で算定)"
    )
    return (
        f"**例**: {' + '.join(parts)} → **`{c}`** (点数表の番号 {r[1]})。{pts}。"
        "使わない桁は `x` のまま書く。"
    )


def icd_table(rows: list[list[str]]) -> list[str]:
    return table(["ICD-10", "傷病名"], [[f"`{r[3]}`", r[2] + valid_note(r, 4)] for r in rows])


def icd_note(D: Data) -> str:
    return (
        "ICD の末尾の `$` は「その下位コードすべて」(例 `C71$` = C710〜C719)。"
        "`M!!!!` は「ICD テーブルに載っていない M で始まるコードすべて」(071030 にだけ出る)。"
        "傷病名は「医療資源を最も投入した傷病」で主治医が ICD-10 (2013 年版) から選ぶ "
        f"(`{D.ryui_ref}` 第2の1(2))。"
    )


def surgery_rows(D: Data, key: str) -> list[list[str]]:
    """Rows of the surgery definition table: [digits 9-10, definition flag, surgery (K code + name)]. Original row order."""
    out = []
    for r in D.surg.get(key, []):
        parts = []
        for i in range(5):
            name, k = r[6 + 2 * i], r[7 + 2 * i]
            if k or name:
                parts.append(f"`{k}` {name}".strip())
        out.append([f"`{r[5]}`", r[3], " ＋ ".join(parts) + valid_note(r, 16)])
    return out


def surgery_table(D: Data, key: str, surg_names: dict[str, list[str]]) -> list[str]:
    rows = surgery_rows(D, key)
    label = {v: " / ".join(n) for v, n in surg_names.items()}
    out = [
        "「9〜10 桁目」が 14 桁コードに入る値。同じ値の手術はどれを行っても同じ分岐。"
        "`97`（その他の手術あり）は **この表で 97 に対応付けられた手術に加えて、この表に無い手術 "
        "(ダミーコード `KKK1`) を行った場合も含む**。`99` は手術なし (`KKK0`)。"
        "「定義フラグ」は定義テーブル (別添2) の手術欄の番号で、ツリー図と突き合わせるときに使う。"
        "「＋」で並ぶものは同一入院中に全部を実施した場合。",
        "",
    ]
    out += table(
        ["9〜10 桁目", "分岐 (点数表の表記)", "定義フラグ", "手術 (K コード)"],
        [[r[0], label.get(r[0].strip("`"), ""), r[1], r[2]] for r in rows],
    )
    return out


def p1_table(D: Data, key: str, names: dict[str, list[str]]) -> list[str]:
    rows = []
    for r in D.p1.get(key, []):
        item = f"`{r[6]}` {r[5]}".strip()
        if r[8] or r[7]:
            item += f" ＋ `{r[8]}` {r[7]}".rstrip()
        rows.append([f"`{r[2]}`", " / ".join(names.get(r[2], [])), r[3], item + valid_note(r, 9), r[4]])
    return [
        "「11 桁目」が 14 桁コードに入る値。「手術との組み合わせ条件」に K コードがある行は、"
        "その手術を行った場合にだけこの処置等１が分岐になる。",
        "",
    ] + table(["11 桁目", "分岐 (点数表の表記)", "定義フラグ", "処置等１ (コード・名称)", "手術との組み合わせ条件"], rows)


def p2_table(D: Data, key: str, names: dict[str, list[str]]) -> list[str]:
    rows = []
    for r in D.p2.get(key, []):
        item = f"`{r[5]}` {r[4]}".strip()
        if r[7] or r[6]:
            item += f" ＋ `{r[7]}` {r[6]}".rstrip()
        rows.append([f"`{r[2]}`", " / ".join(names.get(r[2], [])), r[3], item + valid_note(r, 8)])
    return [
        "「12 桁目」が 14 桁コードに入る値。4 桁の数字コード (`0046` 等) は薬剤・療法のダミーコード "
        f"(一覧は `{aux(N_DUMMY)}`)。",
        "",
    ] + table(["12 桁目", "分岐 (点数表の表記)", "定義フラグ", "処置等２ (コード・名称)"], rows)


FUKU_FLAG = {"1": "手術あり・なし共通", "2": "手術なしの場合", "3": "手術ありの場合"}


def fuku_table(D: Data, key: str) -> list[str]:
    rows = [[f"`{r[5]}`", r[4] + valid_note(r, 6), FUKU_FLAG.get(r[3], r[3])] for r in D.fuku.get(key, [])]
    return [
        "次の ICD の傷病が入院時併存症または入院後発症傷病としてある (疑い病名は除く) とき、"
        "13 桁目が `1`（あり）。「適用」は手術あり・なしのどちらで効くか (定義副傷病フラグ)。",
        "",
    ] + table(["ICD-10", "傷病名", "適用"], rows)


def severity_table(D: Data, key: str) -> list[str]:
    out: list[str] = []
    rows = []
    for r in D.sev1.get(key, []):
        name = r[3]
        for i in range(2):
            lo, hi, v = r[4 + 3 * i], r[5 + 3 * i], r[6 + 3 * i]
            if v != "":
                rows.append([f"`{v}`", f"{name}: {fmt_range(name, lo, hi)}"])
    for r in D.sev2.get(key, []):
        rows.append([f"`{r[5]}`", f"{r[3]}: {r[4]}"])
        rows.append([f"`{r[7]}`", f"{r[3]}: {r[6]}"])
    for r in D.sev3.get(key, []):
        name = r[3]
        for i in range(3):
            lo, hi, v = r[4 + 3 * i], r[5 + 3 * i], r[6 + 3 * i]
            if v != "":
                rows.append([f"`{v}`", f"{name}: {fmt_range(name, lo, hi)}"])
    for r in D.sev4.get(key, []):
        rows.append([f"`{r[2]}`", f"{r[5]}: {r[7]}"])
    if rows:
        out.append(
            "「14 桁目」が 14 桁コードに入る値。複数の条件に当たるときは**最大の値**を採る "
            "(電子点数表の前提条件シート)。"
        )
        out.append("")
        out += table(["14 桁目", "条件"], rows)
    return out


def points_table(D: Data, rows: list[list[str]]) -> list[str]:
    head = [
        "番号", "診断群分類番号", "傷病名", "手術", "処置等１", "処置等２", "定義副傷病", "重症度等",
        "入院日Ⅰ", "入院日Ⅱ", "入院日Ⅲ", "点数Ⅰ", "点数Ⅱ", "点数Ⅲ", "備考",
    ]
    body = []
    for r in rows:
        inc = D.hokatsu.get(r[2], "")
        if r[12] == "" and inc == "1":
            sys.exit(f"{r[2]}: points empty but bundling flag in the conversion table is 1")
        if r[12] != "" and inc == "0":
            sys.exit(f"{r[2]}: points present but bundling flag in the conversion table is 0")
        cells = [r[1], f"`{r[2]}`", r[3], r[4], r[5], r[6], r[7], r[8]]
        if r[12] == "":
            cells += ["—"] * 6 + ["**包括対象外** (医科点数表で算定)"]
        else:
            cells += [r[9], r[10], r[11], r[12], r[13], r[14], valid_note(r, 15).strip()]
        body.append(cells)
    return table(head, body)


def ccpm_table(D: Data, key: str) -> list[str]:
    rows = D.ccpm.get(key, [])
    if not rows:
        return []
    classes = sorted({r[3] for r in rows})
    return [
        "## CCP マトリックス (支払分類)",
        "",
        f"この分類は CCP マトリックス (Comorbidity Complication Procedure Matrix) の対象で、"
        f"上の {len(rows)} 本の 14 桁コードは {len(classes)} 個の**支払分類**にまとめられる。"
        "同じ支払分類のコードは入院日・点数が同じ。レセプトに書くのは 14 桁の診断群分類番号のほう。",
        "",
    ] + table(["診断群分類番号", "CCPM 支払分類"], [[f"`{r[2]}`", f"`{r[3]}`"] for r in rows])


def caveats(D: Data, key: str) -> list[str]:
    return [
        "## 注意 (読んだら必ず踏まえること)",
        "",
        f"- 点数は **1 日あたり**で、**医療機関別係数を掛ける前**の値。当院の係数と掛け方は "
        f"`{aux(N_KEISU)}`。月ごとの請求は 1 日あたり点数 × 係数 × その月の入院日数 "
        "(+ 包括範囲外の出来高分)",
        "- **入院日Ⅲを超えた日以降は医科点数表 (出来高) で算定**する。期間の数え方 "
        "(Ⅰ = 入院日Ⅰまで、Ⅱ = Ⅰの翌日〜入院日Ⅱ、Ⅲ = Ⅱの翌日〜入院日Ⅲ) と 7 日以内の再入院の扱いは "
        f"`{aux(N_YOMIKATA)}`。条文は算定告示 `{aux(N_SANTEI)}` (項 15・16)",
        "- 点数の無い行 (**包括対象外**) は、その 14 桁に該当しても診断群分類点数表では算定しない = 医科点数表で算定",
        f"- ICU・HCU・NICU 等の**特定入院料を届け出た病棟の患者には所定点数に加算する点数**がある (算定告示 項 4〜6。"
        f"当院は項 4)。表は `{aux(N_KASAN)}`",
        "- **コーディングの最終判断は主治医と医事課**。ここでは候補の 14 桁コードと根拠 "
        "(どの表のどの行に当たったか) を示すまでにする。「医療資源を最も投入した傷病」の決定は主治医",
        f"- 収録した版: {D.version_label()}。{D.retrieved} に {D.version['url']} から取得。"
        "**改定・訂正で差し替わる** (特設ページ「第６ 電子点数表等」に新しい正式版が積み上がる)",
        f"- 原本の xlsx (全シート・全分類): {D.static_url} — sandbox に `send_to_workspace` して "
        "pandas で引ける。ここに無い列 (変換テーブルの条件の組み合わせ、有効期間、更新日) はそちら",
        qa_line(D, key),
    ]


def qa_line(D: Data, key: str) -> str:
    """Q&A pointer at the end of a classification node: paths of questions naming this classification
    if any, otherwise the section-level pointer."""
    if not QA_DPC_DIR.exists():
        return f"- 疑義解釈 (Q&A): DPC 別添のディレクトリ `{ddir(QA_DPC_DOC)}` (build 時に無かった。あれば制度一般の問がそこにある)"
    when = f" ({D.qa_note})" if D.qa_note else ""
    if D.qa_by_key.get(key):
        return (
            f"- **疑義解釈 (Q&A) にこの分類を名指しする問がある**{when}: "
            + " / ".join(f"`{leaf(p)}`" for p in D.qa_by_key[key])
            + f"。制度一般 (対象患者・再入院・請求手順) の問は `{ddir(QA_DPC_DOC)}` の各見出しノード"
        )
    return (
        f"- 疑義解釈 (Q&A){when}: この分類を名指しする問は無い。DPC 制度一般 (対象患者・区分の適用の考え方・"
        f"再入院・退院時処方・明細書) の問は `{ddir(QA_DPC_DOC)}` (見出しごとのノード) — **無い = 疑義が無い、ではない**"
    )


def bunrui_node(D: Data, key: str, dir_doc: str) -> tuple[str, dict[str, str]]:
    """One classification. Returns (slug, {relative path: body})."""
    mdc, bun = key[:2], key[2:]
    name = D.bunrui[key][2]
    slug = fs_safe(f"{key}_{name}")
    rows = D.points.get(key, [])
    if not rows:
        sys.exit(f"{key} {name}: no rows in the fee table")
    icd = D.icd.get(key, [])
    section = f"{mdc} {D.mdc.get(mdc, '')}"
    title = f"{key} {name}"
    summary, info = axis_summary(D, key, rows)
    surg_names = names_by_value(rows, slice(8, 10), 4)
    p1_names = names_by_value(rows, slice(10, 11), 5)
    p2_names = names_by_value(rows, slice(11, 12), 6)
    n_surg = len(D.surg.get(key, []))
    split_icd = len(icd) > ICD_SPLIT
    split_surg = info["surg"] and n_surg > SURGERY_SPLIT
    is_dir = split_icd or split_surg
    self_doc = f"{dir_doc}/{slug}"
    n_inc = sum(1 for r in rows if r[12] != "")

    body: list[str] = [
        f"{TENSUHYO} MDC{mdc} {D.mdc.get(mdc, '')} 「{key} {name}」。適用は {APPLIES_FROM} から "
        f"({D.version_label()})。この分類の 14 桁コードは {len(rows)} 本 (うち包括対象 {n_inc} 本)。",
        "",
        "> **この分類の読み方**: (1) 「対象傷病」の ICD に当たれば 6 桁が `" + key + "` に決まる → "
        "(2) 「14 桁コードの組み立て」の表で軸ごとに値を拾い、下の各定義表 (手術 / 処置等 / 副傷病 / 重症度) で"
        "当たる行を確かめる → (3) 「点数表」でその 14 桁の行を引き、入院日Ⅰ/Ⅱ/Ⅲ と 1 日あたり点数を読む。"
        f"用語と共通ルール (期間の数え方、包括範囲、再入院) は `{aux(N_YOMIKATA)}`。",
        "",
        "## 14 桁コードの組み立て (この分類)",
        "",
        "診断群分類番号は 14 桁。この分類で各桁に入る値は次のとおり (`x` = その桁は使わない、そのまま `x` と書く)。",
        "",
    ] + summary + [example_line(rows), ""]

    # target diseases
    body += [f"## 対象傷病 (ICD-10) — {len(icd)} 件" + (" — **子ノードにある**" if split_icd else ""), ""]
    if split_icd:
        body += [
            f"この分類は対象の ICD が {len(icd)} 件と多いため、**ICD の一覧は子ノードに分けてある。"
            f"このノードには載っていない。** `{leaf(f'{self_doc}/{ICD_CHILD}')}` を read すること (消えたわけではない)。",
            "",
        ]
    else:
        body += [icd_note(D), ""] + icd_table(icd)

    # branch definitions
    body += ["## 分岐の定義", ""]
    if info["surg"]:
        body += [f"### 手術 (9〜10 桁目) — K コード {n_surg} 行" + (" — **子ノードにある**" if split_surg else ""), ""]
        if split_surg:
            label = " / ".join(f"`{v}` = {'・'.join(n)}" for v, n in sorted(surg_names.items()) if v != "xx")
            body += [
                f"分岐は {label}。**K コードごとの対応表 ({n_surg} 行) は子ノードに分けてある。"
                f"このノードには載っていない。** `{leaf(f'{self_doc}/{SURGERY_CHILD}')}` を read すること。",
                "",
            ]
        else:
            body += surgery_table(D, key, surg_names)
    if info["p1"]:
        body += ["### 手術・処置等１ (11 桁目)", ""] + p1_table(D, key, p1_names)
    if info["p2"]:
        body += ["### 手術・処置等２ (12 桁目)", ""] + p2_table(D, key, p2_names)
    if info["fuku"]:
        body += ["### 定義副傷病 (13 桁目)", ""] + fuku_table(D, key)
    if info["sev"]:
        body += ["### 重症度等 (14 桁目)", ""] + severity_table(D, key)
    if not any(info[k] for k in ("surg", "p1", "p2", "fuku", "sev", "byotai", "age")):
        body += ["この分類は分岐が無く、14 桁は 1 本だけ (`" + rows[0][2] + "`)。", ""]

    # fee table
    body += [
        f"## 診断群分類点数表 — この分類の全 {len(rows)} コード",
        "",
        "入院日Ⅰ/Ⅱ/Ⅲ は「その日数まで」、点数Ⅰ/Ⅱ/Ⅲ は各期間の **1 日あたり**点数 (係数を掛ける前)。"
        "入院日Ⅰと Ⅱ が同じ行は期間Ⅱが無く、点数Ⅱが `-`。"
        "行名 (手術・処置等・副傷病・重症度等の列) に「なし、１あり」のように複数の分岐値が並ぶ行は、"
        "それらの分岐がすべてこの 1 行にまとまっている。複数の行に当たりうるときは、原則として**後ろの行 (ツリー図で下の分岐) を優先**"
        f"する (留意事項通知 第2の1(5)。`{aux(N_YOMIKATA)}`)。",
        "",
    ] + points_table(D, rows)
    body += ccpm_table(D, key)
    body += caveats(D, key)

    files: dict[str, str] = {}
    fm = front_matter(D, title, mdc, key, section, child_fields=is_dir)
    if not is_dir:
        files[f"{slug}.md"] = "\n".join(fm + body) + "\n"
        return slug, files
    files[f"{slug}/_index.md"] = "\n".join(fm + body) + "\n"
    # `{what}` is substituted with str.replace below (str.format would clobber `{{DOC_BASE}}` in the body)
    lead = (
        f"{TENSUHYO} MDC{mdc} {D.mdc.get(mdc, '')} 「{key} {name}」の**{{what}}**。"
        f"大きいので分類本体から分けてある。\n\n**この分類の残り (14 桁の組み立て・分岐の定義・点数表・注意) は"
        f"親ノードにある** — `{ddir(self_doc)}` を read すること。ここには{{what}}しか入っていない。\n"
    )
    if split_icd:
        files[f"{slug}/{ICD_CHILD}.md"] = "\n".join(
            front_matter(D, f"{title} — 対象傷病 (ICD-10)", mdc, key, section, child_fields=False)
            + [lead.replace("{what}", "対象傷病 (ICD-10) の一覧"), f"## 対象傷病 (ICD-10) — {len(icd)} 件", "", icd_note(D), ""]
            + icd_table(icd)
        ) + "\n"
    if split_surg:
        files[f"{slug}/{SURGERY_CHILD}.md"] = "\n".join(
            front_matter(D, f"{title} — 手術の定義 (K コード → 9〜10 桁目)", mdc, key, section, child_fields=False)
            + [lead.replace("{what}", "手術の定義 (K コードと 9〜10 桁目の対応)"), f"## 手術 (9〜10 桁目) — K コード {n_surg} 行", ""]
            + surgery_table(D, key, surg_names)
        ) + "\n"
    return slug, files


# ---------------------------------------------------------------------------
# MDC directories / section index
# ---------------------------------------------------------------------------


def mdc_index(D: Data, mdc: str, entries: list[tuple[str, str, int, int, bool]]) -> str:
    name = D.mdc[mdc]
    keys = [e[0] for e in entries]
    fm = front_matter(D, f"MDC{mdc} {name}", mdc, f"{keys[0]}〜{keys[-1]}", f"{mdc} {name}", child_fields=True)
    mdc_dir_doc = ddir(f"{SEC_DOC}/{fs_safe(f'{mdc}_{name}')}")
    rows = [
        [f"`{k}`", f"`{slug}`", str(n), str(inc), "子ノードあり (ICD または手術の一覧)" if split else ""]
        for k, slug, n, inc, split in entries
    ]
    body = [
        f"{TENSUHYO} の MDC{mdc} {name}。分類 {len(entries)} 本、14 桁コード {sum(e[2] for e in entries)} 本。"
        "**1 ノード = 1 分類 (6 桁)**。分類ノードを read すると、対象 ICD・分岐の定義・全 14 桁の点数が読める。",
        "",
        f"傷病名から分類を探すなら `grep_doc(query=\"<傷病名や ICD>\", path=\"{mdc_dir_doc}\")`。"
        f"用語と共通ルールは `{aux(N_YOMIKATA)}`。",
        "",
    ] + table(["分類", "ノード", "14 桁コード", "うち包括対象", "備考"], rows)
    return "\n".join(fm + body)


def section_index(D: Data, mdc_entries: dict[str, list], aux_titles: dict[str, str], stats: dict) -> str:
    fm = [
        "---",
        f"title: {yaml_str('診断群分類 (DPC) 電子点数表 (令和8年度改定・正式版 ' + D.version['updated'] + ' 更新)')}",
        f"tensuhyo: {yaml_str(TENSUHYO)}",
        f"publisher: {PUBLISHER}",
        f"applies_from: {yaml_str(APPLIES_FROM)}",
        f"source: {SOURCE}",
        f"retrieved_at: {yaml_str(D.retrieved)}",
    ] + CHILD_FIELDS + ["---", ""]
    v = D.version
    mdc_rows = []
    for mdc, entries in mdc_entries.items():
        mdc_rows.append(
            [f"`{fs_safe(f'{mdc}_{D.mdc[mdc]}')}`", D.mdc[mdc], str(len(entries)), str(sum(e[2] for e in entries))]
        )
    aux_rows = [[f"`{n}`", t] for n, t in aux_titles.items()]
    body = [
        f"厚生労働省が公開する **診断群分類 (DPC) 電子点数表** (令和8年度診療報酬改定、正式版 {v['updated']} 更新) を"
        "分類ごとに読める形にしたもの。DPC/PDPS (急性期入院医療の 1 日あたり包括払い) で、患者の 14 桁の"
        "**診断群分類番号**を決め、入院期間Ⅰ/Ⅱ/Ⅲ ごとの 1 日あたり点数を引くための一次資料。"
        f"適用は {APPLIES_FROM} から。",
        "",
        "## 使い方 (3 手)",
        "",
        f"1. **共通ルールを 1 度読む** — `{aux(N_YOMIKATA)}` (14 桁の構成、期間Ⅰ/Ⅱ/Ⅲ、包括範囲、出来高になる場合)",
        "2. **分類ノードを read する** — 傷病名か ICD で `grep_doc` するか、下の MDC ディレクトリを list して "
        "`<6 桁>_<名称>` を開く。1 ノードに 対象 ICD・分岐の定義 (K コード → 桁の値)・全 14 桁の点数表・注意が入っている "
        "(ICD や K コードが多い分類は `対象傷病` / `手術の定義` の子ノード)",
        f"3. **係数を掛ける** — `{aux(N_KEISU)}` (当院 = {HOSPITAL} の基礎係数・機能評価係数Ⅱ・救急補正係数と、"
        f"機能評価係数Ⅰの一覧)。点数表の点数は係数を掛ける前の 1 日あたり。ICU 等の特定入院料の患者は `{aux(N_KASAN)}` の加算を足す",
        "",
        "**コーディングの最終判断は主治医と医事課**。ここから答えるのは「候補の 14 桁とその根拠 (どの表のどの行)」まで。",
        "",
        "## 補助ノード (セクション直下、`00_`)",
        "",
    ] + table(["ノード", "中身"], aux_rows) + [
        "## MDC ディレクトリ (分類はこの下)",
        "",
        f"分類 {stats['bunrui']} 本 / 14 桁コード {stats['codes']} 本 (うち包括対象 {stats['inc']} 本、"
        f"包括対象外 {stats['codes'] - stats['inc']} 本) / 対象 ICD {stats['icd']} 行 / 手術定義 {stats['surg']} 行。",
        "",
    ] + table(["ディレクトリ", "MDC", "分類", "14 桁コード"], mdc_rows) + [
        "## 収録した版と判定規則",
        "",
        f"- {v['label']}{v['note']} — **{v['rank']}** ({v['url']}、{D.retrieved} 取得)",
        "- 特設ページ「第６ 電子点数表等」には暫定版 1 本と正式版が更新のたびに**同じリンク名で**積み上がる"
        " (令和8年度は 3/18・4/15・5/19 ×2・7/14・8/12)。**「（正式版）」で始まるリンクのうち、直後の注記"
        "「（令和N年M月D日更新）」が最も新しいもの**を採り、同日なら**ページ上で後ろのもの**を採る "
        "(`scripts/fetch_dpc.py`)。ここより新しい正式版が出ていたら差し替える (改定は 2 年ごと、訂正は随時)",
        f"- 原本の xlsx: {D.static_url} (全 19 シート。sandbox に取り込んで pandas で引ける)。"
        f"係数告示の別表 xlsx: {D.keisu_url}",
        "",
        "## 収録していないもの",
        "",
        f"- **算定告示 (第96号) のうち改め文と経過措置** (縦書きの 1〜9 ページ) と、37 ページ以降の診断群分類点数表 (電子点数表 xlsx と同内容)。"
        f"別表の項 1〜20 は `{N_SANTEI}` / `{N_KASAN}` に収録済み",
        "- **定義告示** (厚生労働大臣が定める傷病名、手術、処置等及び定義副傷病名、第98号)。縦書き PDF で、同じ定義が電子点数表の"
        "シート 4〜10 にあるので取っていない",
        "- **診断群分類定義樹形図 (ツリー図、別添1)** と**定義テーブル (別添2)** の xlsx。電子点数表と同じ定義を"
        "図・表にしたもので、分岐の中身は分類ノードの定義表で読める",
        "- **コーディングテキスト** (DPC/PDPS 傷病名コーディングテキスト)、退院患者調査の実施説明資料 (様式1 等)",
        "- 当院の**機能評価係数Ⅰの合計値**。届出済みの診療料で決まるので、`" + N_KEISU + "` の一覧と"
        "医事課の届出状況を突き合わせる",
        "",
        "## 隣のセクション — 疑義解釈 (Q&A)",
        "",
        qa_section_note(D),
        "",
        "## 出典・ライセンス",
        "",
        f"- 令和8年度診療報酬改定について (厚生労働省) <{SOURCE}> 「第６ 電子点数表等」「８．ＤＰＣ」",
        f"- mhlw.go.jp の利用規約 = 公共データ利用規約 (PDL1.0)。カテゴリ `{ddir('/診療報酬')}` の `_index.md` に同じ",
        "",
    ]
    return "\n".join(fm + body)


# ---------------------------------------------------------------------------
# Auxiliary nodes
# ---------------------------------------------------------------------------


def qa_section_note(D: Data) -> str:
    if not QA_DPC_DIR.exists():
        return (
            f"疑義解釈資料の別添「医科診療報酬点数表関係（ＤＰＣ）」は `{ddir(QA_DPC_DOC)}` に入る予定 (build 時点で無かった)。"
            "無ければ特設ページ「３．疑義解釈・訂正通知」を見る"
        )
    n_keys = len(D.qa_by_key)
    return (
        f"疑義解釈資料の別添「医科診療報酬点数表関係（ＤＰＣ）」の Q&A は **`{ddir(QA_DPC_DOC)}`** にある "
        f"(見出しごとに {D.qa_nodes} ノード{'、' + D.qa_note if D.qa_note else ''})。制度一般 (対象病院・対象患者・"
        "区分の適用の考え方・係数・再入院・退院時処方・明細書) の問はそちらを read する。"
        f"問が診断群分類を名指しするものは分類ノード側の「注意」末尾でその問のノードの path を名乗る ({n_keys} 分類)。"
        "疑義解釈は継続発出中なので、収録時点より後の その N は特設ページ「３．疑義解釈・訂正通知」を見る"
    )


def aux_front(D: Data, title: str, *, child_fields: bool = False) -> list[str]:
    return front_matter(D, title, "-", "-", "補助資料", child_fields=child_fields)


def yomikata_node(D: Data) -> str:
    n_codes = len(D.points_rows)
    n_inc = sum(1 for r in D.points_rows if r[12] != "")
    zentei = {r[1]: r[2] for r in D.zentei if r[1]}
    ryui = D.ryui_ref
    body = [
        f"{TENSUHYO} を読むための共通ルール。個々の分類の点数と分岐は各分類ノード "
        f"(`{DOC_BASE}{SEC_DOC}/<MDC>/<6 桁>_<名称>`) にある。ここは用語と数え方だけ。条文の根拠は "
        f"`{ryui}` (留意事項通知、保医発0318第4号)。",
        "",
        "## DPC/PDPS とは (1 分で)",
        "",
        "- DPC 対象病院 (当院を含む) の一般病棟に入院した患者は、**診断群分類点数表に該当する分類区分があれば**、"
        "出来高ではなく **1 日あたりの包括点数 × 医療機関別係数 × 入院日数** で入院料を請求する (通知 第3の1(1))",
        f"- 条文の正典は 2 本: **算定告示の別表** (`{aux(N_SANTEI)}`、項 1〜20 = 所定点数の定義・包括範囲・特定入院料の加算・"
        f"期間Ⅲ超・調整日・係数) と **留意事項通知** (`{ryui}`)。特定入院料 (ICU 等) の加算点数の表は `{aux(N_KASAN)}`",
        "- 包括点数に含まれるのは 入院基本料・入院基本料等加算 (一部除く)・医学管理等・検査・画像診断・投薬・注射・"
        "処置 (1,000 点未満相当)・病理標本作製料 など。**手術・麻酔・放射線治療・リハビリ・精神科専門療法・"
        "1,000 点以上の処置・退院時処方・HIV 薬などは出来高**で別に算定する (正確な範囲は算定告示 項 2 と通知 第3の1(3)。"
        f"`{aux(N_SANTEI)}` と `{ryui}` 第3 を read)",
        "- 対象外の患者 (入院 24 時間以内の死亡、臓器移植、評価療養・患者申出療養、A106・A304・A308 等の病棟、"
        "厚生労働大臣が別に定める者 (五号告示)、短期滞在手術等基本料1 など) は最初から医科点数表 (通知 第1の2)",
        "",
        "## 14 桁の診断群分類番号",
        "",
    ] + table(
        ["桁", "軸", "中身", "値の決め方"],
        [
            ["1〜2", "MDC (主要診断群)", "18 分類 (01 神経系 〜 18 その他)", "傷病名 (ICD) から"],
            ["3〜6", "分類コード", "MDC の中の傷病 (例 `0010` 脳腫瘍)。**末尾が `x` の分類コードがある** (`01021x` 認知症など。x は分類コードの一部)", "同上。電子点数表シート4「ICD」"],
            ["7", "病態等分類", "肺炎 (040080) だけが使う: 市中肺炎か否かと年齢", "シート3"],
            ["8", "年齢・出生時体重等", "年齢 / 月齢 / 出生時体重 / JCS / Burn Index / GAF / 妊娠週数 / 分娩時出血量 の範囲", "シート5"],
            ["9〜10", "手術", "`99` 手術なし / `97` その他の手術あり (定義に無い手術を含む) / `01`〜 分類ごとに定義された手術群", "シート6 (K コード → 値)"],
            ["11", "手術・処置等１", "`0` なし / `1`〜 分類ごとの処置・検査 (人工呼吸、中心静脈注射、心臓カテーテル 等)", "シート7"],
            ["12", "手術・処置等２", "`0` なし / `1`〜`9`,`A`〜 分類ごとの薬剤・療法 (化学療法、放射線療法、特定の薬剤 等)", "シート8"],
            ["13", "定義副傷病", "`0` なし / `1` あり (分類ごとに定義された併存症・続発症の ICD)", "シート9"],
            ["14", "重症度等", "JCS、片眼/両眼、A-DROP、脳卒中の発症時期、Child-Pugh、急性膵炎の重症度 など", "シート10-1〜10-4"],
        ],
    ) + [
        "- **使わない桁は `x`**。例 `010010xx9900xx` = 脳腫瘍・手術なし・処置等１なし・処置等２なし",
        "- **値は分類ごとに意味が違う** (`03` が脳腫瘍では頭蓋内腫瘍摘出術等、別の分類では別の手術)。"
        "必ずその分類のノードの定義表で読む",
        "- 点数表の行名に「なし、１あり」のように複数の分岐値が並ぶ行は、それらの分岐がまとめて 1 行 (同じ点数) になっている",
        "- 同じ入院で複数の手術・処置等に当たり、同じ分類の複数の 14 桁に該当しうるときは、手術・処置等１・処置等２・定義副傷病の"
        "すべてについて**ツリー図で下にある分岐を優先**する (通知 第2の1(5))。各分類ノードの点数表は原本の行順で、"
        "ツリー図の上から下の順に並んでいるので、**後ろの行ほど優先**と読める (手術なし → その他の手術 → 定義された手術、"
        "処置等なし → あり の順)。「Ｋ○○○に準じて算定」の手術は準用元で判断 (同(6))",
        "- 分類区分の決定は**退院時** (転棟を含む)。入院中に決めた分類と退院時の分類が違えば差額を退院月 (調整日の属する月) で調整 "
        "(算定告示 項 16、通知 第2の1(7)、第3の2(4))",
        "",
        "## 入院期間Ⅰ/Ⅱ/Ⅲ と点数",
        "",
        "点数表の各行に 入院日Ⅰ/Ⅱ/Ⅲ (日数) と 点数Ⅰ/Ⅱ/Ⅲ (1 日あたり) がある (通知 第2の2(1)):",
        "",
    ] + table(
        ["期間", "何日目か", "1 日あたり"],
        [
            ["入院期間Ⅰ", "入院日Ⅰ に掲げる日数**以下** (1 日目〜Ⅰ日目)", "点数Ⅰ (最も高い)"],
            ["入院期間Ⅱ", "入院日Ⅰ を超え 入院日Ⅱ **以下** (Ⅰ+1 日目〜Ⅱ日目)", "点数Ⅱ"],
            ["入院期間Ⅲ", "入院日Ⅱ を超え 入院日Ⅲ **以下** (Ⅱ+1 日目〜Ⅲ日目)", "点数Ⅲ (最も低い)"],
            ["入院日Ⅲ超", "Ⅲ+1 日目以降", "**医科点数表 (出来高)** で算定 (算定告示 項 15、通知 第3の1(5))"],
        ],
    ) + [
        "- 入院日Ⅰ = Ⅱ の行 (Ⅰ・Ⅱとも 1 日など) は期間Ⅱが無く、点数Ⅱが `-`",
        "- 月ごとの請求点数 = 1 日あたり点数 × 医療機関別係数 × その月の入院日数 (月の合計で小数第 1 位を四捨五入)。"
        f"係数は `{aux(N_KEISU)}`",
        "- 外泊期間は入院期間に算入する (点数は基本点数の 15%)。**退院した翌日から 7 日以内の再入院**で、"
        "前回の「医療資源を最も投入した傷病名」と今回の「入院の契機となった傷病名」の上 2 桁 (MDC) が同じ、"
        "または上 6 桁が同じなら**一連の入院**とみなし、起算日は初回の入院日 (通知 第3の1(7)。化学療法の予定再入院は除外)",
        "- 入院日Ⅲを超えても、化学療法あり等の分類で入院日Ⅲまでに化学療法が実施されなかった場合は、"
        "Ⅲ超の期間も抗悪性腫瘍剤の薬剤料は算定できない (通知 第3の1(5)①)",
        "",
        "## 包括対象外の 14 桁 (点数の無い行)",
        "",
        f"点数表の {n_codes} 本のうち **{n_codes - n_inc} 本は点数が空** (変換テーブルの包括フラグ 0)。"
        "ツリー図で点線の「医科点数表算定コード」で、その 14 桁に該当する患者は診断群分類点数表では算定せず"
        "**医科点数表で算定**する。レセプトの摘要欄に医科点数表で算定する理由と 14 桁を書く (通知 第2の1(1))。"
        "各分類ノードの点数表では備考に「包括対象外」と書いてある。",
        "",
        "## 定義副傷病のフラグ / 年齢等・重症度等の条件区分",
        "",
        "- 定義副傷病 (13 桁目) の定義には「適用」がある: `1` 手術あり・なし共通 / `2` 手術なしの場合だけ / `3` 手術ありの場合だけ。"
        "入院時併存症と入院後発症傷病の両方を含み、疑い病名は含まない (通知 第2の2(2))",
        "- 年齢・出生時体重等 (8 桁目) の条件区分: 1 年齢 / 2 JCS / 3 出生時体重 / 4 Burn Index / 5 GAF / 6 月齢 / 7 妊娠週数 / 8 分娩時出血量。"
        "年齢は**入院時**、JCS は DPC 病棟等への入院時点 (入院後に発症した傷病が主傷病になる場合はその発症時) (通知 第2の3)",
        "- 重症度等 (14 桁目) の条件区分: 1 年齢 / 2 JCS / 4 片眼・両眼 / 5 片側・両側 / 6 初回・再手術 / 7 一側・両側 / 8 リハビリ / "
        "9・10 急性膵炎の重症度 / 12 A-DROP スコア / 13 他の病院・診療所の病棟からの転院 / 14 脳卒中の発症時期 / 15 Child-Pugh 分類。"
        "複数の条件に当たれば**最大の値**",
        "- 「化学療法」は抗腫瘍効果のある薬剤の入院中の使用 (抗生剤のみ・G-CSF・制吐剤は含まない)、「放射線療法」は第12部の放射線治療、"
        "「全身麻酔」は L008 閉鎖循環式全身麻酔 (通知 第2の3(5)〜(9))",
        "- **分類固有の用語の定義は通知 第2の3 (10)〜(20)** にある: メトトレキサート大量療法 (10)、急性膵炎の「重症」(12)、"
        "インターフェロンβの「一定期間以上」= 7 日以上 (13)、脳梗塞の発症時期の起点 (14)、市中肺炎の例と **A-DROP スコア** (15)、"
        "早産の妊娠週数 (16)、転院 (18)、分娩時出血量 (19)、**Child-Pugh 分類**の評点表 (20)。分類ノードには載せていないので "
        f"`{ryui}` の 第２ を read する",
        "",
        "## 出来高算定になる手術等・ダミーコード・CCP マトリックス",
        "",
        f"- **出来高算定手術等コード** (臓器移植術 と 厚生労働大臣が定める手術等): `{aux(N_DEKIDAKA)}`。"
        "これらを行った患者は診断群分類点数表の対象にならない (通知 第1の2(3)、五号告示)",
        f"- **ダミーコード** (`KKK0` 手術なし、`KKK1` 定義に無い手術、`0005` 化学療法 のように K コード・診療行為コードで表せない分岐を表す 4 桁): "
        f"`{aux(N_DUMMY)}`",
        "- **CCP マトリックス**: 010060 脳梗塞 と 040080 肺炎等 は 14 桁が多数の組み合わせになるため、複数の 14 桁を"
        "「支払分類」(`010060xxCCPM01` 等) にまとめて同じ点数にしている (シート14)。レセプトに書くのは 14 桁のほう",
        "",
        "## 電子点数表 (xlsx) のシート構成",
        "",
        f"原本 {D.static_url} の 19 シート。分類ノードはこのうち 2・4〜11・14 を分類ごとに写している。",
        "",
    ] + table(
        ["シート", "中身", "分類ノードでの扱い"],
        [
            ["ＤＰＣ電子点数表の前提条件", "各シートの約束事 (変更区分の意味、$ ワイルドカード等)", "この節に要約"],
            ["ダミーコード一覧", "手術・処置等のダミーコード", f"`{N_DUMMY}`"],
            ["1) MDC名称 / 2) 分類名称", "MDC 18 / 分類 504", "ディレクトリ / ノード"],
            ["3) 病態等分類", "7 桁目", "分類ノード「14 桁の組み立て」"],
            ["4) ICD", "分類 → ICD-10 (5,898 行)", "分類ノード「対象傷病」"],
            ["5) 年齢、出生時体重等", "8 桁目の条件", "分類ノード「14 桁の組み立て」"],
            ["6) 手術", "K コード → 9〜10 桁目 (8,621 行)", "分類ノード「手術」"],
            ["7) 手術・処置等1 / 8) 手術・処置等2", "11 / 12 桁目", "分類ノード「手術・処置等１/２」"],
            ["9) 定義副傷病名", "13 桁目の ICD", "分類ノード「定義副傷病」"],
            ["10-1〜10-4) 重症度等", "14 桁目の条件", "分類ノード「重症度等」"],
            ["11) 診断群分類点数表", "14 桁ごとの入院日・点数 (3,267 行)", "分類ノード「診断群分類点数表」"],
            ["12) 変換テーブル", "各軸の値の組み合わせ → 14 桁、包括フラグ (9,248 行)", "包括対象外の判定に使用。組み合わせ自体は xlsx を引く"],
            ["13) 出来高算定手術等コード", "包括対象外になる手術", f"`{N_DEKIDAKA}`"],
            ["14) CCPM対応", "CCP マトリックスの支払分類", "分類ノード末尾"],
        ],
    ) + [
        "変更区分の列 (全シート): " + zentei.get("前提条件９", "").replace("\n", " ").replace("　", "") + "。"
        + changed_summary(D),
        "",
        "## 注意",
        "",
        f"- 収録した版: {D.version_label()} ({D.retrieved} 取得)。改定・訂正で差し替わる",
        "- 通知の条文は要約。判断の根拠に使うときは `" + ryui + "` の本文を read する",
        f"- 制度の Q&A (疑義解釈の DPC 別添) は `{ddir(QA_DPC_DOC)}`。分類を名指しする問は分類ノードの末尾が path を名乗る",
        "- コーディングの最終判断は主治医と医事課。ここから出せるのは候補と根拠まで",
    ]
    return "\n".join(aux_front(D, "DPC の読み方 (14 桁・入院期間・包括範囲の共通ルール)") + body)


def changed_summary(D: Data) -> str:
    """Summary of rows whose change category is not 0 in this version (used in the how-to-read node and build output)."""
    if not D.changed_sheets:
        return "**この版は全シートの全行が 0** (前バージョンと同じ)。"
    parts = [f"{name} {n} 行" for name, n in D.changed_sheets.items()]
    keys = sorted(k for k in D.changed if k != "-")
    return (
        f"**この版で 0 以外の行**: {'、'.join(parts)}"
        + (f" (分類 {', '.join(keys)})" if keys else "")
        + "。該当行には各ノードの表で太字の注記 (変更区分・適用開始日) を付けてある。"
    )


def dekidaka_node(D: Data) -> str:
    kinds = {"00": "移植術", "01": "厚生労働大臣指定"}
    rows = [[kinds.get(r[0], r[0]), f"`{r[1]}`" if r[1] else "(コードなし・名称で判定)", r[2] + valid_note(r, 3)] for r in D.dekidaka]
    body = [
        f"{TENSUHYO} シート13「出来高算定手術等コード」({len(rows)} 行)。**この手術等を行った患者は"
        "診断群分類点数表の対象にならず、医科点数表 (出来高) で算定する** (区分 00 = 臓器の移植術: 留意事項通知 第1の2(3)、"
        "区分 01 = 厚生労働大臣が別に定める者 (平成24年厚生労働省告示第140号、五号告示) に掲げる手術等)。"
        f"原本の表題どおり「包括対象外になる手術コード等の一覧」。条文は `{D.ryui_ref}`。",
        "",
        "**手術コードが空の行は薬剤** (厚生労働大臣が定める高額薬剤。名称で判定する)。**太字の注記がある行は"
        "適用開始が 2026-06-01 でない** (年度途中で追加された薬剤 = その日から対象外)。",
        "",
    ] + table(["区分", "手術コード", "名称"], rows) + [
        f"- 収録した版: {D.version_label()}。原本 xlsx: {D.static_url}",
        "- 最終判断は医事課。ここに無い手術でも五号告示の他の要件 (高額薬剤等) で対象外になることがある",
    ]
    return "\n".join(aux_front(D, "出来高算定手術等コード (診断群分類点数表の対象外になる手術)") + body)


def dummy_node(D: Data) -> str:
    rows = [[f"`{r[2]}`", r[3], r[4]] for r in D.dummy]
    body = [
        f"{TENSUHYO} シート「ダミーコード一覧」({len(rows)} 件)。手術 (9〜10 桁目)・手術・処置等１ (11 桁目)・"
        "手術・処置等２ (12 桁目) の定義に出てくる、K コードや診療行為コードで表せない分岐を表す 4 桁のコード。"
        "分類ノードの定義表で `0005` のような 4 桁コードが出たらここで引く。",
        "",
        "- `KKK0` = 手術なし、`KKK1` = 定義テーブルにない手術 (= その他の手術あり `97`)、`KK03` = 補助循環加算 など",
        "- 薬剤名のダミーコード (`0056` ベバシズマブ 等) は、その薬剤 (成分) を入院中に使用した場合",
        "",
    ] + table(["ダミーコード", "項目名", "前回改定からの変更点"], rows) + [
        f"- 収録した版: {D.version_label()}。原本 xlsx: {D.static_url}",
    ]
    return "\n".join(aux_front(D, "ダミーコード一覧 (手術・処置等の分岐に使う 4 桁コード)") + body)


def keisu_node(D: Data) -> tuple[str, dict]:
    """Hospital coefficients + list of functional evaluation coefficient I. Returns (body, matched row)."""
    found = None
    group = None
    groups: dict[str, list[list[str]]] = {}
    for name, rows in D.keisu.items():
        if not name.startswith("別表第") or "機能評価係数Ⅰ" in name:
            continue
        data = [r for r in rows if re.fullmatch(r"\d{5}", r[0]) and r[2]]
        groups[name] = data
        for r in data:
            if r[2] == HOSPITAL:
                found, group = r, name
    if found is None:
        sys.exit(f"no row for {HOSPITAL} in the coefficient annex tables")
    k1_rows = []
    cur = ""
    for r in D.keisu.get("別表第四～六（機能評価係数Ⅰ）", []):
        if r[0].startswith("別表第") and not r[1]:
            cur = r[0]
            continue
        if r[1] and re.fullmatch(r"-?[0-9.]+", r[1]):
            k1_rows.append([cur, r[0], r[1]])
    base_by_group = {g: sorted({r[3] for r in rows}) for g, rows in groups.items()}
    body = [
        f"DPC の**医療機関別係数**のうち、{HOSPITAL}の値と、機能評価係数Ⅰの一覧。出典は"
        "「厚生労働大臣が指定する病院の病棟並びに厚生労働大臣が定める病院、基礎係数、機能評価係数Ⅰ、機能評価係数Ⅱ、"
        "救急補正係数及び激変緩和係数」(平成24年厚生労働省告示第165号) を令和8年厚生労働省告示第99号で改正した後の"
        f"別表第一〜第六 (特設ページ「８．ＤＰＣ」の別表 xlsx、{D.retrieved} 取得)。適用は {APPLIES_FROM} から。",
        "",
        f"## {HOSPITAL}の係数 ({group.replace('（', ' = ').rstrip('）')})",
        "",
    ] + table(
        ["係数", "値", "出どころ"],
        [
            ["基礎係数", f"**{found[3]}**", f"{group} (群で共通)"],
            ["機能評価係数Ⅱ", f"**{found[4]}**", "病院ごと (年度ごとに改定)"],
            ["救急補正係数", f"**{found[5]}**", "病院ごと"],
            ["激変緩和係数", f"**{found[6]}**", "病院ごと (改定年度だけ)"],
            ["機能評価係数Ⅰ", "当院が届け出ている診療料の係数の**合計** (下の一覧から)", "別表第四〜第六。**この値は告示に無い**"],
        ],
    ) + [
        f"**医療機関別係数 = 基礎係数 + 機能評価係数Ⅰ + 機能評価係数Ⅱ + 救急補正係数 + 激変緩和係数** "
        f"(留意事項通知 第3の1(2))。当院は **{float(found[3]) + float(found[4]) + float(found[5]) + float(found[6]):.4f} + 機能評価係数Ⅰ**。"
        "機能評価係数Ⅰは、当院が地方厚生局に届け出ている入院基本料・入院基本料等加算などに対応する係数を下の一覧から拾って"
        "足したもの。**どれを届け出ているかはここでは分からない** — 医事課 (施設基準係) に確認するか、"
        # Section of the in-hospital manuals. A directory in the existing tree (checked in data/doc on 2026-10-06)
        f"`{ddir('/院内マニュアル/施設基準届出状況')}` (専任・専従の登録名簿であって届出一覧ではない) を手掛かりにする。"
        "届出を受理された翌月 1 日から合算 (同(2))。",
        "",
        "医療機関群ごとの基礎係数: "
        + " / ".join(f"{g.replace('（', ' (').replace('）', ')')} {', '.join(v)}" for g, v in base_by_group.items())
        + f"。病院数は {', '.join(f'{g[:4]} {len(r)}' for g, r in groups.items())}。他院の係数は原本 {D.keisu_url}。",
        "",
        "## 機能評価係数Ⅰ の一覧 (別表第四〜第六)",
        "",
        "医科点数表の診療料 (入院基本料・入院基本料等加算など、DPC では包括される項目) を係数に置き換えたもの。"
        "当院が届け出ている項目の係数を合計する。",
        "",
    ] + table(["別表", "医科点数表に規定する診療料", "機能評価係数Ⅰ"], k1_rows) + [
        "## 注意",
        "",
        "- 機能評価係数Ⅱ・救急補正係数は**毎年度改定される** (係数告示の改正)。激変緩和係数は改定年度のみ。"
        "ここは令和8年度の値",
        "- 請求点数 = 分類ノードの 1 日あたり点数 × 医療機関別係数 × 入院日数 (月合計で小数第 1 位を四捨五入)",
        "- 係数の適用や届出の有無の最終判断は医事課",
    ]
    return "\n".join(aux_front(D, f"{HOSPITAL}の医療機関別係数 (基礎係数・機能評価係数Ⅰ/Ⅱ・救急補正係数)") + body), {
        "row": found, "group": group, "k1_rows": len(k1_rows),
    }


# --- Notifications ---------------------------------------------------------

FOOTER_RE = re.compile(r"^\s*-\s*\d+\s*-\s*$")
DAI_RE = re.compile(r"^\s*第([１-９])\s+(\S.*)$")


def clean_txt(text: str) -> list[str]:
    out = []
    blank = 0
    for line in text.splitlines():
        line = line.replace("\x0c", "").rstrip()
        if FOOTER_RE.match(line):
            continue
        if not line.strip():
            blank += 1
            if blank > 1:
                continue
        else:
            blank = 0
        out.append(line)
    return out


def split_dai(lines: list[str]) -> tuple[list[str], list[tuple[str, str, list[str]]]]:
    """Split at "第N ..." headings. Returns (preamble, [(number, heading, body lines)])."""
    pre: list[str] = []
    parts: list[tuple[str, str, list[str]]] = []
    for line in lines:
        m = DAI_RE.match(line)
        if m and (not parts or True):
            parts.append((m.group(1), m.group(2).strip(), [line]))
            continue
        (parts[-1][2] if parts else pre).append(line)
    return pre, parts


def fence(lines: list[str]) -> list[str]:
    return ["```", *lines, "```", ""]


def ryui_parts() -> tuple[list[str], list[tuple[str, str, list[str]]], int]:
    """The notes-of-attention notification split at "第N" (preamble, chapters, total chapter chars).
    Split into child nodes when the char count exceeds TSUCHI_SPLIT."""
    lines = clean_txt((CACHE / "ryui_tsuchi.txt").read_text(encoding="utf-8"))
    pre, parts = split_dai(lines)
    return pre, parts, sum(len("\n".join(p[2])) for p in parts)


def ryui_nodes(D: Data) -> dict[str, str]:
    src = D.manifest["sources"]["ryui_tsuchi.pdf"]
    pre, parts, total = ryui_parts()
    title = "DPC 留意事項通知 (算定方法の一部改正等に伴う実施上の留意事項、令和8年3月18日 保医発0318第4号)"
    lead = (
        "「厚生労働大臣が指定する病院の病棟における療養に要する費用の額の算定方法の一部改正等に伴う実施上の"
        "留意事項について」(令和8年3月18日 保医発0318第4号)。**DPC の対象患者・診断群分類区分の決め方・"
        "入院期間・包括範囲・再入院・転棟の規則の正典**。適用は 2026-06-01 から。"
        f"原本 {src['url']} ({D.retrieved} 取得、sha256 {src['sha256'][:16]}…)。"
        "本文は `pdftotext -layout` の桁揃えのままコードフェンスに入れてある。"
    )
    files: dict[str, str] = {}
    if total <= TSUCHI_SPLIT:
        body = [lead, ""] + fence(pre + sum((p[2] for p in parts), []))
        files[f"{N_RYUI}.md"] = "\n".join(aux_front(D, title) + body) + "\n"
        return files
    names = {p[0]: f"第{p[0]}_{fs_safe(p[1])[:30]}" for p in parts}
    idx = [
        lead,
        "",
        f"通知が {total:,} 字と大きいので「第N」ごとに子ノードに分けてある。**このノードには前文しか無い。**",
        "",
    ] + table(
        ["子ノード", "見出し", "字数"],
        [[f"`{leaf(f'{SEC_DOC}/{N_RYUI}/{names[p[0]]}')}`", f"第{p[0]} {p[1]}", f"{len(chr(10).join(p[2])):,}"] for p in parts],
    ) + ["## 前文", ""] + fence(pre)
    files[f"{N_RYUI}/_index.md"] = "\n".join(aux_front(D, title, child_fields=True) + idx) + "\n"
    for num, head, body_lines in parts:
        body = [
            f"{lead}",
            "",
            f"**ここには「第{num} {head}」しか入っていない。** 他の章は `{ddir(f'{SEC_DOC}/{N_RYUI}')}` (親) が名乗る子ノードにある。",
            "",
        ] + fence(body_lines)
        files[f"{N_RYUI}/{names[num]}.md"] = "\n".join(aux_front(D, f"{title} — 第{num} {head}") + body) + "\n"
    return files


def sanka_node(D: Data) -> str:
    src = D.manifest["sources"]["sanka_tsuchi.pdf"]
    lines = clean_txt((CACHE / "sanka_tsuchi.txt").read_text(encoding="utf-8"))
    # exclude the annexes (forms)
    end = next((i for i, l in enumerate(lines) if re.match(r"^\s*（別紙１）", l)), len(lines))
    body_lines = lines[:end]
    body = [
        "「ＤＰＣ制度への参加等の手続きについて」(令和8年3月27日 保医発0327第1号)。**DPC 対象病院の基準"
        "(急性期一般入院基本料等の届出、診療録管理体制加算、DPC 調査への参加、(データ/病床) 比 0.875 以上、"
        "月 90 件以上、コーディング委員会 年 4 回以上)・制度への参加と退出・再編・準備病院・患者への周知**を定める通知。"
        f"適用は 2026-06-01 から。原本 {src['url']} ({D.retrieved} 取得、sha256 {src['sha256'][:16]}…)。",
        "",
        "**様式 (別紙1〜16、届出書・申請書) は収録していない** — 原本 PDF を見る。"
        f"点数の算定方法は `{D.ryui_ref}`、当院の係数は `{aux(N_KEISU)}`。",
        "",
    ] + fence(body_lines)
    return "\n".join(aux_front(D, "DPC 制度への参加等の手続きについて (令和8年3月27日 保医発0327第1号)") + body)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")


def copy_static(D: Data) -> None:
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    for old in STATIC_DIR.glob("dpc_*.xlsx"):
        if old.name != D.static_xlsx:
            old.unlink()
            print(f"  removed old static: {old}")
    shutil.copyfile(CACHE / "dpc.xlsx", STATIC_DIR / D.static_xlsx)
    shutil.copyfile(CACHE / "keisu_beppyo.xlsx", STATIC_DIR / KEISU_STATIC)


def main() -> None:
    D = Data()
    if SECTION.exists():
        shutil.rmtree(SECTION)
    SECTION.mkdir(parents=True)

    stats = {"bunrui": 0, "codes": 0, "inc": 0, "icd": 0, "surg": 0, "split_icd": 0, "split_surg": 0, "nodes": 0, "max_body": (0, "")}
    mdc_entries: dict[str, list] = {}
    sizes: list[int] = []
    for mdc in sorted(D.mdc):
        keys = sorted(k for k in D.bunrui if k.startswith(mdc))
        if not keys:
            continue
        mdc_dir = SECTION / fs_safe(f"{mdc}_{D.mdc[mdc]}")
        entries = []
        for key in keys:
            slug, files = bunrui_node(D, key, f"{SEC_DOC}/{mdc_dir.name}")
            for rel, text in files.items():
                write(mdc_dir / rel, text)
                stats["nodes"] += 1
                n = len(text)
                sizes.append(n)
                if n > stats["max_body"][0]:
                    stats["max_body"] = (n, str(mdc_dir / rel))
            rows = D.points[key]
            inc = sum(1 for r in rows if r[12] != "")
            split = len(files) > 1
            entries.append((key, slug, len(rows), inc, split))
            stats["bunrui"] += 1
            stats["codes"] += len(rows)
            stats["inc"] += inc
            stats["icd"] += len(D.icd.get(key, []))
            stats["surg"] += len(D.surg.get(key, []))
            stats["split_icd"] += int(f"{slug}/{ICD_CHILD}.md" in files)
            stats["split_surg"] += int(f"{slug}/{SURGERY_CHILD}.md" in files)
        write(mdc_dir / "_index.md", mdc_index(D, mdc, entries))
        stats["nodes"] += 1
        mdc_entries[mdc] = entries

    aux_titles = {
        N_YOMIKATA: "14 桁の構成・入院期間Ⅰ/Ⅱ/Ⅲ・包括範囲・包括対象外・用語 (最初に読む)",
        N_KEISU: f"{HOSPITAL} の基礎係数・機能評価係数Ⅱ・救急補正係数と、機能評価係数Ⅰの一覧",
        N_DEKIDAKA: "診断群分類点数表の対象外になる手術等 (臓器移植・厚生労働大臣指定) の K コード一覧",
        N_DUMMY: "手術・処置等の分岐に使うダミーコード (KKK0 / 0005 化学療法 等) の一覧",
        N_SANTEI: "算定告示 (令和8年厚労省告示第96号) 別表 項 1〜20 — 所定点数・包括範囲・期間Ⅲ超・調整日・係数の正典 (要点 + 条文)",
        N_KASAN: "算定告示 項 4〜14 の表 — 特定入院料 (ICU・HCU・NICU 等) の DPC 加算点数と入院基本料の減算点数 (1 行 = 種別 × 期間 × 点数)",
        N_RYUI: "留意事項通知 (保医発0318第4号) の全文 — 対象患者・区分の決め方・包括範囲・再入院の正典",
        N_SANKA: "参加手続き通知 (保医発0327第1号) — DPC 対象病院の基準・参加・退出 (様式は除く)",
    }
    write(SECTION / f"{N_YOMIKATA}.md", yomikata_node(D))
    write(SECTION / f"{N_DEKIDAKA}.md", dekidaka_node(D))
    write(SECTION / f"{N_DUMMY}.md", dummy_node(D))
    keisu_text, keisu_info = keisu_node(D)
    write(SECTION / f"{N_KEISU}.md", keisu_text)
    ryui = ryui_nodes(D)
    for rel, text in ryui.items():
        write(SECTION / rel, text)
    write(SECTION / f"{N_SANKA}.md", sanka_node(D))
    santei = santei_nodes(D)
    for rel, text in santei.items():
        write(SECTION / rel, text)
    stats["nodes"] += 5 + len(ryui) + len(santei)
    write(SECTION / "_index.md", section_index(D, mdc_entries, aux_titles, stats))
    stats["nodes"] += 1

    copy_static(D)

    changed = {k: dict(v) for k, v in D.changed.items()}
    sizes.sort()
    print(
        f"{SECTION_NAME}: mdc={len(mdc_entries)} bunrui={stats['bunrui']} codes={stats['codes']} "
        f"(inc={stats['inc']} exc={stats['codes'] - stats['inc']}) icd={stats['icd']} surg={stats['surg']}\n"
        f"  nodes={stats['nodes']} split_icd={stats['split_icd']} split_surg={stats['split_surg']} "
        f"ryui_nodes={len(ryui)} median_body={sizes[len(sizes) // 2]} max_body={stats['max_body'][0]}({stats['max_body'][1]}) "
        f"over20k={sum(1 for s in sizes if s > 20000)}\n"
        f"  version={D.version['rank']} updated={D.version['updated']} static={D.static_xlsx} "
        f"keisu={HOSPITAL} {keisu_info['group'][:4]} base={keisu_info['row'][3]} k2={keisu_info['row'][4]} "
        f"kyukyu={keisu_info['row'][5]} k1_rows={keisu_info['k1_rows']}\n"
        f"  kokuji96: kou={D.kokuji_stats['kou']} blocks={D.kokuji_stats['blocks']} rows={D.kokuji_stats['rows']}\n"
        f"  xref: qa_dpc={'yes' if QA_DPC_DIR.exists() else 'no'} (nodes {D.qa_nodes}, named classifications {len(D.qa_by_key)})\n"
        f"  changed_sheets={dict(D.changed_sheets)} changed_bunrui={len(changed)}" + (f" {changed}" if changed else " (change category is 0 in every row)")
    )



# ---------------------------------------------------------------------------
# Annex items 1-20 of the calculation notification (MHLW Notification No. 96 of 2026)
# ---------------------------------------------------------------------------
#
# The original is a 114-page PDF. **Only pp. 1-9 (amendment text, transitional measures) are
# vertical**; pp. 10-36 are the horizontal annex (items 1-19), pp. 37- the landscape DPC fee table
# (same content as the xlsx, discarded), and **item 20** (definition of the hospital coefficient)
# is on the last page, 114. fetch dumped per-line words with x coordinates via pdfplumber, so here
# we (a) cut items where a line starts with a sequential number at x0 < 100, and (b) rebuild the
# two-column tables of items 4-14 (left = wrapped category number + fee name, right = type, period,
# points, notes) from the x coordinates. pdftotext -layout is not used: it drops the right-column
# indent at page breaks (a note like "２ 基本診療料…" lands at the line start).

N_SANTEI = "00_算定方法"
N_KASAN = "00_特定入院料のDPC加算"

KOU_NUM_RE = re.compile(r"^[０-９0-9]{1,2}$")
POINT_RE = re.compile(r"^[0-9０-９,，]+点?$")
LEFT_MAX = 150  # left of this is the left column (item body or table's left-column label)
NOTE_MIN = 218  # right-column lines starting right of this continue a note (イ・ロ・ハ or wrapped text)
CHAR_W = 10.5  # width of one body character (pt). Used to restore indentation

# item -> (short description of the hospital category, nature of the table). The condition text itself is quoted from the item body
KOU_HOSPITAL = {
    4: ("4に規定する病院", "A104 特定機能病院入院基本料の届出病院 (**当院 = 北海道大学病院が該当**)"),
    5: ("5に規定する病院", "A105 専門病院入院基本料の届出病院"),
    6: ("6に規定する病院", "4・5 以外の病院 (A100 一般病棟入院基本料など)"),
}
KOU_GROUP = {4: 4, 5: 5, 6: 6, 7: 4, 8: 5, 9: 6, 10: 4, 11: 5, 12: 6, 13: 5, 14: 6}
KOU_KIND = {
    4: "特定入院料の加算 (所定点数に加算)", 5: "特定入院料の加算 (所定点数に加算)", 6: "特定入院料の加算 (所定点数に加算)",
    7: "退院日の減算 (退院が特定の時間帯に集中している病院)", 8: "退院日の減算 (同)", 9: "退院日の減算 (同)",
    10: "入院日・退院日の減算 (入院日及び退院日が特定の日に集中している病院)", 11: "入院日・退院日の減算 (同)", 12: "入院日・退院日の減算 (同)",
    13: "夜間看護体制特定日減算", 14: "夜間看護体制特定日減算",
}


def _z2h_num(s: str) -> int:
    return int(s.translate(str.maketrans("０１２３４５６７８９", "0123456789")))


def _join_words(words: list[list]) -> str:
    """Join words back into one line: one space for a gap of a character or more, two before points."""
    out = ""
    prev_x1 = None
    for x0, x1, text in words:
        if prev_x1 is not None:
            if x0 >= 400 and POINT_RE.match(text):
                out += "  "
            elif x0 - prev_x1 > 4:
                out += " "
        out += text
        prev_x1 = x1
    return out


def _render(words: list[list], base: float = 72.0) -> str:
    indent = max(0, round((words[0][0] - base) / CHAR_W))
    return "  " * indent + _join_words(words)


class Kokuji:
    """Holds the annex of the calculation notification per item. `text[n]` = lines of item n
    (indent restored), `tables[n]` = tables of items 4-14 (list of category blocks)."""

    def __init__(self, pages: list[dict]) -> None:
        body = [p for p in pages if p["kind"] == "body"]
        if not body:
            sys.exit("calculation notification: no horizontal body page (check kokuji96_lines.json from fetch)")
        self.text: dict[int, list[str]] = {}
        self.raw: dict[int, list[list[list]]] = {}
        expect = 1
        cur = 0
        for p in body:
            for ln in p["lines"]:
                first = ln[0]
                if KOU_NUM_RE.match(first[2]) and first[0] < 100 and len(ln) > 1 and _z2h_num(first[2]) == expect:
                    cur = expect
                    expect += 1
                    self.text[cur] = []
                    self.raw[cur] = []
                if cur == 0:
                    continue  # before the "別表" heading (rest of the amendment text)
                self.text[cur].append(_render(ln))
                self.raw[cur].append(ln)
        if 19 not in self.text:
            sys.exit(f"calculation notification: did not reach item 19 (got items {sorted(self.text)})")
        # Item 19 is just the 2 lines "診断群分類点数表は、次のとおり". Drop any fee-table header lines that follow
        self.text[19] = self.text[19][:2]
        self.raw[19] = self.raw[19][:2]
        self.tables = {n: self._table(n) for n in range(4, 15) if n in self.raw}

    def _table(self, n: int) -> dict:
        """Condition text and category blocks of item n. Two passes: first cut blocks at left-column
        text starting with "区分番号" (left labels wrap over 2-4 lines, so read them once complete),
        then classify right-column lines as heading / type / row / note."""
        cond: list[str] = []
        blocks: list[dict] = []
        cur: dict | None = None
        for ln in self.raw[n]:
            left = [w for w in ln if w[0] < LEFT_MAX]
            right = [w for w in ln if w[0] >= LEFT_MAX]
            if left and left[0][2].startswith("区分番号"):
                cur = {"label": "", "right": []}
                blocks.append(cur)
            if cur is None:
                cond.append(_render(ln))
                continue
            if left:
                cur["label"] += "".join(w[2] for w in left)
            if right:
                cur["right"].append(right)
        for b in blocks:
            m = re.match(r"区分番号(Ａ[０-９－]+)に掲げる(.+)", b["label"])
            if not m:
                sys.exit(f"calculation notification item {n}: cannot parse left-column label: {b['label']!r}")
            b["kubun"] = z2h(m.group(1)).replace("－", "-")
            b["name"] = m.group(2)
            b["rows"], b["notes"] = self._classify(b["right"], b["name"])
            del b["right"]
        return {"cond": cond, "blocks": blocks}

    @staticmethod
    def _classify(lines: list[list[list]], name: str) -> tuple[list[tuple[str, str, str]], list[str]]:
        rows: list[tuple[str, str, str]] = []
        notes: list[str] = []
        sub = ""
        sub_fresh = False
        in_note = False
        for i, right in enumerate(lines):
            rx = right[0][0]
            last = right[-1]
            has_point = POINT_RE.match(last[2]) is not None and last[0] >= 400
            text = _join_words(right[:-1]) if has_point else _join_words(right)
            if in_note or rx >= NOTE_MIN or text.startswith("注") or (
                KOU_NUM_RE.match(right[0][2]) and not has_point
            ):
                in_note = True
                notes.append(_render(right, base=189.0))
                continue
            if has_point:
                rows.append((sub, text, last[2]))
                sub_fresh = False
                continue
            # Line without points: on the first line, the fee name (same as left column) is a heading; otherwise a type (wrapped lines are joined)
            if i == 0 and not rows and (name.startswith(text) or text.startswith(name)):
                continue
            sub = (sub + text) if sub_fresh else text
            sub_fresh = True
        return rows, notes


def santei_nodes(D: Data) -> dict[str, str]:
    pages = json.loads((CACHE / "kokuji96_lines.json").read_text(encoding="utf-8"))
    K = Kokuji(pages)
    src = D.manifest["sources"]["kokuji96.pdf"]
    files: dict[str, str] = {}
    lead = (
        "「厚生労働大臣が指定する病院の病棟における療養に要する費用の額の算定方法」(平成20年厚生労働省告示第93号) の"
        "**別表** — 令和8年厚生労働省告示第96号による改正後。DPC の **1 日あたり点数の定義 (項 1)・包括に含まれる費用と"
        "含まれない費用 (項 2)・特定入院料の加算と減算 (項 4〜14)・入院日Ⅲ超 (項 15)・調整日 (項 16)・1 点 10 円 (項 17)・"
        "端数 (項 18)・医療機関別係数 (項 20)** の正典。項 19 が診断群分類点数表そのもの (各分類ノードにある)。"
        f"適用は {APPLIES_FROM} から。原本 {src['url']} ({D.retrieved} 取得、sha256 {src['sha256'][:16]}…、114p)。"
    )
    kasan_doc = aux(N_KASAN)
    body = [
        lead,
        "",
        "**原本の PDF は 1〜9 ページが縦書きの改め文・経過措置、10〜36 ページが横書きの別表 (この節)、37 ページ以降が"
        "診断群分類点数表 (電子点数表 xlsx と同内容)、最終ページに項 20。** 改め文と経過措置は収録していない "
        f"(経過措置の中身は留意事項通知 第4 `{D.ryui_ref}` にも要点がある)。",
        "",
        "## 要点 (読んでから条文へ)",
        "",
        "- **項 1**: 1 日あたりの額 = 診断群分類点数表の点数 × **項 20 の医療機関別係数** (= 所定点数)。係数の値は "
        f"`{aux(N_KEISU)}`",
        "- **項 2**: 所定点数に**含まれる**費用 = 入院基本料 / 入院基本料等加算 / 短期滞在手術等基本料 / 医学管理等 / 検査 / 画像診断 / "
        "投薬 / 注射 / リハ・精神科専門療法の薬剤料 / 処置 / 病理標本作製料 (イ (1)〜(12))。**そこから除かれる = 出来高で別に算定する**費用が "
        "ロ (1)〜(11): 入院基本料の一部の注加算、A200 急性期総合体制加算など多数の入院基本料等加算、短期滞在手術等基本料1、"
        "B000〜B001-3-3 と B001-6〜B015 の医学管理料、D206 心臓カテーテル・内視鏡検査 (D295〜D325)・D401〜D419-2 の検査、"
        "画像診断管理加算・E003 の一部、G020 無菌製剤処理料、J001 熱傷処置5・人工腎臓 J038〜J042 など 1,000 点以上相当の処置、"
        "N003 術中迅速病理、抗 HIV 薬、血友病の凝固因子製剤。**手術・麻酔・放射線治療・リハ本体・精神科専門療法は最初から含まれない** (イに無い)",
        "- **項 3**: 所定点数に含まれない費用は医科点数表 (出来高) で算定",
        f"- **項 4〜6**: 特定入院料 (救命救急入院料・ICU・HCU・SCU・PICU・NICU・MFICU・GCU・一類感染症・小児入院医療管理料) を届け出た病棟では、"
        f"その算定要件を満たす患者ごとに**所定点数に加算**する点数がある。病院の区分 (4 = A104 特定機能病院 = **当院**、5 = A105 専門病院、6 = それ以外) で表が違う。"
        f"表は `{kasan_doc}` に 1 行 = 種別 × 期間 × 点数で起こしてある",
        f"- **項 7〜9** (退院日の減算)、**項 10〜12** (入院日・退院日の減算)、**項 13〜14** (夜間看護体制特定日減算): 入院基本料の種別ごとに"
        f"**所定点数から減じる**点数。同じく `{kasan_doc}`",
        "- **項 15**: 入院日Ⅲを超えた日以降は医科点数表 (出来高)。ただし化学療法等の分類で入院日Ⅲまでに化学療法等が実施されなかった場合、"
        "Ⅲ超の期間の抗悪性腫瘍剤等の薬剤料は算定できない。地域包括ケア病棟へ転棟した患者は入院日Ⅱまで",
        "- **項 16 (調整日)**: 退院の日・DPC 対象外の病棟等へ転棟する日の前日・入院日Ⅲを超える日の前日 = 調整日。調整日に適用する診断群分類区分が"
        "前日までと違えば、前月までに算定した額との**差額を調整日の属する月で調整**する",
        "- **項 17**: 1 点 = 10 円。**項 18**: 保険者ごとの請求額の 1 円未満は切り捨て",
        "- **項 20**: 医療機関別係数 = 基礎係数 + 機能評価係数Ⅰ + 機能評価係数Ⅱ + 救急補正係数 + 激変緩和係数 (係数告示で病院ごとに定める)",
        "",
        "## 条文 (別表 項 1〜20)",
        "",
        "pdfplumber で語の座標から字下げを復元した本文。項 4〜14 は条件文だけを置き、表 (区分番号ごとの点数) は "
        f"`{kasan_doc}` にある (ここには載っていない)。",
        "",
    ]
    for n in sorted(K.text):
        body.append(f"### 項 {n}")
        body.append("")
        if n in K.tables:
            body += fence(K.tables[n]["cond"])
            body.append(
                f"(項 {n} の表: {'、'.join(b['kubun'] + ' ' + b['name'] for b in K.tables[n]['blocks'])} → `{kasan_doc}`)"
            )
            body.append("")
        else:
            body += fence(K.text[n])
    body += [
        "## 注意",
        "",
        f"- 収録した版: 令和8年厚生労働省告示第96号 (特設ページ「８．ＤＰＣ」、{D.retrieved} 取得)。訂正告示が出たら差し替える",
        f"- 通知側の読み方 (対象患者・区分の決め方・再入院) は `{D.ryui_ref}`、共通ルールの要約は `{aux(N_YOMIKATA)}`",
        "- 改め文 (第1〜第3 の改正条文) と縦書きの経過措置は未収録。原本 PDF の 1〜9 ページ",
    ]
    files[f"{N_SANTEI}.md"] = "\n".join(aux_front(D, "DPC 算定告示の別表 (療養に要する費用の額の算定方法、令和8年厚生労働省告示第96号) 項 1〜20") + body) + "\n"

    # --- addition / reduction tables ---
    rows_all = 0
    kb = [
        "算定告示 (令和8年厚生労働省告示第96号) 別表の **項 4〜14 の表**を、1 行 = 入院料の種別 × 期間 (条件) × 点数 に起こしたもの。"
        "原本は 2 列レイアウト (左欄に区分番号と診療料名が折り返し、右欄に種別・期間・点数・注) で、pdfplumber の語の x 座標から組み直した。"
        f"項の条件文と要点は `{aux(N_SANTEI)}`。{D.retrieved} 取得、原本 {src['url']}。",
        "",
        "## 読み方",
        "",
        "- **項 4〜6 = 加算**: 診断群分類点数表の 1 日あたり点数 (所定点数) に、その特定入院料の届出病棟で算定要件を満たす患者について"
        "**期間ごとの点数を加算**する (係数を掛けるのは所定点数。加算点数の扱いは留意事項通知 第3の1(4))。同じ特定入院料でも"
        "**病院の区分 (項) で点数が違う**ので、まず自院の項を選ぶ",
        "- **項 7〜14 = 減算**: 入院基本料の種別ごとに、退院日 (7〜9)・特定の入院日と退院日 (10〜12)・夜間看護体制特定日 (13〜14) に"
        "所定点数から減じる",
        "- 「注」(精神疾患診断治療初回加算・救急体制充実加算・早期離床・リハビリテーション加算・重症患者対応体制強化加算・小児加算 など) は"
        "各区分の表の下にコードフェンスで原文のまま置いた。これらも DPC で算定できる加算",
        "- 期間の「３日以内」「４日以上７日以内」は、その特定入院料を算定し始めた日からの日数 (医科点数表の各区分の期間と同じ)",
        "",
    ] + table(
        ["項", "病院の区分", "表の性質", "区分"],
        [
            [str(n), f"{KOU_HOSPITAL[KOU_GROUP[n]][0]} = {KOU_HOSPITAL[KOU_GROUP[n]][1]}", KOU_KIND[n],
             "、".join(f"{b['kubun']}" for b in K.tables[n]["blocks"])]
            for n in sorted(K.tables)
        ],
    )
    for n in sorted(K.tables):
        t = K.tables[n]
        grp = KOU_HOSPITAL[KOU_GROUP[n]]
        kb += [f"## 項 {n} — {KOU_KIND[n]}: {grp[0]} ({grp[1]})", "", "条文 (条件):", ""]
        kb += fence(t["cond"])
        for b in t["blocks"]:
            kb += [f"### 項 {n} {b['kubun']} {b['name']}", ""]
            kb += table(
                ["項", "区分番号", "診療料", "種別", "期間・条件", "点数"],
                [[str(n), f"`{b['kubun']}`", b["name"], sub, name, pt] for sub, name, pt in b["rows"]],
            )
            rows_all += len(b["rows"])
            if b["notes"]:
                kb += ["注 (原文):", ""] + fence(b["notes"])
    kb += [
        "## 注意",
        "",
        f"- 収録した版: 令和8年厚生労働省告示第96号 ({D.retrieved} 取得)。表の行数 {rows_all}",
        "- 表の点数は**所定点数 (診断群分類点数表の点数 × 医療機関別係数) に加算・減算する値**で、係数は掛けない (留意事項通知 第3の1(4))。"
        "最終判断は医事課",
        f"- 当院が該当する項 (4・7・10) は A104 特定機能病院入院基本料の届出による。届出状況は医事課へ (`{aux(N_KEISU)}` も参照)",
    ]
    files[f"{N_KASAN}.md"] = "\n".join(aux_front(D, "特定入院料の DPC 加算・減算表 (算定告示 別表 項 4〜14)") + kb) + "\n"
    D.kokuji_stats = {"kou": len(K.text), "rows": rows_all, "blocks": sum(len(t["blocks"]) for t in K.tables.values())}
    return files


if __name__ == "__main__":
    main()
