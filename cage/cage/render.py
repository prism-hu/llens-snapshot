"""HTML rendering of doc (for browsers). Called per request by cage's public route `GET /ext/doc/<addr>`.

Input is a node of the tree walked from the build artifacts (md with placeholders substituted
and the setext workaround applied by build.py). Only rendering happens here; nothing is written to files.

- breadcrumbs (root → parent) + h1 of the title + body (+ child listing for nodes with children)
- front matter is not shown in the body (the body stripped by walker is rendered). title goes to `<title>` and h1
- heading ids match the headings in the read_doc body (`heading_ids`; same rule as nucllei).
  The title h1 is excluded from numbering (has no id)
- numbered headings also get alias anchors (`heading_aliases`; so links with a miscopied number still land).
  Headings containing `**`, `<sup>` etc. also get the raw markdown text as an alias (so links that copied the raw text still land)
- no scripts, inline CSS (responses carry a script-forbidding CSP; server.py)
- raw HTML is escaped (only attribute-less <br> / <sup> / <sub> / <u> pass through),
  href / src keep only allowed schemes
"""

from __future__ import annotations

import html
import re
import unicodedata
from collections import Counter
from urllib.parse import quote

from markdown_it import MarkdownIt

from .walker import Node, node_addr

# Raw HTML is escaped by default. Only attribute-less <br> / <sup> / <sub> / <u> pass through
# (the corpus has 17k in-cell line breaks as <br> in tables; escaping them makes tables unreadable.
# Only forms that cannot carry attributes pass, so script or on* attributes cannot get in).
# HTML comments are not displayed.
_ALLOWED_TAG_RE = re.compile(r"&lt;(/?)(br|sup|sub|u)\s*/?&gt;", re.IGNORECASE)
_COMMENT_RE = re.compile(r"&lt;!--.*?--&gt;", re.DOTALL)


def _sanitize_raw_html(content: str) -> str:
    s = html.escape(content, quote=False)
    s = _COMMENT_RE.sub("", s)
    return _ALLOWED_TAG_RE.sub(lambda m: f"<{m.group(1)}{m.group(2).lower()}>", s)


# Schemes allowed in href / src (same rule as nucllei). Control chars and whitespace are stripped before checking.
_URL_JUNK_RE = re.compile(r"[\x00-\x20\x7f]")
_SCHEME_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.-]*):")


def _url_ok(url: str, *, image: bool) -> bool:
    u = _URL_JUNK_RE.sub("", url)
    m = _SCHEME_RE.match(u)
    if m is None:
        return True  # relative / `#` / `/…`
    scheme = m.group(1).lower()
    if scheme in ("http", "https", "mailto"):
        return True
    return image and u.lower().startswith("data:image/")


def make_md() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"html": True}).enable(["table", "strikethrough"])
    # setext headings are already neutralized by isolate_thematic_breaks in the built md,
    # but disable them on the parser side too just in case (`---` stays hr, `===` stays text).
    md.disable("lheading")
    # URL checks are done on the render side (_url_ok), separately for images and links.
    md.validateLink = lambda url: True

    # add_render_rule binds the function as a renderer method (first arg is self)
    def _html(self, tokens, idx, options, env):
        return _sanitize_raw_html(tokens[idx].content)

    md.add_render_rule("html_inline", _html)
    md.add_render_rule("html_block", _html)

    default_image = md.renderer.rules["image"]

    def _link_open(self, tokens, idx, options, env):
        tok = tokens[idx]
        href = tok.attrGet("href") or ""
        if not _url_ok(href, image=False):
            tok.attrs.pop("href", None)
        return self.renderToken(tokens, idx, options, env)

    def _image(self, tokens, idx, options, env):
        tok = tokens[idx]
        src = tok.attrGet("src") or ""
        if not _url_ok(src, image=True):
            tok.attrSet("src", "")
        return default_image(tokens, idx, options, env)

    def _heading_open(self, tokens, idx, options, env):
        # Aliases (heading_aliases) are placed as empty anchors right before the heading. The heading's own id is unchanged
        anchors = "".join(
            f'<a class="al" id="{html.escape(a)}"></a>' for a in tokens[idx].meta.get("aliases", ())
        )
        return anchors + self.renderToken(tokens, idx, options, env)

    md.add_render_rule("link_open", _link_open)
    md.add_render_rule("heading_open", _heading_open)
    md.add_render_rule("image", _image)
    return md


def _inline_text(tok) -> str:
    """Display text of an inline token = same as itertext of the rendered element.

    Markers like `**` drop out, characters remain. Images carry no text since alt is an attribute.
    For raw HTML, passed-through tags (<br> etc.) and comments carry no text, while escaped
    pieces come out as text (same as the rendered result).
    """
    out: list[str] = []
    for c in tok.children or []:
        if c.type in ("text", "code_inline"):
            out.append(c.content)
        elif c.type in ("softbreak", "hardbreak"):
            out.append("\n")
        elif c.type == "html_inline":
            out.append(html.unescape(re.sub(r"<[^>]*>", "", _sanitize_raw_html(c.content))))
    return "".join(out)


