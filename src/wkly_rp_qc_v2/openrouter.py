import json
import urllib.error
import urllib.request


ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"


def _redact(value: object, credential: str) -> object:
    if isinstance(value, str):
        return value.replace(credential, "[REDACTED]")
    if isinstance(value, list):
        return [_redact(item, credential) for item in value]
    if isinstance(value, dict):
        return {key: _redact(item, credential) for key, item in value.items()}
    return value


class OpenRouterError(RuntimeError):
    def __init__(self, message: str, *, raw_response: object = None, usage: dict | None = None, api_key: str = ""):
        super().__init__(message.replace(api_key, "[REDACTED]") if api_key else message)
        self.raw_response = _redact(raw_response, api_key) if api_key else raw_response
        self.usage = usage or {}


def _decode_response(data: bytes, api_key: str) -> object:
    text = data.decode("utf-8", errors="replace")
    try:
        return _redact(json.loads(text), api_key)
    except json.JSONDecodeError:
        return _redact(text, api_key)


def complete_json(messages: list[dict], schema: dict, *, api_key: str, model: str) -> dict:
    body = json.dumps({
        "model": model,
        "messages": messages,
        "max_tokens": 12000,
        "provider": {"require_parameters": True},
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "weekly_report_quality", "strict": True, "schema": schema},
        },
    }).encode("utf-8")
    request = urllib.request.Request(
        ENDPOINT,
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            raw = _decode_response(response.read(), api_key)
    except urllib.error.HTTPError as error:
        raw = _decode_response(error.read(), api_key)
        raise OpenRouterError(f"OpenRouter HTTP {error.code}: {raw}", raw_response=raw, api_key=api_key) from None
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        reason = getattr(error, "reason", error)
        raise OpenRouterError(f"OpenRouter request failed: {reason}", api_key=api_key) from None

    if not isinstance(raw, dict):
        raise OpenRouterError("OpenRouter returned a non-JSON response", raw_response=raw, api_key=api_key)
    usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
    choices = raw.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise OpenRouterError("OpenRouter response is missing choices", raw_response=raw, usage=usage, api_key=api_key)
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict):
        raise OpenRouterError("OpenRouter response is missing message content", raw_response=raw, usage=usage, api_key=api_key)
    if message.get("refusal"):
        raise OpenRouterError(f"OpenRouter refusal: {message['refusal']}", raw_response=raw, usage=usage, api_key=api_key)
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise OpenRouterError("OpenRouter response is missing content", raw_response=raw, usage=usage, api_key=api_key)
    if choice.get("finish_reason") == "length":
        raise OpenRouterError("OpenRouter response was truncated at the token limit", raw_response=raw, usage=usage, api_key=api_key)
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as error:
        raise OpenRouterError(f"OpenRouter content is not valid JSON: {error.msg}", raw_response=raw, usage=usage, api_key=api_key) from None
    return {"payload": payload, "raw_response": raw, "usage": usage}
