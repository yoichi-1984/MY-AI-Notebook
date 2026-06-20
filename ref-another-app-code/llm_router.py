from __future__ import annotations

import copy
import random
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from google import genai
from google.genai import errors, types

try:
    from gp_chat import config as app_config
except ImportError:
    import config as app_config


LoggerFn = Callable[[str, str], None]


@dataclass(frozen=True)
class LLMClients:
    standard_client: genai.Client
    priority_client: genai.Client
    project_id: str | None
    location: str


@dataclass
class StreamChunk:
    text_delta: str = ""
    thought_delta: str = ""
    usage_metadata: types.GenerateContentResponseUsageMetadata | None = None
    grounding_metadata: dict[str, object] | None = None
    route: str = app_config.LLM_ROUTE_STANDARD
    app_retry_count: int = 0
    sdk_http_headers: dict[str, str] | None = None


@dataclass
class GenerateResult:
    text: str = ""
    usage_metadata: types.GenerateContentResponseUsageMetadata | None = None
    grounding_metadata: dict[str, object] | None = None
    route: str = app_config.LLM_ROUTE_STANDARD
    app_retry_count: int = 0
    sdk_http_headers: dict[str, str] | None = None
    response: types.GenerateContentResponse | None = None


def _log(logger: LoggerFn | None, message: str, level: str = "info") -> None:
    if not logger:
        return
    try:
        logger(message, level)
    except TypeError:
        logger(message)


def _build_standard_http_options() -> types.HttpOptions:
    return types.HttpOptions(
        retry_options=types.HttpRetryOptions(
            attempts=1,
            http_status_codes=list(app_config.LLM_RETRYABLE_STATUS_CODES),
        )
    )


def _build_priority_http_options() -> types.HttpOptions:
    return types.HttpOptions(
        headers={
            app_config.PRIORITY_HEADER_REQUEST_TYPE: (
                app_config.PRIORITY_HEADER_REQUEST_TYPE_VALUE
            ),
            app_config.PRIORITY_HEADER_SHARED_REQUEST_TYPE: (
                app_config.PRIORITY_HEADER_SHARED_REQUEST_TYPE_VALUE
            ),
        },
        retry_options=types.HttpRetryOptions(
            attempts=5,
            initial_delay=1.0,
            exp_base=2.0,
            max_delay=60.0,
            jitter=1.0,
            http_status_codes=list(app_config.LLM_RETRYABLE_STATUS_CODES),
        ),
    )


def build_llm_clients(
    *,
    project_id: str | None,
    location: str,
) -> LLMClients:
    standard_client = genai.Client(
        vertexai=True,
        project=project_id,
        location=location,
        http_options=_build_standard_http_options(),
    )
    priority_client = genai.Client(
        vertexai=True,
        project=project_id,
        location=location,
        http_options=_build_priority_http_options(),
    )
    return LLMClients(
        standard_client=standard_client,
        priority_client=priority_client,
        project_id=project_id,
        location=location,
    )


def coerce_llm_clients(client_or_llm_clients: Any) -> LLMClients:
    if isinstance(client_or_llm_clients, LLMClients):
        return client_or_llm_clients

    api_client = _get_attr(client_or_llm_clients, "_api_client")
    project_id = _get_attr(api_client, "project")
    location = _get_attr(api_client, "location") or "global"
    return build_llm_clients(
        project_id=project_id,
        location=location,
    )


def _clone_config(
    config: types.GenerateContentConfig | dict[str, Any] | None,
) -> types.GenerateContentConfig | None:
    if config is None:
        return None
    if isinstance(config, dict):
        return types.GenerateContentConfig.model_validate(config)
    if hasattr(config, "model_copy"):
        return config.model_copy(deep=True)
    return copy.deepcopy(config)


