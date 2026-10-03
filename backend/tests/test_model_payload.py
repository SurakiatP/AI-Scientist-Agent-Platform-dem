import pytest
from pydantic import ValidationError

from scientist.model_payload import (
    ChatMessage,
    ToolDefinition,
    build_chat_completion_body,
    validate_messages,
    validate_tool_choice,
    validate_tools,
    serialized_input_bytes,
)


def test_bounded_native_tool_description_retains_exact_text_and_reservation_bytes():
    description = ('Discover tools.\n  Preserve this indentation.\n' * 30)[:1060]
    assert len(description) == 1060
    tools = [{'type':'function','function':{'name':'tool_search',
        'description':description,'parameters':{'type':'object','properties':{}}}}]
    messages = [{'role':'user','content':'Find a tool'}]
    body = build_chat_completion_body('fixture', messages, 64, tools=tools)
    assert body['tools'][0]['function']['description'] == description
    assert serialized_input_bytes(messages, tools=tools) >= len(description.encode())
    tools[0]['function']['description'] = 'x' * 2049
    with pytest.raises(ValueError):
        validate_tools(tools)


def test_tool_history_serializes_null_assistant_content_and_raw_calls_exactly():
    messages = [
        {"role": "user", "content": "Compute 1 + 1"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_ab12",
                    "type": "function",
                    "function": {"name": "add", "arguments": '{"left":1, "right":1}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_ab12", "content": "2"},
    ]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "add",
                "description": "Add two integers",
                "parameters": {
                    "type": "object",
                    "properties": {"left": {"type": "integer"}, "right": {"type": "integer"}},
                    "required": ["left", "right"],
                    "additionalProperties": False,
                },
            },
        }
    ]

    body = build_chat_completion_body(
        "approved-model",
        messages,
        128,
        tools=tools,
        tool_choice={"type": "function", "function": {"name": "add"}},
        temperature=0.2,
    )

    assert body == {
        "model": "approved-model",
        "messages": messages,
        "max_tokens": 128,
        "tools": tools,
        "tool_choice": {"type": "function", "function": {"name": "add"}},
        "temperature": 0.2,
    }
    assert body["messages"][1]["tool_calls"][0]["function"]["arguments"] == '{"left":1, "right":1}'


def test_message_dto_and_validator_preserve_supported_history_fields():
    message = ChatMessage.model_validate(
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "lookup", "arguments": "{broken"}}
        ]}
    )

    assert message.model_dump(exclude_unset=True) == validate_messages([message.model_dump(exclude_unset=True)])[0]


def test_checkpoint_message_validation_allows_only_a_trailing_pending_tool_suffix():
    pending = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "call_a", "type": "function", "function": {"name": "lookup", "arguments": "{}"}},
            {"id": "call_b", "type": "function", "function": {"name": "lookup", "arguments": "{}"}},
        ],
    }

    assert validate_messages([pending]) == [pending]
    assert validate_messages([pending, {"role": "tool", "tool_call_id": "call_a", "content": "done"}])[-1]["tool_call_id"] == "call_a"
    with pytest.raises(ValueError, match="pending tool"):
        validate_messages([pending, {"role": "user", "content": "interleaved"}])
    with pytest.raises(ValueError, match="order"):
        validate_messages([pending, {"role": "tool", "tool_call_id": "call_b", "content": "out of order"}])


def test_outbound_chat_completion_requires_all_tool_results_as_a_contiguous_batch():
    pending = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "call_a", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}],
    }
    with pytest.raises(ValueError, match="pending tool"):
        build_chat_completion_body("approved-model", [pending], 10)
    with pytest.raises(ValueError, match="pending tool"):
        build_chat_completion_body(
            "approved-model",
            [pending, {"role": "user", "content": "interleaved"}, {"role": "tool", "tool_call_id": "call_a", "content": "done"}],
            10,
        )
    with pytest.raises(ValueError, match="pending tool"):
        serialized_input_bytes([pending])


@pytest.mark.parametrize(
    "messages",
    [
        [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://example.test/x"}}]}],
        [{"role": "tool", "tool_call_id": "call_missing", "content": "x"}],
        [{"role": "assistant", "content": "x", "url": "https://example.test"}],
        [{"role": "assistant", "content": None, "tool_calls": [
            {"id": "bad id", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}
        ]}],
    ],
)
def test_messages_reject_multimodal_unmatched_or_untrusted_fields(messages):
    with pytest.raises((ValidationError, ValueError)):
        validate_messages(messages)


def test_tool_schemas_reject_oversized_and_remote_references():
    too_large = {
        "type": "function",
        "function": {
            "name": "large",
            "parameters": {"type": "object", "description": "x" * 40_000},
        },
    }
    remote_ref = {
        "type": "function",
        "function": {"name": "remote", "parameters": {"type": "object", "properties": {"x": {"$ref": "https://attacker.test/schema.json"}}}},
    }
    malformed = {
        "type": "function",
        "function": {"name": "malformed", "parameters": {"type": "object", "properties": {"x": {"type": "javascript"}}}},
    }

    with pytest.raises((ValidationError, ValueError)):
        validate_tools([too_large])
    with pytest.raises((ValidationError, ValueError)):
        validate_tools([remote_ref])
    with pytest.raises((ValidationError, ValueError)):
        validate_tools([malformed])


def test_tool_choice_and_schema_are_typed_and_fail_closed():
    assert validate_tool_choice({"type": "function", "function": {"name": "lookup"}}) == {
        "type": "function",
        "function": {"name": "lookup"},
    }
    with pytest.raises((ValidationError, ValueError)):
        validate_tool_choice({"type": "function", "function": {"name": "lookup"}, "endpoint": "https://x"})
    with pytest.raises((ValidationError, ValueError)):
        validate_messages([{"role": "user", "content": "ok", "headers": {"Authorization": "x"}}])