def heading_ids(tokens) -> tuple[list[str], list[str]]:
    """Assign ids to headings (mutates tokens). Returns (assigned ids, duplicated heading texts).

    Rule (same as nucllei): the id is the heading text as is (only surrounding whitespace stripped).
    On duplicates the first keeps it, the 2nd onwards get `<heading>-2`, `-3`… (advancing until
    the number does not collide with an already used id). All heading levels share one namespace.
    """
    used: set[str] = set()
    ids: list[str] = []
    seen: Counter[str] = Counter()
    for i, tok in enumerate(tokens):
        if tok.type != "heading_open":
            continue
        text = _inline_text(tokens[i + 1]).strip()
        seen[text] += 1
        hid = text
        if hid in used:
            n = 2
            while f"{text}-{n}" in used:
                n += 1
            hid = f"{text}-{n}"
        used.add(hid)
        ids.append(hid)
        tok.attrSet("id", hid)
    dups = [t for t, n in seen.items() if n > 1]
    return ids, dups


# Leading number of a heading (ⅩⅠ． / 3. / ４． / 2-3. / Ⅱ-1 / (1) / （１） / ①).
# One unit including the delimiter (．.、 or whitespace). Without a delimiter, "Ⅱ型" / "2024年"
# are not treated as numbers (parenthesized / circled numbers need no delimiter).
_NUM = r"(?:[Ⅰ-Ⅻⅰ-ⅻ]+|[IVX]+|[0-9０-９]+)"
_NUM_PREFIX_RE = re.compile(
    rf"^(?:{_NUM}(?:[.．\-－‐]{_NUM})*(?:[.．、:：][\s　]*|[\s　]+)"
    r"|[(（][0-9０-９]+[)）][\s　]*"
    r"|[①-⑳][\s　]*)"
)


def _strip_number(text: str) -> str:
    return _NUM_PREFIX_RE.sub("", text, count=1).strip()


def heading_aliases(tokens, ids: list[str]) -> list[list[str]]:
    """Decide heading aliases and put them in heading_open's meta["aliases"] (mutates tokens).

    Alias = the text with the leading number removed, and the NFKC-normalized text (each only when it differs).
    Insurance so that an anchor where the model miscopied / dropped the number
    (`#Ⅹ．予防措置と効果` → `ⅩⅠ．予防措置と効果`) still lands within the page.
    In addition, the heading's raw markdown text (the heading line minus `#`, including `**` or `<sup>注6)</sup>`)
    and its number-stripped / NFKC forms become aliases too. The id is the rendered text (markers and tags dropped),
    but the model is told to "copy the heading line minus `#`", so headings with markers/tags arrive as raw text.
    Anything equal to a real id (ids), or shared by 2 or more headings on the page, is discarded
    (no numbering — better not to jump than to jump to a different heading via an alias).
    Returns the alias list per heading (same order as ids).
    """
    heads = [i for i, t in enumerate(tokens) if t.type == "heading_open"]
    cands: list[list[str]] = []
    for hid, i in zip(ids, heads):
        text = _inline_text(tokens[i + 1]).strip()
        raw = (tokens[i + 1].content or "").strip()  # raw markdown text
        cs: list[str] = []
        forms: list[str] = []
        for base in (text, raw):
            stripped = _strip_number(base)
            forms += [base, stripped, unicodedata.normalize("NFKC", base),
                      unicodedata.normalize("NFKC", stripped)]
        for c in forms:
            c = c.strip()
            if c and c != text and c != hid and c not in cs:
                cs.append(c)
        cands.append(cs)
    taken = set(ids)
    count = Counter(c for cs in cands for c in cs)
    out: list[list[str]] = []
    for cs, i in zip(cands, heads):
        keep = [c for c in cs if c not in taken and count[c] == 1]
        if keep:
            tokens[i].meta["aliases"] = keep
        out.append(keep)
    return out


