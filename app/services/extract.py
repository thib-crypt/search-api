"""Structured extraction from arbitrary URLs (§5.1).

Two modes share the same crawl path — and therefore the same SSRF guard, the same
per-host politeness and the same global browser budget as `/v1/crawl`:

* ``css``  — crawl4ai's ``JsonCssExtractionStrategy``. Deterministic, no model call.
* ``llm``  — the page markdown is handed to the model together with a Pydantic
  model built at request time from the caller's JSON Schema.

A failure on one URL (blocked, uncrawlable, unparseable) is reported on that URL
only; the rest of the batch still returns, exactly like `/v1/crawl`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Union

from crawl4ai.extraction_strategy import JsonCssExtractionStrategy
from pydantic import BaseModel, Field, create_model

from app.core.crawl.crawler import CrawlerService
from app.core.llm.client import LLMClient
from app.core.llm.tokens import trim_to_tokens
from app.models.crawl import CrawledPage, CrawlRequest
from app.models.extract import ExtractRequest, ExtractResult

logger = logging.getLogger("unified_search_api.extract")

# Page content sent to the model per URL. Extraction targets a handful of fields;
# a whole book of markdown buys nothing and costs a lot.
_PAGE_TOKEN_BUDGET = 6000

_PRIMITIVES: dict[str, Any] = {
    "string": str,
    "number": float,
    "integer": int,
    "boolean": bool,
    "null": type(None),
}

_SYSTEM_PROMPT = (
    "You extract structured data from web pages. Use ONLY what the page actually "
    "states. Never invent a value: if a field is not present on the page, leave it "
    "null (or omit it when it is optional)."
)


def _safe_name(raw: str, fallback: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in raw).strip("_")
    return cleaned or fallback


def _python_type(spec: object, name: str) -> Any:
    """Map one JSON Schema node to a Python annotation.

    Reasonable effort, not a full JSON Schema implementation. Anything it does not
    recognise becomes ``Any`` — the field is still declared, so nothing the caller
    asked for is silently dropped.
    """
    if not isinstance(spec, dict):
        return Any

    declared = spec.get("type")
    if isinstance(declared, list):  # e.g. ["string", "null"]
        members = tuple(_python_type({**spec, "type": t}, name) for t in declared)
        return Union[members] if members else Any  # noqa: UP007 - runtime construction

    if declared == "object":
        properties = spec.get("properties")
        if isinstance(properties, dict) and properties:
            return model_from_json_schema(spec, name)
        return dict

    if declared == "array":
        items = spec.get("items")
        if items is None:
            return list
        return list[_python_type(items, f"{name}Item")]  # type: ignore[misc]

    if isinstance(declared, str) and declared in _PRIMITIVES:
        return _PRIMITIVES[declared]
    return Any


def model_from_json_schema(schema: dict[str, Any], name: str = "Extracted") -> type[BaseModel]:
    """Build a Pydantic model from a JSON Schema object, at request time."""
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        # No properties to enforce: accept whatever the model returns.
        return create_model(name, __base__=_FreeForm)

    required = set(schema.get("required") or [])
    fields: dict[str, tuple[Any, Any]] = {}
    for index, (key, spec) in enumerate(properties.items()):
        field_name = _safe_name(str(key), f"field_{index}")
        annotation = _python_type(spec, f"{name}_{field_name}")
        description = spec.get("description") if isinstance(spec, dict) else None
        if key in required:
            default = Field(..., alias=str(key), description=description)
        else:
            # Optional fields default to None so a page that simply lacks one does
            # not fail validation and cost the whole URL.
            annotation = annotation | None if annotation is not Any else Any
            default = Field(default=None, alias=str(key), description=description)
        fields[field_name] = (annotation, default)

    # The whole point of this function is to build a model whose shape is only
    # known at request time, from caller-supplied JSON Schema: there is no static
    # type for `**fields` to match, so the overload check has nothing to check.
    model: type[BaseModel] = create_model(  # type: ignore[call-overload]
        _safe_name(name, "Extracted"), __base__=_AliasedBase, **fields
    )
    return model


class _AliasedBase(BaseModel):
    """Base for generated models: keys may be sent under their original names."""

    model_config = {"populate_by_name": True, "extra": "ignore"}


class _FreeForm(BaseModel):
    model_config = {"extra": "allow"}


class ExtractService:
    def __init__(self, crawler: CrawlerService, llm: LLMClient) -> None:
        self._crawler = crawler
        self._llm = llm

    async def extract(self, req: ExtractRequest) -> list[ExtractResult]:
        if req.mode == "css":
            return await self._extract_css(req)
        return await self._extract_llm(req)

    # -- css ----------------------------------------------------------------

    async def _extract_css(self, req: ExtractRequest) -> list[ExtractResult]:
        try:
            strategy = JsonCssExtractionStrategy(schema=req.json_schema)
        except Exception as exc:
            raise ValueError(f"invalid CSS extraction schema: {exc}") from exc

        crawl_req = CrawlRequest(urls=req.urls, formats=["markdown"])
        pairs = await self._crawler.crawl_extracted(crawl_req, extraction_strategy=strategy)

        results: list[ExtractResult] = []
        for page, extracted in pairs:
            if not page.success:
                results.append(_failed(page))
                continue
            try:
                results.append(
                    ExtractResult(url=page.url, data=_records_to_data(extracted), success=True)
                )
            except Exception as exc:
                results.append(
                    ExtractResult(url=page.url, success=False, error=f"extraction failed: {exc}")
                )
        return results

    # -- llm ----------------------------------------------------------------

    async def _extract_llm(self, req: ExtractRequest) -> list[ExtractResult]:
        try:
            response_model = model_from_json_schema(req.json_schema)
        except Exception as exc:
            raise ValueError(f"invalid JSON Schema: {exc}") from exc

        pages = await self._crawler.crawl(CrawlRequest(urls=req.urls, formats=["markdown"]))
        return list(
            await asyncio.gather(*(self._extract_one(page, req, response_model) for page in pages))
        )

    async def _extract_one(
        self, page: CrawledPage, req: ExtractRequest, response_model: type[BaseModel]
    ) -> ExtractResult:
        if not page.success:
            return _failed(page)
        if not page.markdown:
            return ExtractResult(url=page.url, success=False, error="page had no extractable text")

        body = await trim_to_tokens(page.markdown, _PAGE_TOKEN_BUDGET)
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Extract the requested fields from this page.\n\nURL: {page.url}\n\n{body}"
                ),
            },
        ]
        try:
            data = await self._llm.structured(messages, response_model, model=req.model)
        except Exception as exc:
            logger.info("Extraction failed for %s: %s", page.url, exc)
            return ExtractResult(url=page.url, success=False, error=f"extraction failed: {exc}")
        return ExtractResult(url=page.url, data=data.model_dump(by_alias=True), success=True)


def _failed(page: CrawledPage) -> ExtractResult:
    return ExtractResult(url=page.url, success=False, error=page.error or "crawl failed")


def _records_to_data(extracted: str | None) -> dict[str, Any]:
    """crawl4ai returns a JSON *list* of records; the API answers with one object.

    A single record is returned as-is (the common "one product page" case);
    several are wrapped under ``items`` so the shape stays a dict either way.
    """
    if not extracted:
        return {}
    records = json.loads(extracted)
    if isinstance(records, dict):
        return records
    if isinstance(records, list):
        if len(records) == 1 and isinstance(records[0], dict):
            return records[0]
        return {"items": records}
    return {"value": records}
