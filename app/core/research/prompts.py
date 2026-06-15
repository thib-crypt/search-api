"""Prompt templates for the deep-research engine.

Faithful Python port of the prompts from https://github.com/dzhng/deep-research,
adapted to (a) draw content from SearXNG + crawl4ai rather than Firecrawl and
(b) always answer in the language of the user's query.
"""

from __future__ import annotations

from datetime import date


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
    query: str, contents: list[str], num_learnings: int, num_followups: int
) -> str:
    blocks = "\n".join(f"<content>\n{c}\n</content>" for c in contents)
    return (
        f"Given the following contents from a search for the query <query>{query}</query>, "
        f"generate a list of learnings from the contents. Return a maximum of "
        f"{num_learnings} learnings, but feel free to return fewer if the contents are "
        "clear. Make each learning unique and information-dense. Include entities like "
        "people, places, companies, products, and exact metrics, numbers, or dates. "
        "These learnings will be used to research the topic further. Also propose up to "
        f"{num_followups} follow-up questions.\n\n"
        f"<contents>\n{blocks}\n</contents>"
    )


def final_report_prompt(query: str, learnings: list[str]) -> str:
    joined = "\n".join(f"<learning>\n{learning}\n</learning>" for learning in learnings)
    return (
        "Given the following prompt from the user, write a final report on the topic "
        "using the learnings from research. Make it as detailed as possible — aim for "
        "3 or more pages. Include ALL the learnings from research:\n\n"
        f"<prompt>{query}</prompt>\n\n"
        f"Here are all the learnings from research:\n\n{joined}"
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
