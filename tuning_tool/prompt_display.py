"""用于统一展示 system/user 文本块，避免多处重复拼接。"""

from __future__ import annotations


def format_prompt_messages(*, system: str, user: str) -> str:
    lines = [
        "----- BEGIN role=system -----",
        system,
        "----- END role=system -----",
        "",
        "----- BEGIN role=user -----",
        user,
        "----- END role=user -----",
    ]
    return "\n".join(lines)


__all__ = ["format_prompt_messages"]
