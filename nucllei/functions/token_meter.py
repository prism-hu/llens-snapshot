"""
title: Token Meter
description: nucllei #57 が永続化した実測 usage(Chat.meta['usage'])を読んで、context 逼迫時に警告を注入する Filter。
"""

# =============================================================================
# Mechanism / constraints
# -----------------------------------------------------------------------------
# Warning injection only (display retired on 2026-08-09 — nucllei #102):
#     The always-on usage display is now native to nucllei (the occupancy
#     gauge right of the input box + measured values per message). The only job left for this Filter is
#     "insert a system message at the start of the turn that crossed a threshold". status is not used.
#
# Accounting (no own capture / accumulation — nucllei #57 is the source of truth):
#     nucllei's record_usage_by_id_and_message_id (#57) persists SGLang's measured usage
#     to Chat.meta['usage']. True context size = the latest prompt_tokens
#     (what actually went into the model, after system, file injection and filter processing). This Filter
#     only reads that value.
#
# Overflow detection:
#     Without inflating numbers, use the exact prompt_tokens as the numerator and pull the warning injection
#     threshold forward (allowing headroom for generation).
#
# state:
#     self.chat_state[key] = {"warned": set[float]} only (record of fired thresholds).
#     No own token accumulation (#57 holds it). Singleton, but key=chat_id so other chats are not polluted.
# =============================================================================

import logging
from typing import Optional

from pydantic import BaseModel, Field

from nucllei.models.chats import Chats

logger = logging.getLogger(__name__)


def _fmt_num(n: int) -> str:
    # 1024-based. So that 262144 (= 256 * 1024) displays as "256.0k".
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f}M"
    if n >= 1024:
        return f"{n / 1024:.1f}k"
    return str(int(n))


def _chat_key(chat_id: str) -> str:
    return f"chat:{chat_id}"


def _read_usage(chat_id: str) -> Optional[dict]:
    """Read the measured usage that nucllei #57 persisted to Chat.meta['usage'].
    At turn start (inlet) it holds the cumulative total up to the previous turn."""
    chat = Chats.get_chat_by_id(chat_id)
    return ((chat.meta if chat else None) or {}).get("usage") or None


class WarnLevel(BaseModel):
    pct: float = Field(description="閾値 (%)。この%を初めて超えたターンに message を 1 度注入")
    message: str = Field(
        default="",
        description="注入する指示 (「この会話の使用量 N% (残り約 M トークン)。次の手: 」の後ろに付く)。空文字なら注入せず通過のみ",
    )


# Default thresholds are placed early to catch overflow ahead of time (allowing headroom for generation).
_DEFAULT_LEVELS: list[WarnLevel] = [
    WarnLevel(pct=50.0),
    WarnLevel(
        pct=70.0,
        message=(
            "前提を繰り返さず、回答を短く保つ。"
            "新しい長い作業はここで始めず、この回答で結論・途中の状態・コードの差分を書き出しておく。"
        ),
    ),
    WarnLevel(
        pct=85.0,
        message=(
            "上限が近いので、この回答で確定した内容を書き切る。"
            "長くなりそうなら、続きは新しいチャットで始める方がよいとユーザーに伝える。"
        ),
    ),
]


class Filter:
    class Valves(BaseModel):
        priority: int = Field(default=100, description="他 filter より後に動かす")
        # usage.context_size from #57 is the source of truth. Only a fallback for models without it.
        fallback_context_size: int = Field(
            default=262144,
            description="usage.context_size が取れない場合のフォールバック (Kimi K2.6 = 262144)",
        )
        warn_levels: list[WarnLevel] = Field(
            default_factory=lambda: list(_DEFAULT_LEVELS),
            description=(
                "context 使用率(= prompt_tokens / context_size)の閾値とメッセージのペア。"
                "昇順に評価し、同ターンで複数跨いだら最高位だけ発火 (低位は済み扱い)。"
                "各 pct を下回れば個別リセットされ次回超過で再警告。空配列で機能無効。"
            ),
        )

    def __init__(self):
        self.valves = self.Valves()
        # chat_id → {"warned": set[float]}. Holds only fired thresholds (tokens: #57 is the source of truth).
        # The filter is a singleton, so this grows with the number of chats (one entry is just a set of thresholds. Lost on restart,
        # in which case the same threshold warns once more — harmless, so no cleanup).
        self.chat_state: dict[str, dict] = {}

    def _warned(self, key: str) -> set:
        st = self.chat_state.setdefault(key, {"warned": set()})
        return st["warned"]

    def _ctx_size(self, usage: Optional[dict]) -> int:
        ctx = (usage or {}).get("context_size")
        return ctx if isinstance(ctx, int) and ctx > 0 else self.valves.fallback_context_size

    def _maybe_inject_warning(self, body: dict, prompt: int, context_size: int, warned: set) -> None:
        levels = [lvl for lvl in (self.valves.warn_levels or []) if lvl.pct > 0]
        if not levels or context_size <= 0:
            return
        pct = prompt / context_size * 100.0

        # Thresholds dropped below are removed from warned so they can warn again on the next crossing
        warned.intersection_update({t for t in warned if pct >= t})

        # Fire only the single highest "exceeded & unwarned & has message" level, evaluated in ascending order.
        sorted_levels = sorted(levels, key=lambda lvl: lvl.pct)
        fired: Optional[WarnLevel] = None
        for lvl in reversed(sorted_levels):
            if pct >= lvl.pct and lvl.pct not in warned and lvl.message:
                fired = lvl
                break
        if fired is None:
            return

        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            return

        remaining = max(0, context_size - prompt)
        warning = (
            f"[システムからの注記] この会話の使用量 {pct:.0f}% "
            f"(残り約 {_fmt_num(remaining)} トークン)。"
            f"次の手: {fired.message}"
        )
        # Insert right before the latest user message (keeps the prefix intact and preserves the prompt cache).
        insert_pos = len(messages) - 1
        if messages[insert_pos].get("role") != "user":
            insert_pos = len(messages)
        messages.insert(insert_pos, {"role": "system", "content": warning})
        body["messages"] = messages

        # A higher-level firing subsumes the lower-level notifications of the same turn.
        warned.update({lvl.pct for lvl in sorted_levels if lvl.pct <= fired.pct})
        logger.info(
            f"[TokenMeter] WARN injected pct={pct:.0f} fired={fired.pct:.0f}"
        )

    # --------------------------------------------------------
    # inlet: read #57 usage up to the previous turn and inject a warning if a threshold is exceeded
    # --------------------------------------------------------
    async def inlet(
        self,
        body: dict,
        __metadata__: Optional[dict] = None,
        __user__: Optional[dict] = None,
    ) -> dict:
        chat_id = (__metadata__ or {}).get("chat_id")
        if not chat_id:
            return body
        # If inlet raises, nucllei stops the whole chat. The warning is an add-on, so
        # if it cannot be read, pass through without a warning (fail-open. Same as large_file_gate).
        try:
            usage = _read_usage(chat_id)
            prompt = int((usage or {}).get("prompt_tokens") or 0)
            context_size = self._ctx_size(usage)
            # Judge thresholds by the previous turn's occupancy → inject the warning ahead (effective before this turn grows)
            if prompt > 0:
                self._maybe_inject_warning(body, prompt, context_size, self._warned(_chat_key(chat_id)))
        except Exception:  # noqa: BLE001 - fail-open (comment above). traceback goes to the log
            logger.exception("[TokenMeter] inlet failed → passing through without a warning (fail-open)")
        return body
