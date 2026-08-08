"""Context budgeting for tool results (§3).

The rule the whole MCP layer is built around: **no tool result exceeds
`MCP_MAX_RESULT_TOKENS`.**

An HTTP response is read once and dropped. A tool result is not: it lands in the
agent's transcript and is re-sent, in full, on every subsequent turn. Ten pages
of documentation returned verbatim is 400 kB of markdown the agent then pays for
over and over — and that crowds out the conversation that made the call worth
anything. So each tool produces its complete payload (unchanged, straight from
the existing services), and what goes back on the wire is:

* `structured_content` — the metadata in full (URLs, statuses, counters, usage).
  Small, and the part an agent reasons over rather than reads;
* `content` — the text, truncated, each cut marked in plain language with the
  URI that serves the rest;
* one `resource_link`, **only when something was actually cut**. The payload is
  parked in the `ArtifactStore` in that case, so the remainder stays fetchable
  rather than lost. A result that fit gets no link: a pointer to text the agent
  already has is one more thing to read and reason about for nothing.

Two details matter more than they look:

**Truncation is from the top.** The head of a page carries the title, the intro
and the headings — exactly what an agent needs to decide whether the rest is
worth a resource read. Cutting from the middle would keep more "interesting"
text and lose the ability to make that decision.

**The budget is global, then divided.** Ten URLs do not get ten times the
budget; they share it. A URL that failed does not hold a share — it is
redistributed over the ones that produced text, so asking for ten pages and
getting two back returns two useful pages, not two thin ones.
"""

from __future__ import annotations

from dataclasses import dataclass

from mcp.types import ContentBlock, ResourceLink, TextContent

from app.core.llm.tokens import count_tokens, trim_to_tokens
from app.mcp.artifacts import ArtifactPage, ArtifactStore

# URI scheme every artefact and job resource lives under (§7).
SCHEME = "search-api"

# Share of the budget held back for the header line, the per-page titles and the
# truncation markers. They are small but not free, and a ceiling is a ceiling.
_OVERHEAD_RESERVE = 0.15
# A page's slice never drops below this: below a couple of sentences the text is
# not a preview of anything, and the agent may as well be handed the link alone.
_MIN_PAGE_TOKENS = 64


def artifact_uri(artifact_id: str, page: int | None = None) -> str:
    if page is None:
        return f"{SCHEME}://artifact/{artifact_id}"
    return f"{SCHEME}://artifact/{artifact_id}/page/{page}"


def job_uri(job_id: str) -> str:
    return f"{SCHEME}://job/{job_id}"


@dataclass
class BudgetedResult:
    """What a tool hands back: blocks for the wire, plus what they cost."""

    blocks: list[ContentBlock]
    # Tokens actually emitted as text, for the `mcp_result_tokens` histogram —
    # the only way to find out whether the budget holds outside a test.
    text_tokens: int
    truncated: bool
    artifact_id: str | None = None


def _marker(kept: int, total: int, uri: str) -> str:
    return f"\n\n[truncated: {kept:,} of {total:,} characters. Read the rest at {uri}]"


def _heading(index: int, page: ArtifactPage) -> str:
    return str(page.title or page.meta.get("url") or f"Page {index}")


async def budget_pages(
    *,
    tool: str,
    pages: list[ArtifactPage],
    store: ArtifactStore,
    max_result_tokens: int,
    max_chars_per_page: int | None = None,
    payload: dict[str, object] | None = None,
    header: str | None = None,
) -> BudgetedResult:
    """Fit ``pages`` into the token ceiling; park the full payload if anything is cut.

    ``max_chars_per_page`` is the *caller's* cap and applies on top of the share
    the budget allows; it can only make a page shorter, never longer. That
    ordering is deliberate: an agent asking for 50 000 characters across ten URLs
    must not be able to talk its way past the instance's context budget.
    """
    # An id has to exist before the text is written, because the truncation
    # markers point at it. The artefact is only *stored* if a marker was used.
    artifact_id = ArtifactStore.mint_id()

    parts: list[str] = [header] if header else []
    truncated = False

    with_text = [(index, page) for index, page in enumerate(pages, start=1) if page.text]
    if with_text:
        usable = max(int(max_result_tokens * (1 - _OVERHEAD_RESERVE)), _MIN_PAGE_TOKENS)
        per_page_tokens = max(usable // len(with_text), _MIN_PAGE_TOKENS)
        for index, page in with_text:
            text = page.text
            if max_chars_per_page is not None and max_chars_per_page > 0:
                text = text[:max_chars_per_page]
            body = await trim_to_tokens(text, per_page_tokens)
            chunk = f"## {_heading(index, page)}\n\n{body}"
            if len(body) < len(page.text):
                truncated = True
                chunk += _marker(len(body), len(page.text), artifact_uri(artifact_id, index))
            parts.append(chunk)

    # A page with no text still deserves a line: an agent told only about the two
    # URLs that worked will cheerfully re-request the eight that did not.
    for index, page in enumerate(pages, start=1):
        error = page.meta.get("error")
        if not page.text and error:
            parts.append(f"## {_heading(index, page)}\n\n(no content: {error})")

    body_text = "\n\n".join(part for part in parts if part)
    blocks: list[ContentBlock] = []
    if body_text:
        blocks.append(TextContent(type="text", text=body_text))

    if truncated:
        artifact = await store.put(
            tool, pages, payload=dict(payload or {}), artifact_id=artifact_id
        )
        blocks.append(
            ResourceLink(
                type="resource_link",
                uri=artifact_uri(artifact.id),
                name=f"{tool} full result ({artifact.total_chars:,} characters)",
                description=(
                    "The complete, untruncated payload of this call. Read one page at a "
                    f"time with {artifact_uri(artifact.id, 1)} … "
                    f"{artifact_uri(artifact.id, max(len(pages), 1))}."
                ),
                mime_type="text/markdown",
            )
        )

    return BudgetedResult(
        blocks=blocks,
        text_tokens=await count_tokens(body_text),
        truncated=truncated,
        artifact_id=artifact_id if truncated else None,
    )
