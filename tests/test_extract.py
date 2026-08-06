"""POST /v1/extract (§5.1): both modes, per-URL failures, JSON-Schema mapping."""

from __future__ import annotations

import json
from typing import Any, get_args, get_origin

from pydantic import BaseModel
from starlette.testclient import TestClient

from app.api.deps import get_crawler, get_llm
from app.main import app
from app.models.crawl import CrawledPage, CrawlRequest
from app.services.extract import model_from_json_schema

SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "price": {"type": "number"},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title"],
}


class FakeCrawler:
    """Succeeds on every URL except those containing 'bad'."""

    def __init__(self, extracted: str | None = None) -> None:
        self.extracted = extracted
        self.last_strategy = None

    def _page(self, url: str) -> CrawledPage:
        if "bad" in url:
            return CrawledPage(url=url, success=False, error="boom")
        return CrawledPage(url=url, success=True, markdown=f"content of {url}", word_count=3)

    async def crawl(self, req: CrawlRequest) -> list[CrawledPage]:
        return [self._page(str(u)) for u in req.urls]

    async def crawl_extracted(self, req: CrawlRequest, *, extraction_strategy):
        self.last_strategy = extraction_strategy
        return [(self._page(str(u)), self.extracted) for u in req.urls]


class FakeLLM:
    default_model = "test/model"

    def __init__(self) -> None:
        self.calls = 0

    async def structured(self, messages, response_model, *, model=None, **kwargs):
        self.calls += 1
        return response_model.model_validate({"title": "Widget", "price": 9.5, "tags": ["a"]})


def _override(crawler, llm):
    app.dependency_overrides[get_crawler] = lambda: crawler
    app.dependency_overrides[get_llm] = lambda: llm


# --- JSON Schema -> Pydantic ---------------------------------------------


def test_json_schema_maps_primitives_arrays_and_required() -> None:
    model = model_from_json_schema(SCHEMA)
    fields = model.model_fields

    assert fields["title"].annotation is str
    assert fields["title"].is_required()
    # Non-required fields become optional so a page missing one still validates.
    assert not fields["price"].is_required()
    assert get_origin(fields["tags"].annotation) is not None


def test_json_schema_maps_nested_objects() -> None:
    model = model_from_json_schema(
        {
            "type": "object",
            "properties": {
                "author": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                }
            },
            "required": ["author"],
        }
    )
    nested = model.model_fields["author"].annotation
    assert isinstance(nested, type) and issubclass(nested, BaseModel)
    assert nested.model_fields["name"].annotation is str


def test_unknown_types_fall_back_to_any_instead_of_erroring() -> None:
    model = model_from_json_schema(
        {"type": "object", "properties": {"weird": {"type": "quaternion"}}}
    )
    # The field is still declared -- nothing the caller asked for is dropped.
    assert "weird" in model.model_fields
    assert model.model_fields["weird"].annotation is Any


def test_union_types_are_supported() -> None:
    model = model_from_json_schema(
        {"type": "object", "properties": {"n": {"type": ["integer", "null"]}}}
    )
    assert set(get_args(model.model_fields["n"].annotation)) >= {int, type(None)}


def test_field_names_that_are_not_identifiers_are_sanitized() -> None:
    model = model_from_json_schema(
        {"type": "object", "properties": {"list-price": {"type": "number"}}}
    )
    instance = model.model_validate({"list-price": 3.0})
    assert instance.model_dump(by_alias=True)["list-price"] == 3.0


# --- Endpoint -------------------------------------------------------------


def test_extract_llm_mode() -> None:
    llm = FakeLLM()
    _override(FakeCrawler(), llm)
    try:
        with TestClient(app) as client:
            resp = client.post(
                "/v1/extract",
                json={"urls": ["https://a.example/", "https://b.example/"], "schema": SCHEMA},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 2
        assert llm.calls == 2
        assert body["results"][0]["success"] is True
        assert body["results"][0]["data"]["title"] == "Widget"
    finally:
        app.dependency_overrides.clear()


def test_extract_css_mode_makes_no_llm_call() -> None:
    llm = FakeLLM()
    crawler = FakeCrawler(extracted=json.dumps([{"title": "From CSS", "price": "3"}]))
    _override(crawler, llm)
    try:
        with TestClient(app) as client:
            resp = client.post(
                "/v1/extract",
                json={
                    "urls": ["https://a.example/"],
                    "mode": "css",
                    "schema": {
                        "name": "product",
                        "baseSelector": "div.product",
                        "fields": [{"name": "title", "selector": "h1", "type": "text"}],
                    },
                },
            )
        assert resp.status_code == 200
        assert llm.calls == 0
        assert crawler.last_strategy is not None
        assert resp.json()["results"][0]["data"]["title"] == "From CSS"
    finally:
        app.dependency_overrides.clear()


def test_css_mode_wraps_multiple_records_under_items() -> None:
    crawler = FakeCrawler(extracted=json.dumps([{"t": 1}, {"t": 2}]))
    _override(crawler, FakeLLM())
    try:
        with TestClient(app) as client:
            resp = client.post(
                "/v1/extract",
                json={
                    "urls": ["https://a.example/"],
                    "mode": "css",
                    "schema": {"name": "x", "baseSelector": "div", "fields": []},
                },
            )
        assert resp.json()["results"][0]["data"] == {"items": [{"t": 1}, {"t": 2}]}
    finally:
        app.dependency_overrides.clear()


def test_one_failing_url_does_not_sink_the_batch() -> None:
    _override(FakeCrawler(), FakeLLM())
    try:
        with TestClient(app) as client:
            resp = client.post(
                "/v1/extract",
                json={"urls": ["https://ok.example/", "https://bad.example/"], "schema": SCHEMA},
            )
        results = resp.json()["results"]
        assert results[0]["success"] is True
        assert results[1]["success"] is False
        assert results[1]["error"] == "boom"
        assert results[1]["data"] is None
    finally:
        app.dependency_overrides.clear()


def test_llm_failure_on_one_url_is_reported_per_url() -> None:
    class HalfBrokenLLM(FakeLLM):
        async def structured(self, messages, response_model, *, model=None, **kwargs):
            if "b.example" in messages[1]["content"]:
                raise RuntimeError("model said no")
            return await super().structured(messages, response_model, model=model)

    _override(FakeCrawler(), HalfBrokenLLM())
    try:
        with TestClient(app) as client:
            resp = client.post(
                "/v1/extract",
                json={"urls": ["https://a.example/", "https://b.example/"], "schema": SCHEMA},
            )
        results = resp.json()["results"]
        assert results[0]["success"] is True
        assert results[1]["success"] is False
        assert "model said no" in results[1]["error"]
    finally:
        app.dependency_overrides.clear()


def test_extract_rejects_empty_urls() -> None:
    with TestClient(app) as client:
        resp = client.post("/v1/extract", json={"urls": [], "schema": SCHEMA})
    assert resp.status_code == 422
