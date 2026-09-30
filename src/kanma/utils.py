"""通用工具：diff 文本渲染与分块（控制送模型的上下文规模）。"""

from __future__ import annotations

from kanma.models.schemas import DiffBundle, FileDiff

DEFAULT_CHUNK_CHARS = 12000


def render_file_diff(fd: FileDiff, with_line_no: bool = True) -> str:
    """把单文件 diff 渲染为便于模型定位的文本（带新文件行号）。"""
    out = [f"### 文件：{fd.path}（{fd.change_type}，+{fd.added_lines}/-{fd.removed_lines}）"]
    for hunk in fd.hunks:
        out.append(hunk.header)
        cursor = hunk.new_start
        lines = hunk.content.splitlines()
        for raw in lines[1:]:
            if raw.startswith("+") and not raw.startswith("+++"):
                prefix = f"{cursor:>5} +| " if with_line_no else "+ "
                out.append(prefix + raw[1:])
                cursor += 1
            elif raw.startswith("-") and not raw.startswith("---"):
                prefix = f"{'':>5} -| " if with_line_no else "- "
                out.append(prefix + raw[1:])
            else:
                prefix = f"{cursor:>5}  | " if with_line_no else "  "
                out.append(prefix + (raw[1:] if raw.startswith(" ") else raw))
                cursor += 1
    return "\n".join(out)


def render_diff(diff: DiffBundle, with_line_no: bool = True) -> str:
    return "\n\n".join(
        render_file_diff(fd, with_line_no) for fd in diff.reviewable_files
    )


def render_diffs(diffs: list[DiffBundle], with_line_no: bool = True) -> str:
    """把多个工程的 diff 拼接为可送模型的文本（多工程评审用）。"""
    parts: list[str] = []
    for d in diffs:
        body = render_diff(d, with_line_no)
        if body:
            header = f"# 工程：{d.repo_name or d.project}\n\n" if d.repo_name else ""
            parts.append(header + body)
    return "\n\n".join(parts)


def chunk_diff(diff: DiffBundle, max_chars: int = DEFAULT_CHUNK_CHARS) -> list[str]:
    """按文件累积分块：单文件超限时按 hunk 再切，避免上下文溢出。"""
    chunks: list[str] = []
    current: list[str] = []
    size = 0

    def flush() -> None:
        nonlocal current, size
        if current:
            chunks.append("\n\n".join(current))
            current, size = [], 0

    for fd in diff.reviewable_files:
        text = render_file_diff(fd)
        if len(text) > max_chars:
            flush()
            buf: list[str] = []
            buf_size = 0
            header = f"### 文件：{fd.path}（分片）"
            buf.append(header)
            buf_size += len(header)
            for hunk in fd.hunks:
                hunk_text = hunk.header + "\n" + "\n".join(hunk.content.splitlines()[1:])
                if buf_size + len(hunk_text) > max_chars and len(buf) > 1:
                    chunks.append("\n".join(buf))
                    buf, buf_size = [header], len(header)
                buf.append(hunk_text)
                buf_size += len(hunk_text)
            if len(buf) > 1:
                chunks.append("\n".join(buf))
            continue
        if size + len(text) > max_chars:
            flush()
        current.append(text)
        size += len(text)
    flush()
    return chunks


def truncate(text: str, limit: int = 2000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（已截断，共 {len(text)} 字符）"
