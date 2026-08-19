"""Tests for the Agnes AI provider registration."""

from unittest.mock import patch

from nanobot.config.schema import Config, ProvidersConfig
from nanobot.providers.openai_compat_provider import OpenAICompatProvider
from nanobot.providers.registry import PROVIDERS, find_by_name


def test_agnes_config_field_exists() -> None:
    config = ProvidersConfig()

    assert hasattr(config, "agnes")


def test_agnes_provider_in_registry() -> None:
    specs = {spec.name: spec for spec in PROVIDERS}

    assert "agnes" in specs
    agnes = specs["agnes"]
    assert agnes.backend == "openai_compat"
    assert agnes.env_key == "AGNES_API_KEY"
    assert agnes.display_name == "Agnes AI"
    assert agnes.default_api_base == "https://apihub.agnes-ai.com/v1"


def test_agnes_does_not_hijack_openai_keys() -> None:
    """Agnes keys share the generic ``sk-`` prefix, so key-prefix detection
    must stay off or plain OpenAI keys would be routed to Agnes."""
    spec = find_by_name("agnes")

    assert spec is not None
    assert spec.detect_by_key_prefix == ""
    assert spec.detect_by_base_keyword == "agnes-ai.com"


def test_find_by_name_accepts_agnes_spellings() -> None:
    spec = find_by_name("agnes")

    assert spec is not None
    assert find_by_name("Agnes") is spec


def test_agnes_model_auto_matches_with_default_api_base() -> None:
    config = Config.model_validate({
        "providers": {
            "agnes": {
                "apiKey": "agnes-key",
            },
        },
        "agents": {
            "defaults": {
                "model": "agnes-2.5-flash",
            },
        },
    })

    assert config.get_provider_name("agnes-2.5-flash") == "agnes"
    assert config.get_api_key("agnes-2.5-flash") == "agnes-key"
    assert config.get_api_base("agnes-2.5-flash") == "https://apihub.agnes-ai.com/v1"


def test_agnes_detected_by_api_base_keyword() -> None:
    """An explicit apiBase pointing at the Agnes hub resolves to Agnes even
    when the model name carries no Agnes keyword."""
    config = Config.model_validate({
        "providers": {
            "agnes": {
                "apiKey": "agnes-key",
                "apiBase": "https://apihub.agnes-ai.com/v1",
            },
        },
        "agents": {
            "defaults": {
                "model": "some-unbranded-model",
            },
        },
    })

    assert config.get_api_key("agnes-2.5-flash") == "agnes-key"


def test_agnes_preserves_official_model_name() -> None:
    spec = find_by_name("agnes")
    with patch("nanobot.providers.openai_compat_provider.AsyncOpenAI"):
        provider = OpenAICompatProvider(
            api_key="agnes-key",
            default_model="agnes-2.5-flash",
            spec=spec,
        )

    kwargs = provider._build_kwargs(
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        model="agnes-2.5-flash",
        max_tokens=1024,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    )

    assert kwargs["model"] == "agnes-2.5-flash"


def test_agnes_strips_optional_vendor_prefix() -> None:
    """``agnes/<model>`` is accepted and the bare model goes upstream."""
    spec = find_by_name("agnes")
    with patch("nanobot.providers.openai_compat_provider.AsyncOpenAI"):
        provider = OpenAICompatProvider(
            api_key="agnes-key",
            default_model="agnes/agnes-2.5-flash",
            spec=spec,
        )

    kwargs = provider._build_kwargs(
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        model="agnes/agnes-2.5-flash",
        max_tokens=1024,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    )

    assert kwargs["model"] == "agnes-2.5-flash"
