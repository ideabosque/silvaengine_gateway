#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Unit tests for _quote_sql_string in export_pg_db.py.

Verifies that:
1. JSONB values (dict/list) are serialized with json.dumps (valid JSON).
2. Backslashes are NOT doubled (standard_conforming_strings=on).
3. Single quotes in string values are properly escaped via ''.
4. None / bool / int / float are handled correctly.
5. Round-trip: output can be unescaped back to the original value.
6. Edge cases: strings with backslashes, quotes, JSON with nested quotes.
"""
import json
import ast
import re

import pytest

from silvaengine_gateway.tests.export_pg_db import _quote_sql_string


def _unquote_sql_string(sql_val):
    """Reverse of _quote_sql_string for string values: strip outer quotes and unescape ''."""
    assert sql_val.startswith("'") and sql_val.endswith("'"), f"Not a SQL string: {sql_val}"
    inner = sql_val[1:-1]
    # With standard_conforming_strings=on, backslashes are literal.
    # Only '' needs unescaping to '.
    return inner.replace("''", "'")


class TestNoneAndPrimitives:
    def test_none(self):
        assert _quote_sql_string(None) == "NULL"

    def test_true(self):
        assert _quote_sql_string(True) == "true"

    def test_false(self):
        assert _quote_sql_string(False) == "false"

    def test_int(self):
        assert _quote_sql_string(42) == "42"

    def test_negative_int(self):
        assert _quote_sql_string(-7) == "-7"

    def test_float(self):
        assert _quote_sql_string(3.14) == "3.14"

    def test_zero(self):
        assert _quote_sql_string(0) == "0"


class TestPlainStrings:
    def test_simple_string(self):
        result = _quote_sql_string("hello world")
        assert result == "'hello world'"
        assert _unquote_sql_string(result) == "hello world"

    def test_string_with_single_quote(self):
        result = _quote_sql_string("it's a test")
        assert "''" in result
        assert _unquote_sql_string(result) == "it's a test"

    def test_string_with_double_quote(self):
        result = _quote_sql_string('say "hello"')
        assert result == "'say \"hello\"'"
        assert _unquote_sql_string(result) == 'say "hello"'

    def test_string_with_backslash(self):
        """Backslashes must NOT be doubled (standard_conforming_strings=on)."""
        result = _quote_sql_string("path\\to\\file")
        assert result == "'path\\to\\file'"
        assert "\\\\" not in result  # No doubled backslashes
        assert _unquote_sql_string(result) == "path\\to\\file"

    def test_string_with_backslash_and_quote(self):
        result = _quote_sql_string("it's a \\ backslash")
        assert _unquote_sql_string(result) == "it's a \\ backslash"

    def test_empty_string(self):
        assert _quote_sql_string("") == "''"

    def test_string_with_newline(self):
        result = _quote_sql_string("line1\nline2")
        assert _unquote_sql_string(result) == "line1\nline2"

    def test_unicode_string(self):
        result = _quote_sql_string("搜索关键词")
        assert _unquote_sql_string(result) == "搜索关键词"


class TestJsonbDict:
    def test_simple_dict(self):
        val = {"key": "value"}
        result = _quote_sql_string(val)
        inner = _unquote_sql_string(result)
        parsed = json.loads(inner)
        assert parsed == val

    def test_nested_dict(self):
        val = {"text": {"format": {"type": "text", "schema": []}}}
        result = _quote_sql_string(val)
        inner = _unquote_sql_string(result)
        parsed = json.loads(inner)
        assert parsed == val

    def test_dict_with_special_chars(self):
        val = {"api_key": "sk-ant...SQAA\""}
        result = _quote_sql_string(val)
        inner = _unquote_sql_string(result)
        parsed = json.loads(inner)
        assert parsed == val

    def test_dict_with_backslash_in_value(self):
        """JSONB with backslash in string value must survive round-trip."""
        val = {"path": "C:\\Users\\test"}
        result = _quote_sql_string(val)
        inner = _unquote_sql_string(result)
        parsed = json.loads(inner)
        assert parsed == val
        # Verify no doubled backslashes in SQL output
        assert "\\\\\\\\" not in result

    def test_dict_with_single_quote_in_value(self):
        val = {"name": "O'Brien"}
        result = _quote_sql_string(val)
        inner = _unquote_sql_string(result)
        parsed = json.loads(inner)
        assert parsed == val

    def test_dict_with_none_value(self):
        val = {"key": None, "other": 123}
        result = _quote_sql_string(val)
        inner = _unquote_sql_string(result)
        parsed = json.loads(inner)
        assert parsed == val

    def test_dict_with_bool_value(self):
        val = {"flag": True, "other": False}
        result = _quote_sql_string(val)
        inner = _unquote_sql_string(result)
        parsed = json.loads(inner)
        assert parsed == val

    def test_dict_ensure_ascii_false(self):
        """Non-ASCII characters should not be escaped to \\uXXXX."""
        val = {"query": "搜索关键词"}
        result = _quote_sql_string(val)
        assert "搜索关键词" in result  # Raw UTF-8, not \\uXXXX


class TestJsonbList:
    def test_simple_list(self):
        val = ["a", "b", "c"]
        result = _quote_sql_string(val)
        inner = _unquote_sql_string(result)
        parsed = json.loads(inner)
        assert parsed == val

    def test_empty_list(self):
        val = []
        result = _quote_sql_string(val)
        inner = _unquote_sql_string(result)
        parsed = json.loads(inner)
        assert parsed == val

    def test_list_of_dicts(self):
        val = [
            {"name": "google_api_key", "value": "AIzaSy...wcck", "data_type": "string"},
            {"name": "keyword", "value": "ecommerce", "data_type": "string"},
        ]
        result = _quote_sql_string(val)
        inner = _unquote_sql_string(result)
        parsed = json.loads(inner)
        assert parsed == val

    def test_list_with_numbers(self):
        val = [1, 2.5, -3]
        result = _quote_sql_string(val)
        inner = _unquote_sql_string(result)
        parsed = json.loads(inner)
        assert parsed == val

    def test_list_with_quotes_in_strings(self):
        """The exact edge case that caused the original bug."""
        val = ["sk-ant...SQAA\",   \"max_output_tokens"]
        result = _quote_sql_string(val)
        inner = _unquote_sql_string(result)
        parsed = json.loads(inner)
        assert parsed == val


class TestNoPythonRepr:
    """Ensure str() is never used for dict/list — no Python repr artifacts."""

    def test_no_double_single_quotes_in_json(self):
        """The original bug: str(dict) + SQL escape produced {''key''...}."""
        val = {"key": "value"}
        result = _quote_sql_string(val)
        assert "{''" not in result
        assert "''}" not in result

    def test_no_python_none_in_json(self):
        val = {"key": None}
        result = _quote_sql_string(val)
        assert "None" not in result  # Python repr would show None, JSON shows null

    def test_no_python_true_in_json(self):
        val = {"key": True}
        result = _quote_sql_string(val)
        # Python repr would show True, JSON shows true
        inner = _unquote_sql_string(result)
        assert "true" in inner
        assert "True" not in inner


class TestRealWorldData:
    """Test with data patterns from the actual aace_agents table."""

    def test_configuration_with_model_and_tools(self):
        val = {
            "text": {"format": {"type": "text"}},
            "model": "gpt-5",
            "tools": [],
            "reasoning": {"effort": "low"},
            "openai_api_key": "sk-test123",
            "max_output_tokens": 10000,
        }
        result = _quote_sql_string(val)
        inner = _unquote_sql_string(result)
        parsed = json.loads(inner)
        assert parsed == val

    def test_mcp_server_uuids(self):
        val = ["78553767125219982598"]
        result = _quote_sql_string(val)
        inner = _unquote_sql_string(result)
        parsed = json.loads(inner)
        assert parsed == val

    def test_variables_list(self):
        val = [
            {"name": "google_api_key", "value": "AIzaSy...wcck", "data_type": "string"},
            {"name": "keyword", "value": "ecommerce", "data_type": "string"},
        ]
        result = _quote_sql_string(val)
        inner = _unquote_sql_string(result)
        parsed = json.loads(inner)
        assert parsed == val

    def test_instructions_with_xml_and_newlines(self):
        """The instructions field contains XML tags and newlines."""
        val = "## System Prompt\n\n<Step>\n  <UIComponent required=\"true\" />\n</Step>\n\n```\n\n---"
        result = _quote_sql_string(val)
        assert _unquote_sql_string(result) == val