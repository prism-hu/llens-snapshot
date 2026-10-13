"""Per-CQ intermediate JSON (output of parse_to_entries.py) → delivery node MD.

Mechanizes step 4 of `docs/cage/11_lung_cancer_2024.md` (read the intermediate JSON and write
node MD). The 2024 ingestion ran step 4 by hand plus subagents; the 2025 regeneration on
2026-09-12 produced all 202 CQs at once with this script.

In three lines:

    cache/html/2025/*.html          originals (already fetched)
      → parse_to_entries.py         per-CQ intermediate JSON (everything mechanically determined)
      → render_lung_cancer_entries.py  node MD

Parts mechanically determined by the original (source, question, recommendations, strength,
evidence, agreement rate, explanation body, references) are replaced wholesale from the
intermediate JSON. **Only parts involving human judgment are carried over from the existing node**:

- the leading `[context]` abbreviation of `title` (source questions are long, so the delivery side prefixes one)
- the `### a. <treatment name>` headings under `## 解説` (the source only has `a．` labels)
- the value of `**文脈**` (an editorial label, not verbatim from the source; if a chapter name changes between editions, fix it here and regenerate)
- whether a source note is folded into "parentheses at the end of the question" or made a `注:` line
- `EXTRA_NOTES` (disclaimers on nodes that transcribe a source typo as is)

Therefore it **does not run without existing nodes** (the first ingestion does step 4 by hand).
Output is idempotent against existing nodes: the carried-over judgment parts remain in the
output verbatim, so running twice on the same entries changes nothing the second time.

    # originals already fetched into cache/html/2025/ (robots.txt checked / UA / 2.7 s interval)
    uv run python scripts/parse_to_entries.py --out cache/extracts
    uv run python scripts/render_lung_cancer_entries.py --entries cache/extracts

With `--out DIR`, data is left untouched and output goes to DIR (for previewing the diff).
`--list` writes nothing and only prints the diff classification.

All 202 CQs are regenerated (to keep a single path; nodes with only notation differences also
follow the source). `SKIP` is an escape hatch for nodes not to regenerate, currently empty
(`肺癌2025_転移/CQ12` joined the same path on 2026-09-12). The printed classes are "substantive"
(recommendation, strength, agreement rate, question, explanation body, citation numbers,
references or notes differed from the source) / "notation only" (punctuation, brackets,
width, quotes only) / "unchanged".

Each CQ node ends with one line linking to the original web page (with the CQ anchor)
(`原本 Web: [<guideline> <chapter> CQ<N>](<source>)`). The front matter `source` does not
appear in read_doc, so it goes in the body like the `原本 PDF:` line of neurology/dermatology.
The external URL is unreachable from the hospital's closed network, but serves as a pointer
for opening the original off-site (smartphone etc.).

Besides CQ nodes, the **end-of-section regimen** block (`## 項末のレジメン`) at the end of the
chapter `_index.md` is rebuilt. The hand-written TOC above it is left untouched.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import shutil
import sys
import unicodedata
from collections import Counter
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
from textfix import doc_ref  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data" / "doc" / "診療ガイドライン"
ENTRIES_DIR = ROOT / "cache" / "extracts"

# Nodes not to regenerate. Currently none (all 202 CQs go through the same path).
SKIP: set[str] = set()

# Notes attached to nodes that transcribe a source typo as is (human judgment, so kept here).
# Body and references follow the source; this one line is added as `注:` so readers know it is a typo.
EXTRA_NOTES: dict[str, list[str]] = {
    "肺癌2025_転移/CQ12": [
        "参考文献18の「nn-small cell lung cancer」は原本の表記どおり(誤記と思われる)。"
    ],
}

# One line that tells CQs saying "see the end-of-section regimen" how to reach the regimen node.
# {path} is the canonical chapter directory (`{{DOC_BASE}}/診療ガイドライン/<chapter>/`). The intermediate
# JSON's regimen_ref.path is a prefix-less identifier (`/診療ガイドライン/<chapter>`), fixed up when writing
REGIMEN_NOTE = "注: 項末のレジメンは {path} の「{heading}」にある。"
# Heading of the block in the chapter _index.md holding the generated regimen (everything below is rebuilt every run)
REGIMEN_SECTION = "## 項末のレジメン"

# Link line to the original web page at the end of a CQ node (front matter is not shown by read_doc, so it goes in the body)
SOURCE_LINE = "原本 Web: [{label}]({url})"
_SOURCE_LINE_RE = re.compile(r"\n*^原本 Web: \[[^\]]*\]\(\S+\)\n?\Z", re.M)

VOTE_TAIL_TEXT = "(投票結果の合意率は上記「推奨」を参照)"
VOTE_SENT = re.compile(r"下記に，[^。]*投票結果を記載する。?")
_MARKER = re.compile(r"^[＊*※注]+[：:]?\s*")
# Characters ignored as notation variants (differences in these alone do not trigger a rewrite)
TYPO = set('。.，,、・"“”〔〕()（）:：;；/-~〜～ 　')

STRENGTH_JA = {
    ("1", "positive"): "1(強く推奨)",
    ("2", "positive"): "2(弱く推奨)",
    ("1", "negative"): "1(行わないことを強く推奨)",
    ("2", "negative"): "2(行わないことを弱く推奨)",
}


# --------------------------------------------------------------------------
# Reading existing nodes (judgment parts to carry over, and the current state for diffing)
# --------------------------------------------------------------------------

def norm(t: str | None) -> str:
    t = unicodedata.normalize("NFKC", t or "")
    t = t.replace("＊", "").replace("*", "").replace("※", "")
    return re.sub(r"\s+", "", t)


def nomark(t: str | None) -> str:
    """Body with citation numbers removed (separates body differences from citation placement)."""
    t = norm(t)
    t = re.sub(r"\[\d+\][~～]\[\d+\]", "", t)
    return re.sub(r"\[\d+\]|\[ref:[^\]]+\]", "", t)


def strip_note_marker(text: str | None) -> str:
    return _MARKER.sub("", text or "").strip()


def parse_node(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", raw, re.S)
    if not m:
        raise SystemExit(f"cannot read front matter: {path}")
    fm = yaml.safe_load(m.group(1)) or {}
    # The trailing source link line is rebuilt by render every time, so exclude it from reading judgment parts
    body = _SOURCE_LINE_RE.sub("\n", m.group(2))
    out: dict = {"fm": fm, "raw": raw}

    qm = re.search(r"^\*\*質問\*\*:\s*(.*)$", body, re.M)
    out["question"] = qm.group(1).strip() if qm else None
    cm = re.search(r"^\*\*文脈\*\*:\s*(.*)$", body, re.M)
    out["context"] = cm.group(1).strip() if cm else None

    def section(name: str) -> str | None:
        mm = re.search(r"^## %s\n(.*?)(?=\n## |\Z)" % name, body, re.S | re.M)
        return mm.group(1).strip() if mm else None

    rec_sec = section("推奨") or ""
    out["notes"] = [l[len("注:"):].strip() for l in rec_sec.split("\n") if l.startswith("注:")]

    recs = []
    lines = rec_sec.split("\n")
    i = 0
    while i < len(lines):
        l = lines[i]
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        if nxt.startswith("推奨の強さ:") or nxt.startswith("推奨に至る根拠が明確ではない"):
            text = l.strip()
            lm = re.match(r"^([a-z])\.\s+(.*)$", text)
            label = lm.group(1) if lm else None
            if lm:
                text = lm.group(2)
            sm = re.search(r"推奨の強さ:\s*([^/]+?)\s*(?:/|$)", nxt)
            em = re.search(r"エビデンスの強さ:\s*([^/]+?)\s*(?:/|$)", nxt)
            rm = re.search(r"合意率:\s*(.+?)(?:\s*/\s*投票実施年度:.*)?$", nxt)
            ym = re.search(r"投票実施年度:\s*(\S+?)\s*$", nxt)
            recs.append({
                "label": label,
                "text": text.rstrip(),
                "strength": sm.group(1).strip() if sm else (
                    "推奨に至る根拠が明確ではない" if "明確ではない" in nxt else None),
                "evidence": em.group(1).strip() if em else None,
                "rate": rm.group(1).strip() if rm else None,
                "year": ym.group(1).strip() if ym else None,
            })
            i += 2
            continue
        i += 1
    out["recs"] = recs

    exp = section("解説") or ""
    paras = []
    for para in exp.split("\n\n"):
        para = para.strip()
        if not para or para.startswith("### "):
            continue
        para = para.replace(VOTE_TAIL_TEXT, "").strip()
        if para:
            paras.append(para)
    out["explanations"] = paras
    out["exp_headings"] = re.findall(r"^### (.*)$", exp, re.M)

    refs_sec = section("参考文献")
    out["refs"] = []
    if refs_sec:
        for l in refs_sec.split("\n"):
            mm = re.match(r"^(\d+)\.\s+(.*)$", l.strip())
            if mm:
                cite = mm.group(2)
                um = re.search(r"<(\S+)>\s*$", cite)
                url = um.group(1) if um else None
                if um:
                    cite = cite[:um.start()].strip()
                out["refs"].append({"n": int(mm.group(1)), "citation": cite, "url": url})
    return out


# --------------------------------------------------------------------------
# Generating node MD
# --------------------------------------------------------------------------

def yq(s: str) -> str:
    """YAML single-quoted scalar (compatible with yaml.safe_dump)."""
    return "'" + s.replace("'", "''") + "'"


def strength_line(rec: dict) -> str:
    if rec["strength"] == "明確ではない":
        head = "推奨に至る根拠が明確ではない"
    else:
        head = "推奨の強さ: " + STRENGTH_JA.get((rec["strength"], rec["direction"]), rec["strength"])
        head += " / エビデンスの強さ: " + (rec["evidence"] or "")
    rate = rec["agreement_rate"]
    if rate:
        total = rec["agreement_total"] or ""
        head += " / 合意率: " + (rate + " " + total).strip()
    # Vote year from the vote table caption. It can differ per recommendation within one CQ
    # (NSCLC CQ49: a/b in 2025, c in 2018).
    if rec.get("agreement_year"):
        head += " / 投票実施年度: " + rec["agreement_year"]
    return head


def _attach(out: list[str]) -> None:
    """Append the "see agreement rate above" boilerplate to the nearest body paragraph (skipping note lines, tables, headings)."""
    for j in range(len(out) - 1, -1, -1):
        line = out[j].strip()
        if not line or line.startswith(("#", "|", "注:")) or re.match(r"^注[：:]", line):
            continue
        if VOTE_TAIL_TEXT not in out[j]:
            out[j] = out[j] + VOTE_TAIL_TEXT
        return


def _sim(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def render(entry: dict, node: dict, retrieved_at: str, nid: str = "", src_label: str = "") -> str:
    e, n = entry, node
    out: list[str] = ["---", f"retrieved_at: {yq(retrieved_at)}", f"source: {e['source']}"]
    # title = the existing node's `[context]` abbreviation + the full 2025 source question
    m = re.match(r"^(\[[^\]]*\]\s*)", n["fm"].get("title") or "")
    prefix = m.group(1) if m else ""
    out.append(f"title: {yq(prefix + e['question'])}")
    out += ["---", ""]

    # Note placement (parentheses at the end of the question, or a `注:` line) follows the
    # existing node's judgment; the wording comes from the 2025 source (entries' notes).
    # Node-side notes with no counterpart in the source are leftovers of the old edition and are dropped.
    old_q = n["question"] or ""
    qtail = ""
    m = re.search(r"（([^（]*)）\s*$", old_q)
    if m and norm(old_q[:m.start()]).startswith(norm(e["question"])[:20]):
        qtail = m.group(1)
    in_paren: list[str] = []
    in_note: list[str] = []
    for note in e["notes"]:
        k = norm(note)
        score_q = _sim(k, norm(qtail)) if qtail else 0.0
        score_n = max((_sim(k, norm(x)) for x in n["notes"]), default=0.0)
        if score_q >= 0.6 and score_q >= score_n:
            in_paren.append(note)
        elif score_n >= 0.6:
            in_note.append(note)
        elif len(note) <= 50 and not in_paren:
            in_paren.append(note)   # short notes fold into parentheses at the end of the question (per the procedure doc)
        else:
            in_note.append(note)
    # The 「注：…」 the source puts below the vote table (e.g. why 40% is a weak recommendation) also
    # becomes a `注:` line. It is source text; without it the agreement rate is left hanging.
    seen_vn = set()
    for rec in e["recommendations"]:
        vn = rec.get("agreement_note")
        if not vn:
            continue
        k = strip_note_marker(vn)
        if k in seen_vn:
            continue
        seen_vn.add(k)
        in_note.append(k)
    # Disclaimer for nodes that transcribe a source typo as is
    in_note += EXTRA_NOTES.get(nid, [])

    q = e["question"]
    if in_paren:
        q = q + "（" + "，".join(in_paren) + "）"
    out += [f"**質問**: {q}", "", f"**文脈**: {n['context']}", "", "## 推奨", ""]

    last_heading = None
    for rec in e["recommendations"]:
        gh = rec.get("group_heading")
        if gh and gh != last_heading:
            out += [gh, ""]
            last_heading = gh
        label = rec["label"]
        if label:
            out.append(f"{label}. {rec['text']}  ")
            out.append("   " + strength_line(rec))
        else:
            out.append(f"{rec['text']}  ")
            out.append(strength_line(rec))
        out.append("")
    for note in in_note:
        out.append(f"注: {note}")
    if in_note:
        out.append("")
    out += ["## 解説", ""]

    # The `### a. <treatment name>` headings are human-written; carried over from the existing node.
    # Match by order when block and heading counts agree, otherwise by label
    # (prevents headings shifting in CQs that have a preamble block with label=null).
    headings = list(n["exp_headings"])
    blocks = e["explanations"]
    if len(headings) == len(blocks):
        assign = dict(enumerate(headings))
    else:
        by_letter = {}
        for h in headings:
            m = re.match(r"^([a-z])[.．]", h)
            if m:
                by_letter[m.group(1)] = h
        assign = {i: by_letter[x["label"]] for i, x in enumerate(blocks) if x["label"] in by_letter}

    for bi, x in enumerate(blocks):
        if len(blocks) > 1 and bi in assign:
            out += [f"### {assign[bi]}", ""]
        paras = [p.strip() for p in x["markdown"].split("\n\n") if p.strip()]
        lab = x["label"]
        if lab and bi not in assign and paras and not paras[0].startswith(f"**{lab}．**"):
            # Labeled blocks that cannot get a heading put the source `a．` back into the body
            paras[0] = f"**{lab}．**" + paras[0]
        for p in paras:
            p2 = VOTE_SENT.sub("", p).strip()
            if p2 != p:
                if p2:
                    p2 = p2 + VOTE_TAIL_TEXT
                else:
                    # Paragraph consisting only of the guide sentence. Leave no blank line; append the boilerplate to the previous non-「注：」 paragraph
                    _attach(out)
            if p2:
                out += [p2, ""]
                if e.get("regimen_ref") and "項末" in p2:
                    ref = e["regimen_ref"]
                    out += [REGIMEN_NOTE.format(path=doc_ref(ref["path"], is_dir=True),
                                                heading=ref["heading"]), ""]
        if x.get("vote_sentence_after"):
            _attach(out)

    if e["references"]:
        out += ["## 参考文献", ""]
        for r in e["references"]:
            line = f"{r['n']}. {r['citation']}"
            if r["url"]:
                line += f" <{r['url']}>"
            out.append(line)
        out.append("")

    if src_label:
        out += [SOURCE_LINE.format(label=f"{src_label} {e['id']}", url=e["source"]), ""]

    text = re.sub(r"\n{3,}", "\n\n", "\n".join(out))
    if not text.endswith("\n"):
        text += "\n"
    return text


def source_label(index_md: Path) -> str:
    """Build the source link label (`<guideline> <chapter>`) from the chapter `_index.md` front matter."""
    raw = index_md.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n", raw, re.S)
    fm = (yaml.safe_load(m.group(1)) if m else None) or {}
    if not fm.get("guideline") or not fm.get("title"):
        raise SystemExit(f"_index.md lacks guideline / title: {index_md}")
    return f"{fm['guideline']} {fm['title']}"


def render_regimen_section(blocks: list[dict]) -> str:
    """Write the end-of-section regimens into the trailing block of the chapter `_index.md`.

    The source divides a chapter into sections and ends each with a "regimen" (dose / day / cycle
    tables). CQ bodies only point there ("see the end-of-section regimen"), so without picking it
    up the target is missing from the delivery tree. The tables are used across sections, so they
    go in the chapter rather than individual CQs, with a `### <section name>` per section
    (source subheadings hang one level below).
    """
    out = [REGIMEN_SECTION, "",
           "原本が各項の末尾に置いているレジメン(投与量・投与日・サイクル)。"
           "CQ 本文の「項末のレジメンを参照」はここを指す。", ""]
    for b in blocks:
        body = re.sub(r"(?m)^(#{2,3}) ", r"##\1 ", b["markdown"])
        out += [f"### {b['heading']}", "", f"出典: <{b['source']}>", "", body, ""]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).rstrip() + "\n"


# --------------------------------------------------------------------------
# Deciding which nodes to rewrite
# --------------------------------------------------------------------------

def reasons_for(entry: dict, node: dict, rendered: str, current: str) -> list[str]:
    """Substantive differences from the source. Empty means notation only, so no rewrite."""
    e, n = entry, node
    reasons: list[str] = []

    er = e["recommendations"]
    if len(n["recs"]) != len(er):
        reasons.append("number of recommendations")
    for a, b in zip(n["recs"], er):
        t = b["label"] or "-"
        if norm(a["text"]) != norm(b["text"]):
            reasons.append(f"recommendation text({t})")
        exp = STRENGTH_JA.get((b["strength"], b["direction"]), "") if b["strength"] != "明確ではない" else ""
        if exp and norm(a["strength"] or "") != norm("推奨の強さ: " + exp)[len("推奨の強さ:"):] \
                and norm(a["strength"] or "") != norm(exp):
            reasons.append(f"recommendation strength({t})")
        if b["evidence"] and (a["evidence"] or "") != b["evidence"]:
            reasons.append(f"evidence({t})")
        want = (b["agreement_rate"] + " " + (b["agreement_total"] or "")).strip() if b["agreement_rate"] else None
        if want and norm(a["rate"] or "") != norm(want):
            reasons.append(f'agreement rate({t}) {a["rate"]}→{want}')
        if not want and a["rate"]:
            reasons.append(f"agreement rate({t}) no vote table in source")
        if b.get("agreement_year") and a.get("year") != b["agreement_year"]:
            reasons.append(f"vote year({t})")

    if nomark(n["question"] or "") != nomark(e["question"]) \
            and not nomark(n["question"] or "").startswith(nomark(e["question"])):
        reasons.append("question text")

    ep = []
    for x in e["explanations"]:
        for p_ in x["markdown"].split("\n\n"):
            p_ = VOTE_SENT.sub("", p_).strip()
            if p_:
                ep.append(p_)
    A, B = nomark("\n".join(n["explanations"])), nomark("\n".join(ep))
    if A != B:
        sm = difflib.SequenceMatcher(None, A, B, autojunk=False)
        sub = 0
        for t_, i1, i2, j1, j2 in sm.get_opcodes():
            if t_ == "equal":
                continue
            a_, b_ = A[i1:i2], B[j1:j2]
            if set(a_) <= TYPO and set(b_) <= TYPO:
                continue
            sub += max(len(a_), len(b_))
        if sub > 0:
            reasons.append(f"explanation body({sub} chars)")

    Am, Bm = norm("\n".join(n["explanations"])), norm("\n".join(ep))
    if A == B and Am != Bm:
        reasons.append("citation numbers")

    nr = [re.sub(r"Epub.*$", "", norm(x["citation"]))[:110] for x in n["refs"]]
    er_ = [re.sub(r"Epub.*$", "", norm(x["citation"]))[:110] for x in e["references"]]
    if nr != er_:
        if sorted(nr) == sorted(er_):
            reasons.append("reference order")
        else:
            reasons.append(f"references({len(nr)}→{len(er_)})")

    # Whether notes (parentheses at the end of the question / `注:` lines) and title match the 2025 source
    def _lines(t: str) -> list[str]:
        return [l for l in t.split("\n")
                if l.startswith("**質問**") or l.startswith("注:") or l.startswith("title:")]
    if _lines(current) != _lines(rendered):
        reasons.append("question/notes/title")
    return reasons


# --------------------------------------------------------------------------

def domains(entries_dir: Path) -> list[str]:
    return sorted(p.name for p in entries_dir.iterdir()
                  if p.is_dir() and re.match(r"(肺癌|中皮腫|胸腺腫瘍)2025_", p.name))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--entries", default=str(ENTRIES_DIR), help="directory of intermediate JSON (default: cache/extracts)")
    ap.add_argument("--data", default=str(DATA_DIR), help="directory of delivery nodes (default: data/doc/診療ガイドライン)")
    ap.add_argument("--out", default=None, help="output directory (default: rewrite --data in place)")
    ap.add_argument("--retrieved-at", default="2026-09-12", help="retrieved_at for the front matter")
    ap.add_argument("--only", default=None, help="restrict to one domain")
    ap.add_argument("--list", action="store_true", help="only print the nodes to rewrite and why; write nothing")
    args = ap.parse_args()

    ent_dir, data_dir = Path(args.entries), Path(args.data)
    out_dir = Path(args.out) if args.out else data_dir
    doms = [d for d in domains(ent_dir) if not args.only or d == args.only]
    if not doms:
        raise SystemExit(f"entries not found: {ent_dir}")

    rewritten, cosmetic, unchanged, skipped = [], [], [], []
    regimen_nodes: list[str] = []
    counter: Counter = Counter()
    for dom in doms:
        src_label = source_label(data_dir / dom / "_index.md")
        if out_dir != data_dir:
            (out_dir / dom).mkdir(parents=True, exist_ok=True)
            idx = data_dir / dom / "_index.md"
            if idx.exists():
                shutil.copy2(idx, out_dir / dom / "_index.md")
        for f in sorted((ent_dir / dom).glob("CQ*.json")):
            cq = f.stem
            nid = f"{dom}/{cq}"
            entry = json.loads(f.read_text(encoding="utf-8"))
            cur_path = data_dir / dom / f"{cq}.md"
            node = parse_node(cur_path)
            current = node["raw"]
            new = render(entry, node, args.retrieved_at, nid, src_label)
            if nid in SKIP:
                # Node not regenerated from the source. Only align retrieved_at
                text = re.sub(r"^retrieved_at: .*$", f"retrieved_at: {yq(args.retrieved_at)}",
                              current, count=1, flags=re.M)
                skipped.append(nid)
            else:
                text = new
                rs = reasons_for(entry, node, new, current)
                if rs:
                    rewritten.append((nid, rs))
                    for r in rs:
                        counter[re.sub(r"\(.*\)", "", r)] += 1
                elif text != current:
                    cosmetic.append(nid)
                else:
                    unchanged.append(nid)
            if not args.list:
                (out_dir / dom / f"{cq}.md").write_text(text, encoding="utf-8")
        # End-of-section regimens (not CQs, so gathered into one node directly under the section)
        # Section _index.md: align retrieved_at and rebuild the regimen block
        idx = out_dir / dom / "_index.md"
        if idx.exists() and not args.list:
            t = idx.read_text(encoding="utf-8")
            t2 = re.sub(r"^retrieved_at: .*$", f"retrieved_at: {yq(args.retrieved_at)}", t, count=1, flags=re.M)
            reg_path = ent_dir / dom / "REGIMEN.json"
            head = t2.split("\n" + REGIMEN_SECTION + "\n", 1)[0].rstrip() + "\n"
            if reg_path.exists():
                blocks = json.loads(reg_path.read_text(encoding="utf-8"))
                t2 = head + "\n" + render_regimen_section(blocks)
                regimen_nodes += [f"{dom}/{b['heading']}" for b in blocks]
            else:
                t2 = head
            if t2 != t:
                idx.write_text(t2, encoding="utf-8")

    total = len(rewritten) + len(cosmetic) + len(unchanged) + len(skipped)
    print(f"{total} CQ: substantive {len(rewritten)} / notation only {len(cosmetic)} / "
          f"unchanged {len(unchanged)} / not regenerated {len(skipped)}"
          + (f" + regimen {len(regimen_nodes)} sections" if regimen_nodes else ""))
    for k, v in counter.most_common():
        print(f"  {v:4d} {k}")
    print()
    for nid, rs in rewritten:
        print("  ", nid, "|", ", ".join(rs))
    if args.list:
        print("\n(--list: nothing written)", file=sys.stderr)


if __name__ == "__main__":
    main()
