"""
title: MarkItDown Extract
description: アップロード文書を markitdown で Markdown 抽出する Attachment Function。
"""

# =============================================================================
# Positioning / constraints
# -----------------------------------------------------------------------------
# PDFs: only the text layer is extracted; effectively text-less ones (scanned PDFs) pass through as None = unextracted
# — nucllei tells the model via <source extraction="none"> + file_id, and
# the model looks at the relevant pages itself with feed_image ("reading materials" in the prompt).
# The pdf_vision filter (auto-injection of all page images) has retired (2026-09-04, functions/retired/).
# This is the only Attachment. Enabled by nucllei's ATTACHMENT_FUNCTION_ID=markitdown_extract
# (docker-compose.yml. Swapping is just re-pointing the env).
# NOTE: on 2026-08-05 docling_extract (the heavyweight standby for when layout / tables are needed) and the docling
#    service were removed together — it sat in compose without ever becoming the production extraction route.
# =============================================================================

import logging

from markitdown import MarkItDown  # bundled in the nucllei image
from pydantic import BaseModel, Field

from nucllei.storage.provider import Storage

logger = logging.getLogger(__name__)

# Only document types markitdown can turn into md. Images / audio / video and unknown extensions pass through
# as None (unextracted) and are left to file_id-based tool processing (the sandbox's send_to_workspace etc.).
# NOTE: large_file_gate.py holds a copy with the same order (_TEXT_EXTENSIONS) — used to judge
#   formats outside scope (unsupported). Functions cannot import each other at runtime,
#   so when changing this, align the gate side too.
TEXT_EXTENSIONS = (
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


class Attachment:
    class Valves(BaseModel):
        pdf_text_char_threshold: int = Field(
            default=100,
            description=(
                "PDF 抽出テキストの総文字数がこれ未満ならテキストレイヤ無しとみなして"
                "未抽出 (None) にする。スキャン PDF はモデルが feed_image で見る (pdf_vision は退役)"
            ),
        )

    def __init__(self):
        self.valves = self.Valves()

    def process(self, file, __event__: str = "upload") -> str | None:
        # If already extracted on re-attach (attach), keep it as-is (no re-extraction)
        if __event__ == "attach" and (file.data or {}).get("content"):
            return None

        filename = (file.filename or "").lower()
        if not filename.endswith(TEXT_EXTENSIONS):
            return None

        local_path = Storage.get_file(file.path)
        text = (MarkItDown().convert(local_path).text_content or "").strip()

        if filename.endswith(".pdf") and len(text) < self.valves.pdf_text_char_threshold:
            logger.info(
                f"[attachment-extract] {file.filename}: judged as having no text layer "
                f"(chars={len(text)}) → left unextracted (the model reads it with feed_image)"
            )
            return None

        if not text:
            return None  # an empty extraction passes through as unextracted, not as "extracted but empty"

        return text