_CSS = """
:root{color-scheme:light dark;--fg:#1f2328;--bg:#ffffff;--mute:#59636e;--line:#d1d9e0;
--link:#0969da;--code:#f6f8fa;--hl:#fff8c5}
@media (prefers-color-scheme:dark){:root{--fg:#e6edf3;--bg:#0d1117;--mute:#9198a1;
--line:#3d444d;--link:#4493f8;--code:#151b23;--hl:#3b2e00}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.7 system-ui,-apple-system,
"Hiragino Sans","Noto Sans JP","Yu Gothic UI",sans-serif;overflow-wrap:anywhere}
main{max-width:52rem;margin:0 auto;padding:16px}
nav.crumbs{font-size:.85rem;color:var(--mute);margin-bottom:1rem}
nav.crumbs a{color:var(--mute)}
a{color:var(--link)}
h1{font-size:1.6rem;line-height:1.35;margin:.2em 0 .8em}
h2{font-size:1.3rem;border-bottom:1px solid var(--line);padding-bottom:.2em;margin-top:1.8em}
h3{font-size:1.1rem;margin-top:1.5em}
h4,h5,h6{font-size:1rem}
:target{background:var(--hl);scroll-margin-top:8px}
a.al:target+:is(h1,h2,h3,h4,h5,h6),a.al:target+a.al+:is(h1,h2,h3,h4,h5,h6),
a.al:target+a.al+a.al+:is(h1,h2,h3,h4,h5,h6){background:var(--hl)}
img{max-width:100%;height:auto}
table{border-collapse:collapse;display:block;max-width:100%;overflow-x:auto;margin:1em 0}
th,td{border:1px solid var(--line);padding:.3em .6em;vertical-align:top}
th{background:var(--code)}
code{background:var(--code);padding:.1em .3em;border-radius:4px;font-size:.9em}
pre{background:var(--code);padding:.8em;overflow-x:auto;border-radius:6px}
pre code{padding:0;background:none}
blockquote{margin:1em 0;padding:0 1em;color:var(--mute);border-left:4px solid var(--line)}
hr{border:0;border-top:1px solid var(--line);margin:1.5em 0}
ul.children{padding-left:1.2em}
ul.children .n{color:var(--mute);font-size:.85em}
""".strip()


def node_href(node: Node) -> str:
    """The node's canonical address (walker.node_addr), percent-encoded for an HTML href.

    Not made relative because page addresses count levels differently for a leaf `X.md`
    and a directory `X/` (a relative link would be off by one level).
    """
    return quote(node_addr(node), safe="/:")


def render_page(
    *,
    title: str,
    body: str,
    crumbs: list[tuple[str, str]],
    children: list[tuple[str, str, int]],
    md: MarkdownIt,
) -> str:
    """HTML of one page. crumbs: (title, href) from root to the parent. children: (title, href, child count)."""
    tokens = md.parse(body)
    # ids are numbered from body headings only (so the headings in the read_doc body and the
    # anchors the model writes agree). The title's <title> / h1 do not take part (the h1 has no id).
    ids, _dups = heading_ids(tokens)
    # Add aliases to numbered headings so anchors with a miscopied number still land
    heading_aliases(tokens, ids)
    # If the body starts with an h1 equal to the title, use the body's h1 (with id) instead of the title h1
    title_h1 = not (
        len(tokens) >= 3
        and tokens[0].type == "heading_open"
        and tokens[0].tag == "h1"
        and _inline_text(tokens[1]).strip() == title.strip()
    )
    body_html = md.renderer.render(tokens, md.options, {})

    esc = html.escape
    parts = [
        "<!doctype html>",
        '<html lang="ja">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f"<title>{esc(title)}</title>",
        f"<style>{_CSS}</style>",
        "</head>",
        "<body>",
        "<main>",
    ]
    if crumbs:
        links = " / ".join(f'<a href="{esc(h)}">{esc(t)}</a>' for t, h in crumbs)
        parts.append(
            f'<nav class="crumbs">{links} / <span aria-current="page">{esc(title)}</span></nav>'
        )
    if title_h1:
        parts.append(f"<h1>{esc(title)}</h1>")
    if body_html:
        parts.append(body_html.rstrip("\n"))
    if children:
        items = []
        for t, h, n in children:
            extra = f' <span class="n">({n})</span>' if n else ""
            items.append(f'<li><a href="{esc(h)}">{esc(t)}</a>{extra}</li>')
        parts.append('<ul class="children">\n' + "\n".join(items) + "\n</ul>")
    parts += ["</main>", "</body>", "</html>", ""]
    return "\n".join(parts)


def render_node(tree: Node, segments: list[str], md: MarkdownIt) -> str:
    """HTML of one node in the tree (addressed by the segment list from root; the caller has verified it exists).

    The listing puts nodes with children first, each group in name (segment) order.
    """
    crumbs: list[tuple[str, str]] = []
    node = tree
    for seg in segments:
        crumbs.append((node.title, node_href(node)))
        node = node.children[seg]
    kids = sorted(node.children.items(), key=lambda kv: (0 if kv[1].children else 1, kv[0]))
    children = [(c.title, node_href(c), len(c.children)) for _k, c in kids]
    return render_page(title=node.title, body=node.body, crumbs=crumbs, children=children, md=md)


def not_found_page(message: str) -> str:
    esc = html.escape
    return (
        '<!doctype html>\n<html lang="ja">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        f"<title>見つかりません</title>\n<style>{_CSS}</style>\n</head>\n<body>\n<main>\n"
        f"<h1>見つかりません</h1>\n<p>{esc(message)}</p>\n</main>\n</body>\n</html>\n"
    )