def _get_attr(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _enum_value(value: Any) -> Any:
    if value is None:
        return None
    return getattr(value, "value", value)


def _normalize_headers(headers: Any) -> dict[str, str] | None:
    if headers is None:
        return None
    try:
        return {str(key): str(value) for key, value in dict(headers).items()}
    except Exception:
        return None


def _extract_response_text(response: types.GenerateContentResponse | None) -> str:
    if response is None:
        return ""
    candidates = _get_attr(response, "candidates", []) or []
    if candidates:
        text_parts: list[str] = []
        parts = _get_attr(_get_attr(candidates[0], "content"), "parts", []) or []
        for part in parts:
            thought_value = _get_attr(part, "thought")
            if thought_value:
                # Reasoning model's reasoning process is ignored in text extraction
                continue
            part_text = _get_attr(part, "text")
            if part_text:
                text_parts.append(part_text)
        if text_parts:
            return "".join(text_parts)
    return _get_attr(response, "text", "") or ""


def _extract_grounding_metadata(
    response: types.GenerateContentResponse | None,
) -> dict[str, object] | None:
    if response is None:
        return None
    candidates = _get_attr(response, "candidates", []) or []
    if not candidates:
        return None
    raw_meta = _get_attr(candidates[0], "grounding_metadata")
    if raw_meta is None:
        return None

    metadata: dict[str, object] = {}
    queries = list(_get_attr(raw_meta, "web_search_queries", []) or [])
    if queries:
        metadata["queries"] = queries

    sources = []
    for chunk in _get_attr(raw_meta, "grounding_chunks", []) or []:
        web = _get_attr(chunk, "web")
        uri = _get_attr(web, "uri")
        if not uri:
            continue
        sources.append({"title": _get_attr(web, "title") or uri, "uri": uri})
    if sources:
        metadata["sources"] = sources

    return metadata or None


def merge_grounding_metadata(
    current: dict[str, object] | None,
    incoming: dict[str, object] | None,
) -> dict[str, object] | None:
    if not incoming:
        return current

    merged = {
        "sources": list((current or {}).get("sources", [])),
        "queries": list((current or {}).get("queries", [])),
    }

    existing_uris = {source.get("uri") for source in merged["sources"]}
    for source in incoming.get("sources", []):
        uri = source.get("uri")
        if uri and uri not in existing_uris:
            merged["sources"].append(source)
            existing_uris.add(uri)

    existing_queries = set(merged["queries"])
    for query in incoming.get("queries", []):
        if query not in existing_queries:
            merged["queries"].append(query)
            existing_queries.add(query)

    if not merged["sources"] and not merged["queries"]:
        return None
    return merged


def summarize_usage_metadata(
    usage_metadata: types.GenerateContentResponseUsageMetadata | None,
) -> dict[str, Any] | None:
    if usage_metadata is None:
        return None
    return {
        "prompt_token_count": _get_attr(usage_metadata, "prompt_token_count", 0) or 0,
        "candidates_token_count": _get_attr(usage_metadata, "candidates_token_count", 0)
        or 0,
        "total_token_count": _get_attr(usage_metadata, "total_token_count", 0) or 0,
        "thoughts_token_count": _get_attr(usage_metadata, "thoughts_token_count", 0) or 0,
        "cached_content_token_count": _get_attr(
            usage_metadata, "cached_content_token_count", 0
        )
        or 0,
        "traffic_type": _enum_value(_get_attr(usage_metadata, "traffic_type")),
    }


def format_usage_log(
    usage_metadata: types.GenerateContentResponseUsageMetadata | None,
) -> str | None:
    usage = summarize_usage_metadata(usage_metadata)
    if not usage:
        return None
    return (
        "[USAGE] "
        f"prompt={usage['prompt_token_count']} "
        f"output={usage['candidates_token_count']} "
        f"total={usage['total_token_count']} "
        f"thoughts={usage['thoughts_token_count']} "
        f"cached={usage['cached_content_token_count']} "
        f"trafficType={usage['traffic_type']}"
    )


def _is_retryable_error(exc: Exception) -> bool:
    code = _get_attr(exc, "code") or _get_attr(exc, "status_code")
    if code in app_config.LLM_RETRYABLE_STATUS_CODES:
        return True
    message = str(exc).lower()
    return any(
        token in message
        for token in (
            "resource exhausted",
            "resource_exhausted",
            "too many requests",
            "timed out",
            "timeout",
            "internal error",
            "service unavailable",
            "bad gateway",
        )
    )


def _should_switch_to_priority(exc: Exception) -> bool:
    code = _get_attr(exc, "code") or _get_attr(exc, "status_code")
    if code != 429:
        return False
    message = str(exc).lower()
    return "resource exhausted" in message or "resource_exhausted" in message


def _compute_retry_wait_seconds(attempt_number: int) -> float:
    base_wait = app_config.PRIORITY_APP_RETRY_WAIT_SECONDS[
        min(
            attempt_number - 1,
            len(app_config.PRIORITY_APP_RETRY_WAIT_SECONDS) - 1,
        )
    ]
    return base_wait + random.uniform(0.0, 1.0)


def _is_visible_stream_chunk(chunk: StreamChunk) -> bool:
    return bool(chunk.text_delta or chunk.thought_delta or chunk.grounding_metadata)


def _build_generate_result(
    *,
    response: types.GenerateContentResponse,
    route: str,
    app_retry_count: int,
) -> GenerateResult:
    return GenerateResult(
        text=_extract_response_text(response),
        usage_metadata=_get_attr(response, "usage_metadata"),
        grounding_metadata=_extract_grounding_metadata(response),
        route=route,
        app_retry_count=app_retry_count,
        # Response level SDK response contains headers
        sdk_http_headers=_normalize_headers(
            _get_attr(_get_attr(response, "sdk_http_response"), "headers")
        ),
        response=response,
    )


def _generate_once(
    *,
    client: genai.Client,
    model_id: str,
    contents: Any,
    config: types.GenerateContentConfig | dict[str, Any] | None,
    route: str,
    app_retry_count: int,
) -> GenerateResult:
    response = client.models.generate_content(
        model=model_id,
        contents=contents,
        config=_clone_config(config),
    )
    return _build_generate_result(
        response=response,
        route=route,
        app_retry_count=app_retry_count,
    )


def _stream_once(
    *,
    client: genai.Client,
    model_id: str,
    contents: Any,
    config: types.GenerateContentConfig | dict[str, Any] | None,
    route: str,
    app_retry_count: int,
) -> Iterator[StreamChunk]:
    stream = client.models.generate_content_stream(
        model=model_id,
        contents=contents,
        config=_clone_config(config),
    )
    for response in stream:
        usage_metadata = _get_attr(response, "usage_metadata")
        grounding_metadata = _extract_grounding_metadata(response)
        headers = _normalize_headers(
            _get_attr(_get_attr(response, "sdk_http_response"), "headers")
        )
        candidates = _get_attr(response, "candidates", []) or []
        if not candidates:
            if usage_metadata or grounding_metadata:
                yield StreamChunk(
                    usage_metadata=usage_metadata,
                    grounding_metadata=grounding_metadata,
                    route=route,
                    app_retry_count=app_retry_count,
                    sdk_http_headers=headers,
                )
            continue

        candidate = candidates[0]
        parts = _get_attr(_get_attr(candidate, "content"), "parts", []) or []
        emitted = False
        for part in parts:
            thought_delta = ""
            text_delta = ""
            thought_value = _get_attr(part, "thought")
            # Logic for thinking models
            if isinstance(thought_value, str) and thought_value:
                thought_delta = thought_value
            elif thought_value is True:
                # Fallback if thought is a boolean but text contains the reasoning
                thought_delta = _get_attr(part, "text", "") or ""
            else:
                text_delta = _get_attr(part, "text", "") or ""

            if not (text_delta or thought_delta):
                continue

            yield StreamChunk(
                text_delta=text_delta,
                thought_delta=thought_delta,
                usage_metadata=usage_metadata,
                grounding_metadata=grounding_metadata,
                route=route,
                app_retry_count=app_retry_count,
                sdk_http_headers=headers,
            )
            # Metadata is only yielded once
            usage_metadata = None
            grounding_metadata = None
            headers = None
            emitted = True

        if not emitted and (usage_metadata or grounding_metadata):
            yield StreamChunk(
                usage_metadata=usage_metadata,
                grounding_metadata=grounding_metadata,
                route=route,
                app_retry_count=app_retry_count,
                sdk_http_headers=headers,
            )


def generate_content_with_route(
    *,
    llm_clients: LLMClients,
    model_id: str,
    contents: Any,
    config: types.GenerateContentConfig | dict[str, Any] | None = None,
    mode: str = "normal",
    logger: LoggerFn | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> GenerateResult:
    _log(
        logger,
            (
                f"[LLM] route={app_config.LLM_ROUTE_STANDARD} mode={mode} "
                f"model={model_id} location={llm_clients.location}"
            ),
        )
    try:
        result = _generate_once(
            client=llm_clients.standard_client,
            model_id=model_id,
            contents=contents,
            config=config,
            route=app_config.LLM_ROUTE_STANDARD,
            app_retry_count=0,
        )
        usage_log = format_usage_log(result.usage_metadata)
        if usage_log:
            _log(logger, usage_log)
        return result
    except Exception as exc:
        if not _should_switch_to_priority(exc):
            raise
        _log(logger, "[LLM] 429 on standard. switching to priority.", "warning")

    last_exc: Exception | None = None
    for attempt_index in range(app_config.PRIORITY_APP_RETRY_COUNT + 1):
        _log(
            logger,
            (
                f"[LLM] route={app_config.LLM_ROUTE_PRIORITY} mode={mode} "
                f"attempt={attempt_index} model={model_id} location={llm_clients.location}"
            ),
        )
        try:
            result = _generate_once(
                client=llm_clients.priority_client,
                model_id=model_id,
                contents=contents,
                config=config,
                route=app_config.LLM_ROUTE_PRIORITY,
                app_retry_count=attempt_index,
            )
            usage_log = format_usage_log(result.usage_metadata)
            if usage_log:
                _log(logger, usage_log)
            return result
        except Exception as exc:
            last_exc = exc
            if attempt_index >= app_config.PRIORITY_APP_RETRY_COUNT:
                break
            if not _is_retryable_error(exc):
                raise
            wait_seconds = _compute_retry_wait_seconds(attempt_index + 1)
            _log(
                logger,
                (
                    f"[LLM] priority retry {attempt_index + 1}/"
                    f"{app_config.PRIORITY_APP_RETRY_COUNT} wait={wait_seconds:.1f}s"
                ),
                "warning",
            )
            sleep_fn(wait_seconds)

    if last_exc is not None:
        raise last_exc
    raise RuntimeError("Priority route finished without a result.")


def generate_content_stream_with_route(
    *,
    llm_clients: LLMClients,
    model_id: str,
    contents: Any,
    config: types.GenerateContentConfig | dict[str, Any] | None = None,
    mode: str = "normal",
    logger: LoggerFn | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> Iterator[StreamChunk]:
    _log(
        logger,
            (
                f"[LLM] route={app_config.LLM_ROUTE_STANDARD} mode={mode} "
                f"model={model_id} location={llm_clients.location}"
            ),
        )

    emitted_visible_chunk = False
    latest_usage_metadata = None
    try:
        for chunk in _stream_once(
            client=llm_clients.standard_client,
                model_id=model_id,
                contents=contents,
                config=config,
                route=app_config.LLM_ROUTE_STANDARD,
                app_retry_count=0,
            ):
            if chunk.usage_metadata:
                latest_usage_metadata = chunk.usage_metadata
            if _is_visible_stream_chunk(chunk):
                emitted_visible_chunk = True
            yield chunk
        usage_log = format_usage_log(latest_usage_metadata)
        if usage_log:
            _log(logger, usage_log)
        return
    except Exception as exc:
        if emitted_visible_chunk or not _should_switch_to_priority(exc):
            raise
        _log(logger, "[LLM] 429 on standard. switching to priority.", "warning")

    last_exc: Exception | None = None
    for attempt_index in range(app_config.PRIORITY_APP_RETRY_COUNT + 1):
        _log(
            logger,
            (
                f"[LLM] route={app_config.LLM_ROUTE_PRIORITY} mode={mode} "
                f"attempt={attempt_index} model={model_id} location={llm_clients.location}"
            ),
        )
        emitted_priority_visible_chunk = False
        latest_priority_usage = None
        try:
            for chunk in _stream_once(
                client=llm_clients.priority_client,
                model_id=model_id,
                contents=contents,
                config=config,
                route=app_config.LLM_ROUTE_PRIORITY,
                app_retry_count=attempt_index,
            ):
                if chunk.usage_metadata:
                    latest_priority_usage = chunk.usage_metadata
                if _is_visible_stream_chunk(chunk):
                    emitted_priority_visible_chunk = True
                yield chunk
            usage_log = format_usage_log(latest_priority_usage)
            if usage_log:
                _log(logger, usage_log)
            return
        except Exception as exc:
            last_exc = exc
            if (
                emitted_priority_visible_chunk
                or attempt_index >= app_config.PRIORITY_APP_RETRY_COUNT
            ):
                break
            if not _is_retryable_error(exc):
                raise
            wait_seconds = _compute_retry_wait_seconds(attempt_index + 1)
            _log(
                logger,
                (
                    f"[LLM] priority retry {attempt_index + 1}/"
                    f"{app_config.PRIORITY_APP_RETRY_COUNT} wait={wait_seconds:.1f}s"
                ),
                "warning",
            )
            sleep_fn(wait_seconds)

    if last_exc is not None:
        raise last_exc
    raise RuntimeError("Priority streaming route finished without a result.")