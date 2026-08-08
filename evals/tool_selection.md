# Tool-selection eval

The unit tests prove the nine tools are wired correctly. They cannot prove the thing that
actually decides whether this is a good MCP server: **that an agent picks the right one.**

Selection is settled entirely by the descriptions in `app/mcp/tools.py`, which is why
`search`, `answer` and `deep_research` — three tools that all answer "find me things about
X", three orders of magnitude apart in cost — are the whole risk of §2. A failure here is
never a bug in the code. It is a description that needs rewriting.

## How to run it

By hand, in a real client (Claude Code or Claude Desktop pointed at `/mcp`), whenever a
tool description, a tool name or the server instructions change. A fresh conversation per
row, no other context, no hints. Then note which tool the model reached for **first**.

Nothing here is automated on purpose. The evaluation is "did the model choose well given
only the descriptions", and a harness that primes the model with the answer format has
already changed the thing under test.

## The set

Fifteen prompts. The trap column is the tool a badly-written description gets chosen
instead — that is what each row is really testing.

| # | Prompt | Expected | Trap | What it tests |
|---|---|---|---|---|
| 1 | "what time is it in Tokyo" | `web_search` | `answer`, `deep_research` | that a trivial fact does not reach for a paid tool |
| 2 | "summarise this article: https://example.com/post" | `fetch_url` | `answer` | that a known URL is read, not re-searched |
| 3 | "what pages does example.com have?" | `map_site` | `crawl_site` | that listing is not crawling |
| 4 | "get me the price and stock for these 3 product pages" | `extract_structured` | `fetch_url` + reading by hand | that typed fields have a tool |
| 5 | "HNSW vs IVFFlat in pgvector — what are the trade-offs?" | `answer` | `deep_research` | that a paragraph-sized answer is not a report |
| 6 | "write me a report on the state of RAG in production in 2026" | `deep_research_estimate` → `deep_research` | `answer` | that a genuine report is worth the cost, and priced first |
| 7 | "find me three recent papers on speculative decoding" | `web_search` | `answer` | that "find" means links |
| 8 | "read these 5 docs pages and tell me how auth works" | `fetch_url` | `crawl_site`, `answer` | that a known list is faster than a crawl |
| 9 | "how much would a deep research run on quantum error correction cost?" | `deep_research_estimate` | `deep_research` | that pricing does not require spending |
| 10 | "index the whole docs site so I can ask about it" | `map_site` → `crawl_site` | `crawl_site` alone | that you look before you leap |
| 11 | "is it true that Postgres 17 removed the WAL archive?" | `web_search` + `fetch_url` | `answer` | that verification reads sources (cf. the `verify_claim` prompt) |
| 12 | "what did my crawl job a1b2c3 end up finding?" | `job_status` | re-running `crawl_site` | that a job id is collected, not re-run |
| 13 | "compare the pricing pages of these two competitors" | `fetch_url` | `answer`, `extract_structured` | that reading two pages is free |
| 14 | "what's the capital of Australia" | `web_search` or no tool at all | `answer` | that the model does not spend on what it knows |
| 15 | "I need everything published about the EU AI Act's GPAI obligations, with sources" | `deep_research` | `answer`, repeated `web_search` | that a real research task is not faked with cheap tools |

## Reading the results

- **Rows 1, 5, 7, 14 choosing a paid tool** — the free tools' descriptions are not saying
  loudly enough what they are for, or the paid ones are not saying loudly enough what they
  are *not* for. Look at the "do not use X when…" sentences first.
- **Row 6 or 15 choosing `answer`** — `answer` is over-selling itself. Its description
  should say where it stops.
- **Row 6 skipping the estimate** — `deep_research`'s description needs to point harder at
  `deep_research_estimate`; that pointer is the only thing standing between an agent loop
  and a surprising invoice.
- **Row 10 skipping `map_site`** — `crawl_site` is not warning about its own size.
- **Row 12 re-running the crawl** — `job_status` is not discoverable enough from the job
  payload `crawl_site` returns.

## Related automated checks

Some of this *is* mechanically checkable, and those checks live in
`tests/test_mcp_schema.py`:

- every description mentions what the tool costs;
- the expensive tools name the cheaper alternative (`deep_research` → `answer` and
  `deep_research_estimate`; `extract_structured` → `fetch_url`; `crawl_site` → `fetch_url`;
  `answer` → `web_search`);
- the advertised schema bounds match the ones actually enforced;
- the whole tool surface is snapshotted, so a description or argument change shows up in a
  diff rather than surprising a production agent.

Those tests keep the *structure* honest. This file is the only thing that checks the
prose does its job.
