"""Output formatter for the world model plugin.

Transforms LLM responses containing ``<world_model>`` reasoning blocks
into a clean mixed format: a simulation summary panel + the final answer.
"""

from __future__ import annotations

import re
from typing import Optional

from . import de_bono_hats


# Regex to find <world_model>...</world_model> blocks.
#
# Neither side of the capture carries `\s*`. The previous form was
# `<world_model>\s*(.*?)\s*</world_model>`, and on an unclosed tag — a truncated
# LLM response, a model that opened the block and stopped — the engine retried
# the greedy whitespace prefix from every offset. That reads linear and is not:
# measured on a whitespace run after an unclosed tag, 400 sp = 39 ms,
# 800 sp = 310 ms, 1600 sp = 2.42 s, 3200 sp = 18.7 s, eight for one doubling.
# The same lengths with letters cost 0.01–0.08 ms. This runs on every assistant
# response, so one malformed reply was an 18-second hang.
#
# Dropping both is behaviour-preserving for the block itself: line 52 already
# strips the captured text, so the surrounding whitespace never reached a
# caller. With them gone the same 3,200-space input costs 0.14 ms and the
# captured groups are identical.
_WORLD_MODEL_RE = re.compile(
    r"<world_model>(.*?)</world_model>",
    re.DOTALL | re.IGNORECASE,
)

# Fallback: catch unclosed <world_model> tags (missing closing tag)
_FALLBACK_WM_RE = re.compile(
    r"<world_model>\s*(.*?)(?:</world_model>|$)",
    re.DOTALL | re.IGNORECASE,
)

# Regex to find [world_model_guide]...[world_model_guide] blocks
# Fallback: matches unclosed tags too (token truncation safety)
_GUIDE_RE = re.compile(
    r"\[world_model_guide\].*?\[/world_model_guide\]",
    re.DOTALL,
)


def _strip_guide_blocks(text: str) -> str:
    """Remove injected guidance blocks from the output."""
    return _GUIDE_RE.sub("", text).strip()


def _extract_world_model(text: str) -> tuple[list[str], str]:
    """Extract all <world_model> blocks, return (blocks, remaining_text).

    Tries properly closed tags first; falls back to unclosed tags if none found.
    """
    blocks: list[str] = []
    remaining = text

    while True:
        match = _WORLD_MODEL_RE.search(remaining)
        if not match:
            break
        blocks.append(match.group(1).strip())
        remaining = remaining[:match.start()] + remaining[match.end():]

    # Fallback: if no properly closed blocks, try unclosed <world_model> tags.
    # Do NOT swallow the content into blocks — strip the tag and leave content
    # in remaining so the user still sees the response (no data loss).
    if not blocks:
        match = _FALLBACK_WM_RE.search(remaining)
        if match and not match.group(0).rstrip().endswith("</world_model>"):
            content = match.group(1).strip()
            content = re.sub(r"<world_model>\s*", "", content, flags=re.IGNORECASE)
            remaining = (remaining[:match.start()] + content + remaining[match.end():]).strip()

    return blocks, remaining.strip()


def _detect_level_blocks(blocks: list[str]) -> list[tuple[int, str]]:
    """Tag each block with its recursion level (0 if unknown)."""
    tagged: list[tuple[int, str]] = []
    level = 0
    for block in blocks:
        # Look for explicit level markers
        m = re.search(r"(?:Level|level|RECURSION LEVEL)\s*(\d+)", block, re.IGNORECASE)
        if m:
            level = int(m.group(1))
        else:
            level += 1  # sequential assumption
        tagged.append((level, block))
    return tagged


def _format_simulation_panel(blocks: list[str], active_hats: Optional[list[str]] = None) -> str:
    """Format world model blocks into a clean summary panel."""
    non_empty = [b for b in blocks if b.strip()]
    if not non_empty:
        return ""

    header = "[DOGA: Thinking Process]"
    if active_hats:
        header += de_bono_hats.format_hats_header(active_hats)
    panel_lines = [header]
    panel_lines.append("   " + "=" * 50)

    tagged = _detect_level_blocks(non_empty)
    prev_level = 0

    for level, block in tagged:
        if level != prev_level and level > 1:
            panel_lines.append("")
            panel_lines.append(f"   ─── Level {level} ───")
            panel_lines.append("")
        elif prev_level > 0:
            panel_lines.append("")
            panel_lines.append("   " + "." * 50)
            panel_lines.append("")

        for line in block.strip().split("\n"):
            panel_lines.append(f"   {line}")

        prev_level = level

    panel_lines.append("")
    panel_lines.append("   " + "=" * 50)
    return "\n".join(panel_lines)


def format_response(
    response_text: str,
    show_simulation: bool = True,
    active_hats: Optional[list[str]] = None,
) -> str:
    """Main entry point for ``transform_llm_output``.

    Strips the injected guide, extracts world model blocks, and formats
    them as a summary panel above the final answer.
    """
    # First strip our injected guide markers
    cleaned = _strip_guide_blocks(response_text)

    # Extract world model reasoning blocks
    blocks, final_answer = _extract_world_model(cleaned)

    # Strip any remaining raw <world_model> tags from final answer
    # (catches mixed closed+unclosed tag scenarios)
    final_answer = re.sub(
        r"</?world_model>\s*", "", final_answer, flags=re.IGNORECASE
    ).strip()

    if not show_simulation or not blocks:
        return final_answer

    panel = _format_simulation_panel(blocks, active_hats=active_hats)
    if not panel:
        return final_answer

    return f"{panel}\n\n[Response]\n{final_answer}"
