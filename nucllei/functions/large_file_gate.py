"""
title: Large File Gate
description: 巨大な添付の抽出テキストを context に入れず file_id で扱わせる inlet Filter。
"""

# =============================================================================
# Mechanism / constraints
# -----------------------------------------------------------------------------
# nucllei port of owui/filters/large_file_gate.py 0.4.0.
#
# nucllei injects the extracted text of attachments **verbatim in full** into the system
# <source> block, with no summarization or chunking (README_API §4.3 "full text — there is no RAG
# retrieval step"). A huge file alone eats up the context.
#
# This filter counts the tokens of each attachment's extracted text with tiktoken in inlet, and **drops** only the
# files exceeding the threshold (default 100k tokens) from body['files'] / metadata['files']
# (= that body is not injected / all-or-nothing. No truncation). Instead it injects one system
# instruction stating the file_id and how to handle it. The body is gone but the file remains in Files, so
# the model can recover it from the file_id via sandbox tools (send_to_workspace → run_python) and
# process it without putting the content into the context.
#
# In addition, it detects substantial attachments whose extraction is empty (too large, Attachment timeout, etc.)
# and announces "extraction failed / look at the relevant pages with feed_image(file_id, pages=…) and answer"
# (scanned PDFs have no text layer and run_python would come up empty too). However,
# audio / image / video are intentionally passed through unextracted by Attachment by design, so they are not treated as failed
# (to avoid misrouting to the sandbox path — the correct routes are transcribe_speech / feed_image).
#
# Furthermore, formats outside Attachment's extraction scope (legacy Office .doc/.xls/.ppt, RTF, Access, archives, etc.)
# arrive with no body as <source extraction="none">, and there was an incident where the model mistook "empty extraction = huge"
# and entered the large-file-handling skill (2026-10-02). This is separated as a third
# reason `unsupported`, pointing to the route send_to_workspace → convert in the sandbox and read.
# A small .doc has no content all the same, so the raw size lower bound (min_failed_raw_bytes) is
# not applied.
#
# Differences from the owui version:
#  1) The skill trigger mechanism differs. OWUI's `<$id|label>` mention (auto-load) does not exist in nucllei,
#     so the note makes the model call load_skill (the model route in README_API §6.3).
#     Prerequisite: the target skill is in the preset's meta.skillIds.
#  2) Files.get_file_by_id is synchronous (it was async in owui v0.9.5).
#  3) Extraction is done by markitdown_extract (Attachment).
#  4) pdf_vision (auto-injection of all page images) has **retired** (2026-09-04, functions/retired/).
#     The former "PDFs already imaged by pdf_vision are not treated as failed" check (parsing the `pdf-vision`
#     note) was removed on 2026-08-16 — scanned PDFs are treated as failed and routed to feed_image.
#     To revive it, either restore that check or have pdf_vision drop the file.
#
# Memo: setting file_handler = True on the module/instance makes nucllei drop all files unconditionally
# (skip_files in utils/filter.py). It cannot discriminate by size, so it is not used; instead the inlet
# thins the list.
# =============================================================================

import logging
import os
from typing import Any, Awaitable, Callable, Optional

import tiktoken
from pydantic import BaseModel, Field

from nucllei.models.files import Files

logger = logging.getLogger(__name__)

# Tokens are counted with tiktoken (valve encoding_name, default o200k_base). From v20261005-01 the nucllei image
# bakes tiktoken 0.14.0 and o200k_base / cl100k_base into
# TIKTOKEN_CACHE_DIR=/app/tiktoken, so they can be read in the air-gapped network without fetching.
# On earlier images the import fails and this function fails to load (RELEASE.md).
#
# Until 2026-10-04 the image had no tiktoken and it was always the "chars ÷ 2" estimate. The meaning of max_tokens
# changed to "actual token count", so the same 100000 drops files of a different size (measured chars/token
# for o200k_base, 2026-10-05):
#   - Japanese (in-house documents, guidelines, reimbursement md): 1.3–1.8, roughly 1.5
#     → old ~200k chars → new ~130–180k chars (roughly 150k). Japanese is dropped earlier than before
#   - Numeric CSV: 1.7 → ~170k chars. Japanese CSV: 1.3 → ~130k chars
#   - English prose: 4.3 → ~430k chars. English is dropped later (at a larger size) than before
# When the encoding cannot be read, do not fall back to the estimate; log and raise (the inlet's fail-open
# catches it, passes the body through as-is and notifies the user with a toast). Estimating silently would revert to
# "estimating while believing it is measured" and the threshold's meaning would become unclear again.


