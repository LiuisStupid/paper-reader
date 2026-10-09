"""Thin async client for the local LLM.

Speaks the Anthropic Messages protocol (what the Claude CLI and gateways such as
DeepSeek's /anthropic endpoint use) and, as a fallback, the OpenAI chat
completions protocol so a plain local runtime (ollama, vllm, lm-studio) works
too.  Streaming is exposed as a normalised async iterator of events:

    {"type": "thinking", "text": "..."}    # reasoning tokens (may be absent)
    {"type": "text",     "text": "..."}    # answer tokens
    {"type": "done",     "usage": {...}, "stop_reason": "..."}
    {"type": "error",    "message": "..."}
"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx

from . import config

TIMEOUT = httpx.Timeout(connect=15.0, read=600.0, write=60.0, pool=15.0)


class LLMError(RuntimeError):
    pass


def _headers(cfg: Dict[str, Any]) -> Dict[str, str]:
    h = {"content-type": "application/json", "accept": "text/event-stream"}
    token = cfg.get("auth_token")
    key = cfg.get("api_key")
    if cfg.get("protocol") == "openai":
        if token or key:
            h["authorization"] = "Bearer %s" % (token or key)
        return h
    h["anthropic-version"] = "2023-06-01"
    # The gateway here accepts either header; send both so any Anthropic-shaped
    # proxy works without extra configuration.
    if key:
        h["x-api-key"] = key
    if token:
        h["authorization"] = "Bearer %s" % token
        if not key:
            h["x-api-key"] = token
    return h


def _endpoint(cfg: Dict[str, Any]) -> str:
    base = cfg["base_url"]
    if cfg.get("protocol") == "openai":
        return base + "/v1/chat/completions"
    return base + "/v1/messages"


def _build_payload(
    cfg: Dict[str, Any],
    messages: List[Dict[str, str]],
    system: Optional[str],
    model: str,
    max_tokens: int,
    temperature: float,
    thinking: Optional[bool],
    stream: bool,
) -> Dict[str, Any]:
    if cfg.get("protocol") == "openai":
        msgs: List[Dict[str, str]] = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.extend(messages)
        payload: Dict[str, Any] = {
            "model": model,
            "messages": msgs,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": stream,
        }
        return payload

    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": stream,
    }
    if system:
        payload["system"] = system
    # Only send the thinking flag when the model is known to accept it; on the
    # public Anthropic API an unexpected `thinking` field is a hard 400.
    if thinking is not None and cfg.get("supports_thinking_toggle", True):
        payload["thinking"] = {"type": "enabled" if thinking else "disabled"}
    return payload


async def stream_chat(
    messages: List[Dict[str, str]],
    system: Optional[str] = None,
    model: Optional[str] = None,
    max_tokens: int = 4096,
    temperature: float = 0.3,
    thinking: Optional[bool] = None,
    cfg: Optional[Dict[str, Any]] = None,
) -> AsyncIterator[Dict[str, Any]]:
    cfg = cfg or config.resolve_llm()
    model = model or cfg["model"]
    payload = _build_payload(
        cfg, messages, system, model, max_tokens, temperature, thinking, stream=True
    )

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            async with client.stream(
                "POST", _endpoint(cfg), headers=_headers(cfg), json=payload
            ) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", "replace")
                    yield {"type": "error", "message": _explain(resp.status_code, body)}
                    return
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw or raw == "[DONE]":
                        continue
                    try:
                        evt = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    for out in _translate_event(evt, cfg.get("protocol", "anthropic")):
                        yield out
        except httpx.HTTPError as exc:
            yield {"type": "error", "message": "无法连接大模型服务：%s" % exc}


def _translate_event(evt: Dict[str, Any], protocol: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []

    if protocol == "openai":
        for choice in evt.get("choices") or []:
            piece = (choice.get("delta") or {}).get("content")
            if piece:
                out.append({"type": "text", "text": piece})
        if evt.get("usage"):
            out.append({"type": "done", "usage": evt["usage"], "stop_reason": "end_turn"})
        return out

    etype = evt.get("type")
    if etype == "content_block_delta":
        delta = evt.get("delta") or {}
        dtext = delta.get("text")
        if dtext:
            out.append({"type": "text", "text": dtext})
        dthink = delta.get("thinking")
        if dthink:
            out.append({"type": "thinking", "text": dthink})
    elif etype == "message_delta":
        out.append(
            {
                "type": "done",
                "usage": evt.get("usage") or {},
                "stop_reason": (evt.get("delta") or {}).get("stop_reason"),
            }
        )
    elif etype == "error":
        err = evt.get("error") or {}
        out.append({"type": "error", "message": err.get("message") or str(err)})
    return out


async def complete(
    prompt: str,
    system: Optional[str] = None,
    model: Optional[str] = None,
    max_tokens: int = 4096,
    temperature: float = 0.2,
    thinking: Optional[bool] = False,
    cfg: Optional[Dict[str, Any]] = None,
) -> str:
    """Non-streaming helper -- used for translation, where we want the whole
    answer before writing it to the cache."""
    chunks: List[str] = []
    async for evt in stream_chat(
        [{"role": "user", "content": prompt}],
        system=system,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        thinking=thinking,
        cfg=cfg,
    ):
        if evt["type"] == "text":
            chunks.append(evt["text"])
        elif evt["type"] == "error":
            raise LLMError(evt["message"])
    return "".join(chunks).strip()


def _explain(status: int, body: str) -> str:
    snippet = body.strip()[:400]
    if status == 401:
        return "鉴权失败 (401)：API Key / Auth Token 无效。请在「设置」中检查。"
    if status == 404:
        return "接口不存在 (404)：Base URL 可能不正确。当前应指向形如 https://host/anthropic 的地址。"
    if status == 429:
        return "请求过于频繁 (429)：稍后再试或降低并发。"
    return "大模型服务返回 %d：%s" % (status, snippet)


async def check_connection(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Used by the agent bootstrap and the settings panel."""
    cfg = cfg or config.resolve_llm()
    if not (cfg.get("auth_token") or cfg.get("api_key")):
        return {"ok": False, "error": "未检测到 API Key / Auth Token"}
    try:
        text = await complete(
            "ping", max_tokens=16, temperature=0, thinking=False, cfg=cfg
        )
        return {"ok": True, "model": cfg["model"], "reply": text[:80]}
    except LLMError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - surfaced to the user verbatim
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
