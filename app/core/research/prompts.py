"""Prompt templates for the deep-research engine.

Faithful Python port of the prompts from https://github.com/dzhng/deep-research,
adapted to (a) draw content from SearXNG + crawl4ai rather than Firecrawl and
(b) always answer in the language of the user's query.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # import only for typing: prompts must stay free of model imports
    from app.models.search import SearchResultItem


def system_prompt() -> str:
    today = date.today().isoformat()
    return (
        f"You are an expert researcher. Today is {today}. Follow these instructions "
        "when responding:\n"
        "- You may be asked to research subjects beyond your knowledge cutoff; assume "
        "the user is right when they present recent news.\n"
        "- The user is a highly experienced analyst, so be as detailed as possible and "
        "do not simplify — they can handle a lot of detail.\n"
        "- Be highly organized and accurate; mistakes erode trust.\n"
        "- Value good arguments over authorities; the source is irrelevant.\n"
        "- Consider new technologies and contrarian ideas, not just conventional wisdom.\n"
        "- You may use high levels of speculation or prediction, but flag it clearly.\n"
        "- IMPORTANT: Always write in the SAME LANGUAGE as the user's research query."
    )


def serp_queries_prompt(query: str, num_queries: int, learnings: list[str]) -> str:
    prompt = (
        "Given the following prompt from the user, generate a list of SERP queries to "
        f"research the topic. Return a maximum of {num_queries} queries, but feel free "
        "to return fewer if the original prompt is clear. Make each query unique and "
        "non-overlapping.\n\n"
        f"<prompt>{query}</prompt>"
    )
    if learnings:
        joined = "\n".join(learnings)
        prompt += (
            "\n\nHere are some learnings from previous research; use them to generate "
            f"more specific queries:\n{joined}"
        )
    return prompt


def process_result_prompt(
    query: str,
    contents: list[str],
    num_learnings: int,
    num_followups: int,
    urls: list[str] | None = None,
) -> str:
    """Contents are numbered so each learning can point back at its sources."""
    labels = urls or []
    blocks = "\n".join(
        f'<content index="{i + 1}" url="{labels[i] if i < len(labels) else ""}">\n{c}\n</content>'
        for i, c in enumerate(contents)
    )
    return (
        f"Given the following contents from a search for the query <query>{query}</query>, "
        f"generate a list of learnings from the contents. Return a maximum of "
        f"{num_learnings} learnings, but feel free to return fewer if the contents are "
        "clear. Make each learning unique and information-dense. Include entities like "
        "people, places, companies, products, and exact metrics, numbers, or dates. "
        "For every learning, list the index numbers of the contents it comes from in "
        "`source_indices` (they are the numbers in the `index` attribute below). "
        "These learnings will be used to research the topic further. Also propose up to "
        f"{num_followups} follow-up questions.\n\n"
        f"<contents>\n{blocks}\n</contents>"
    )


def final_report_prompt(query: str, learnings: list[str], sources: list[str]) -> str:
    """``learnings`` already carry their ``[n]`` markers; ``sources`` is the numbered map."""
    joined = "\n".join(f"<learning>\n{learning}\n</learning>" for learning in learnings)
    numbered = "\n".join(f"[{i + 1}] {url}" for i, url in enumerate(sources))
    prompt = (
        "Given the following prompt from the user, write a final report on the topic "
        "using the learnings from research. Make it as detailed as possible — aim for "
        "3 or more pages. Include ALL the learnings from research. Write the report in "
        "Markdown, and return the report only — no preamble, no commentary.\n\n"
        f"<prompt>{query}</prompt>\n\n"
        f"Here are all the learnings from research:\n\n{joined}"
    )
    if numbered:
        prompt += (
            "\n\nEach learning ends with the numbers of the sources it came from. "
            "Cite them inline in the report with markers like [1], [2] — right after "
            "the sentence they support — using ONLY the numbers below. Never invent a "
            "number that is not in this list, and do not add a bibliography at the end "
            "(one is appended automatically).\n\n"
            f"Numbered sources:\n{numbered}"
        )
    return prompt


def rerank_prompt(goal: str, results: list[SearchResultItem], top_k: int) -> str:
    """Ask which SERP results deserve a crawl, on metadata alone."""
    listed = "\n".join(
        f"[{i}] {r.title}\n    url: {r.url}\n    snippet: {(r.snippet or '')[:400]}"
        for i, r in enumerate(results)
    )
    return (
        "You are selecting which search results are worth fetching in full for the "
        "research goal below. Judge on the title, the domain and the snippet only.\n\n"
        f"<goal>{goal}</goal>\n\n"
        f"Results:\n{listed}\n\n"
        f"Return at most {top_k} indices in `keep`, ordered from most to least relevant. "
        "Prefer primary, substantive, up-to-date sources. Reject link lists, aggregator "
        "and social pages, thin or off-topic content, and near-duplicates of a result you "
        "already kept. Give a short reason for each rejected index in `dropped_reason`."
    )


def feedback_prompt(query: str, max_questions: int, force: bool = False) -> str:
    if force:
        instruction = (
            "Given the following query from the user, generate up to "
            f"{max_questions} clarifying questions to better direct the research."
        )
    else:
        instruction = (
            "Given the following query from the user, decide whether clarifying "
            f"questions are needed. Ask up to {max_questions} questions ONLY if the "
            "query is ambiguous or under-specified; otherwise return an empty list."
        )
    return f"{instruction}\n\n<query>{query}</query>"