# A copy of TEXT_EXTENSIONS in markitdown_extract.py (Attachment), same order. Functions cannot import
# each other at runtime, so it is held as a copy — when changing the other side, always align this one too.
# Extensions not in this list and not media = formats outside extraction scope, announced as unsupported.
_TEXT_EXTENSIONS = (
    ".pdf",
    ".docx",
    ".xlsx",
    ".pptx",
    ".csv",
    ".tsv",
    ".html",
    ".htm",
    ".md",
    ".txt",
    ".json",
    ".xml",
)


def _fmt_bytes(n: Optional[int]) -> str:
    if not n:
        return "?"
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f}MB"
    if n >= 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n}B"


def _iter_file_items(body: dict):
    """Enumerate attachment references in body['files'] and body['metadata']['files'] as (id, name)."""
    seen: set[str] = set()
    sources = [
        body.get("files"),
        (body.get("metadata") or {}).get("files"),
    ]
    for src in sources:
        if not isinstance(src, list):
            continue
        for f in src:
            if not isinstance(f, dict):
                continue
            inner = f.get("file") or {}
            fid = f.get("id") or inner.get("id")
            if not fid or fid in seen:
                continue
            seen.add(fid)
            name = (
                inner.get("filename")
                or f.get("name")
                or (inner.get("meta") or {}).get("name")
                or fid
            )
            yield fid, name


def _remove_ids(body: dict, ids: set[str]) -> None:
    """Remove attachments with the given ids from body['files'] / metadata['files']."""

    def _filtered(lst):
        out = []
        for f in lst:
            if isinstance(f, dict):
                fid = f.get("id") or (f.get("file") or {}).get("id")
                if fid in ids:
                    continue
            out.append(f)
        return out

    if isinstance(body.get("files"), list):
        body["files"] = _filtered(body["files"])
    md = body.get("metadata")
    if isinstance(md, dict) and isinstance(md.get("files"), list):
        md["files"] = _filtered(md["files"])


class Filter:
    class Valves(BaseModel):
        priority: int = Field(
            default=50,
            description="token_meter(100) の前で動かす",
        )
        max_tokens: int = Field(
            default=100000,
            description=(
                "抽出テキストがこの token 数 (encoding_name で数えた実数) を超えたら本文を "
                "context に入れず間引く。256k context のうち単一ファイルに許容する上限の目安。"
                "o200k_base で 100000 は日本語で約 15 万字、英語で約 43 万字"
            ),
        )
        min_failed_raw_bytes: int = Field(
            default=100 * 1024,
            description=(
                "raw がこのサイズ以上なのに抽出が空なら『抽出失敗』とみなして告知する。"
                "小さすぎる空ファイル (中身の無い docx 等) を誤検知しないための下限。"
            ),
        )
        encoding_name: str = Field(
            default="o200k_base",
            description=(
                "token を数える tiktoken エンコーディング名。nucllei の image に焼いてあるのは "
                "o200k_base と cl100k_base (それ以外は閉域では取得できず判定が失敗する)"
            ),
        )
        skill_id: str = Field(
            default="large-file-handling",
            description=(
                "間引き発火時に note でロードを促す skill id。モデルが load_skill で読む。"
                "preset の meta.skillIds に入っていないと発火しない。空文字で言及しない。"
            ),
        )

    def __init__(self) -> None:
        self.valves = self.Valves()
        # (encoding name, Encoding). self.valves is swapped when the valve is re-saved, so
        # hold the name along with it and reuse only when it matches.
        self._enc: Optional[tuple[str, Any]] = None

    # ------------------------------------------------------------------
    def _encoding(self):
        name = self.valves.encoding_name
        if self._enc is None or self._enc[0] != name:
            try:
                self._enc = (name, tiktoken.get_encoding(name))
            except Exception as e:
                logger.error(
                    f"[large-file-gate] cannot read tiktoken encoding {name!r} "
                    f"(TIKTOKEN_CACHE_DIR={os.environ.get('TIKTOKEN_CACHE_DIR')!r}): {e!r}"
                )
                raise RuntimeError(
                    f"large_file_gate: cannot load tiktoken encoding {name!r}. "
                    f"Check valve encoding_name and the nucllei image's TIKTOKEN_CACHE_DIR"
                ) from e
        return self._enc[1]

    def _count_tokens(self, text: str) -> int:
        if not text:
            return 0
        # Even if `<|endoftext|>` etc. is mixed into the extracted text, do not raise by treating it as a special token
        return len(self._encoding().encode(text, disallowed_special=()))

    def _build_note(self, excluded: list[dict]) -> str:
        """Build the action instruction for dropped (oversize) / extraction-failed (failed) / unsupported files
        (no body is emitted). oversize goes through the skill procedure, failed to feed_image,
        unsupported to the route send_to_workspace → convert in the sandbox.
        nucllei has no mention auto-load, so the model is made to call load_skill.
        The form follows nucllei's convention (README_API §5.1): what happened (fact) first, the instruction
        to the model after `次の手:`. Tools from other toolkits are named conditionally, only when available."""
        oversize = [e for e in excluded if e["reason"] == "oversize"]
        failed = [e for e in excluded if e["reason"] == "failed"]
        unsupported = [e for e in excluded if e["reason"] == "unsupported"]

        lines = ["[システムからの注記 (ユーザーの発言ではない)]"]

        if oversize:
            lines.append(
                "以下の添付は大きすぎるため、本文をこの会話に読み込んでいない:"
            )
            for e in oversize:
                lines.append(
                    f"- {e['name']} (file_id={e['id']}, {e['content_type'] or '?'}, "
                    f"抽出 約{e['tokens']} トークン, 元 {_fmt_bytes(e['size'])})"
                )
            if self.valves.skill_id:
                lines.append(
                    f"次の手: 本文を自分で全部読み込まない。作業を始める前に "
                    f"load_skill で `{self.valves.skill_id}` をロードし、その手順に従って "
                    f"file_id から処理する。"
                )
            else:
                lines.append(
                    "次の手: 本文を自分で全部読み込まない。file_id から処理する。"
                )

        if failed:
            lines.append(
                "以下の添付は抽出結果が空だった (テキスト層が無い PDF (スキャン等) の可能性が高い)。"
                "本文は読み込めていない:"
            )
            for e in failed:
                lines.append(
                    f"- {e['name']} (file_id={e['id']}, {e['content_type'] or '?'}, "
                    f"元 {_fmt_bytes(e['size'])})"
                )
            lines.append(
                "次の手: 内容が必要なら、PDF は `feed_image(file_id=…, pages=…)` で該当ページを"
                "見て答える。PDF でなければ `send_to_workspace(file_id=…)` で sandbox に取り込み、"
                "原本を読む (どちらもその tool が使えるときだけ。使えなければ読めなかったと伝える)。"
            )

        if unsupported:
            lines.append(
                "以下の添付はテキスト抽出の対象外の形式 (旧 Office .doc/.xls/.ppt、RTF、"
                "Access、圧縮等) で、本文は読み込めていない:"
            )
            for e in unsupported:
                lines.append(
                    f"- {e['name']} (file_id={e['id']}, {e['content_type'] or '?'}, "
                    f"元 {_fmt_bytes(e['size'])})"
                )
            lines.append(
                "次の手: `send_to_workspace` が使えるなら `send_to_workspace(file_id=…)` で sandbox に"
                "取り込み、load_skill で `file-conversion` をロードしてその手順で変換して読む "
                "(.msg / .dbf / .sav やテキスト系の扱いも同じ skill にある)。"
                "large-file-handling skill の手順は不要。"
            )

        return "\n".join(lines)

    def _inject_note(self, body: dict, note: str) -> None:
        """Insert the system instruction right before the latest user message (without breaking the upstream prefix)."""
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            return
        insert_pos = len(messages) - 1
        if messages[insert_pos].get("role") != "user":
            insert_pos = len(messages)
        messages.insert(insert_pos, {"role": "system", "content": note})
        body["messages"] = messages

    # ------------------------------------------------------------------
    async def inlet(
        self,
        body: dict,
        __event_emitter__: Optional[Callable[[Any], Awaitable[None]]] = None,
        __metadata__: Optional[dict] = None,
        __user__: Optional[dict] = None,
    ) -> dict:
        # If inlet raises, nucllei re-raises and breaks the whole chat.
        # Whatever happens, pass the body through as-is (fail-open). At worst "the huge body goes in as-is" =
        # back to the original behavior; the chat is not stopped.
        try:
            return await self._gate(body)
        except Exception:  # noqa: BLE001 - fail-open (comment above). traceback goes to the log
            logger.exception("[large-file-gate] inlet failed → passing through untouched (fail-open)")
            # If fail-open only shows in the log, the cause cannot be traced when "the huge body goes in as-is and
            # the inference layer exceeds the context" happens afterwards. Tell the user one line
            # (swallowed further so a failure of the notification itself does not break the fail-open).
            # status is not used (removed entirely from llens on 2026-08-09) — a transient warning, so
            # it goes out as a toast (notification event).
            if __event_emitter__:
                try:
                    await __event_emitter__({
                        "type": "notification",
                        "data": {
                            "type": "warning",
                            "content": (
                                "添付のサイズ判定に失敗したため、本文をそのまま渡しました "
                                "(大きすぎる場合は応答が失敗することがあります)"
                            ),
                        },
                    })
                except Exception as e:  # noqa: BLE001 - do not let a notification failure break the fail-open
                    logger.warning(f"[large-file-gate] fail-open notification failed: {e!r}")
            return body

    async def _gate(self, body: dict) -> dict:
        candidates = list(_iter_file_items(body))
        if not candidates:
            return body

        excluded: list[dict] = []
        for fid, name in candidates:
            try:
                rec = Files.get_file_by_id(fid)  # synchronous in nucllei
            except Exception as e:  # noqa: BLE001 - skip judging just that file and pass it through (fail-open)
                logger.error(f"[large-file-gate] {name}: get_file_by_id failed → passing through unjudged: {e!r}")
                continue
            if not rec:
                continue
            extracted = (getattr(rec, "data", None) or {}).get("content") or ""
            tokens = self._count_tokens(extracted)
            meta = getattr(rec, "meta", None) or {}
            size = meta.get("size") or 0

            ct = (meta.get("content_type") or "").lower()
            # Audio / image / video are intentionally excluded from extraction by Attachment (markitdown) by design
            # and pass through unextracted, so they are not "extraction failed". Treating them as failed here
            # misroutes to the sandbox-based "re-read with run_python" (the correct routes are actually
            # transcribe_speech for audio and feed_image for images).
            # nucllei tells the model about unextracted files via <source extraction="none"> + file_id, and
            # each tool's description shows the route, so the gate passes them silently.
            is_media = ct.startswith(("audio/", "image/", "video/"))
            # Formats Attachment rejected by extension (= never even attempted extraction). Legacy Office, RTF,
            # Access, archives, etc. Files without an extension fall here too (Attachment rejects them likewise).
            # The check uses the file record's filename, same as Attachment (the name on the body side is
            # a display name and falls back to file_id when unavailable).
            fname = str(getattr(rec, "filename", None) or name).lower()
            is_unsupported = not is_media and not fname.endswith(_TEXT_EXTENSIONS)

            reason = None
            if tokens > self.valves.max_tokens:
                reason = "oversize"
            elif not extracted.strip() and is_unsupported:
                # Having no content is the same even when small, so the raw size lower bound is not consulted.
                reason = "unsupported"
            elif (
                not extracted.strip()
                and not is_media
                and size >= self.valves.min_failed_raw_bytes
            ):
                # raw is large but extraction is empty = Attachment incomplete (timeout etc.) or
                # a scanned PDF without a text layer. Either way the note routes to feed_image (look with eyes).
                reason = "failed"
            if reason is None:
                continue

            excluded.append(
                {
                    "id": fid,
                    "name": name,
                    "tokens": tokens,
                    "content_type": ct or meta.get("content_type"),
                    "size": meta.get("size"),
                    "reason": reason,
                }
            )

        if not excluded:
            return body

        # Only oversize is removed from the context (the goal is to remove huge bodies). failed / unsupported (empty extraction)
        # already have an empty body and do no harm; rather, keeping them in body['files'] lets send_to_workspace
        # pick them up (if the gate removes them they vanish from the tool's __files__ too and cannot be recovered with run_python).
        _remove_ids(body, {e["id"] for e in excluded if e["reason"] == "oversize"})
        self._inject_note(body, self._build_note(excluded))

        summary = ", ".join(f"{e['name']}({e['tokens']}tok)" for e in excluded)
        # Removing attachments from the context / adding the note changes the conversation's content, so keep it as a warning
        logger.warning(f"[large-file-gate] note for {len(excluded)} file(s): {summary}")

        return body
