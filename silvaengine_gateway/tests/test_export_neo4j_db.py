#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Unit tests for helper functions in export_neo4j_db.py.

Verifies:
1. _props_cypher() produces valid Cypher `+= {map}` syntax (not bare `{map}`).
2. _props_cypher() returns "" for empty props (so SET clause is omitted).
3. _bt() backtick-quotes labels/types correctly.
4. _cypher_value() renders strings, numbers, booleans, None, lists.
5. Node and relationship statement generation uses correct SET syntax.
"""
import pytest

from silvaengine_gateway.tests.export_neo4j_db import (
    _props_cypher,
    _bt,
    _cypher_value,
)


# ---------------------------------------------------------------------------
# _props_cypher
# ---------------------------------------------------------------------------

class TestPropsCypher:
    def test_empty_dict_returns_empty(self):
        assert _props_cypher({}) == ""

    def test_none_returns_empty(self):
        assert _props_cypher(None) == ""

    def test_simple_dict_uses_merge_operator(self):
        result = _props_cypher({"name": "test"})
        # Must contain "+= " — the Cypher merge-assignment operator
        assert "+= {" in result
        assert "name" in result
        assert "'test'" in result

    def test_no_bare_brace_without_operator(self):
        """The old bug: returned ' {map}' which produced invalid 'SET n {map}'."""
        result = _props_cypher({"name": "test"})
        # The result should NOT start with a bare space + brace
        assert not result.startswith(" {")
        assert result.startswith(" += {")

    def test_multiple_props(self):
        result = _props_cypher({"name": "test", "code": "CX"})
        assert "+= {" in result
        assert "'test'" in result
        assert "'CX'" in result

    def test_props_are_sorted(self):
        result = _props_cypher({"z": 1, "a": 2})
        # 'a' should come before 'z'
        assert result.index("a") < result.index("z")

    def test_integer_value(self):
        result = _props_cypher({"count": 42})
        assert "42" in result
        assert "+= {" in result

    def test_float_value(self):
        result = _props_cypher({"price": 99.5})
        assert "99.5" in result

    def test_boolean_values(self):
        result = _props_cypher({"active": True, "deleted": False})
        assert "true" in result
        assert "false" in result

    def test_none_value(self):
        result = _props_cypher({"note": None})
        assert "NULL" in result or "null" in result

    def test_list_value(self):
        result = _props_cypher({"tags": ["a", "b"]})
        assert "['a', 'b']" in result or '["a", "b"]' in result

    def test_unicode_value(self):
        result = _props_cypher({"name": "四川航空"})
        assert "四川航空" in result

    def test_backtick_quoted_key(self):
        """Keys with special chars should be backtick-quoted."""
        result = _props_cypher({"my-key": "val"})
        assert "`my-key`" in result


# ---------------------------------------------------------------------------
# _bt (backtick quoting)
# ---------------------------------------------------------------------------

class TestBacktick:
    def test_simple_label(self):
        assert _bt("Airline") == "Airline"

    def test_label_with_dash(self):
        assert _bt("my-label") == "`my-label`"

    def test_label_with_space(self):
        assert _bt("my label") == "`my label`"

    def test_label_with_special_char(self):
        assert _bt("label.name") == "`label.name`"

    def test_empty_string(self):
        assert _bt("") == "``"


# ---------------------------------------------------------------------------
# _cypher_value
# ---------------------------------------------------------------------------

class TestCypherValue:
    def test_string(self):
        assert _cypher_value("hello") == "'hello'"

    def test_string_with_quote(self):
        result = _cypher_value("it's")
        # _cypher_value escapes ' as \' (backslash-quote)
        assert "\\'" in result or "''" in result  # Either escape style is valid

    def test_string_with_backslash(self):
        result = _cypher_value("path\\to")
        # Should escape backslash for Cypher
        assert "\\" in result

    def test_integer(self):
        assert _cypher_value(42) == "42"

    def test_negative_int(self):
        assert _cypher_value(-7) == "-7"

    def test_float(self):
        assert _cypher_value(3.14) == "3.14"

    def test_boolean_true(self):
        assert _cypher_value(True) == "true"

    def test_boolean_false(self):
        assert _cypher_value(False) == "false"

    def test_none(self):
        # _cypher_value uses lowercase 'null' for Cypher
        assert _cypher_value(None).lower() == "null"

    def test_list(self):
        result = _cypher_value(["a", "b"])
        assert "['a', 'b']" in result

    def test_unicode(self):
        result = _cypher_value("中國國際航空")
        assert "中國國際航空" in result


# ---------------------------------------------------------------------------
# Integration: full statement generation (syntax correctness)
# ---------------------------------------------------------------------------

class TestStatementGeneration:
    def test_node_statement_uses_merge_set_syntax(self):
        """Simulate what export_nodes does and verify the Cypher is valid."""
        label = "Airline"
        element_id = "4:abc123:0"
        props = {"code": "CX", "name": "Cathay Pacific"}
        props_cypher = _props_cypher(props)

        stmt = (
            f"MERGE (n:{_bt(label)} {{_element_id: {_cypher_value(element_id)}}})"
            f" SET n{props_cypher}"
            f" SET n._element_id = {_cypher_value(element_id)};"
        )
        # Must NOT contain "SET n {" (bare brace — the old bug)
        assert "SET n {" not in stmt
        # Must contain "SET n += {" (valid merge syntax)
        assert "SET n += {" in stmt
        # Must end with semicolon
        assert stmt.endswith(";")

    def test_node_statement_no_props(self):
        """Node with no properties should NOT emit bare 'SET n' (invalid Cypher)."""
        label = "Airport"
        element_id = "4:abc123:1"
        props_cypher = _props_cypher({})

        # Simulate the fixed export logic
        set_props = f" SET n{props_cypher}" if props_cypher else ""
        stmt = (
            f"MERGE (n:{_bt(label)} {{_element_id: {_cypher_value(element_id)}}})"
            f"{set_props}"
            f" SET n._element_id = {_cypher_value(element_id)};"
        )
        # Must NOT contain bare "SET n SET" (the old bug when props_cypher is "")
        assert "SET n SET" not in stmt
        # Must still set _element_id
        assert "SET n._element_id = " in stmt
        # Must end with semicolon
        assert stmt.endswith(";")

    def test_relationship_statement_no_props(self):
        """Relationship with no properties must NOT have 'SET r;'."""
        a_match = "MATCH (a {_element_id: '4:abc:0'})"
        b_match = "MATCH (b {_element_id: '4:abc:1'})"
        rel_type = "ARRIVES_AT"
        props_cypher = _props_cypher({})  # Empty — no props

        set_clause = f" SET r{props_cypher};" if props_cypher else ";"
        stmt = f"{a_match} {b_match} MERGE (a)-[r:{_bt(rel_type)}]->(b){set_clause}"

        # Must NOT contain "SET r;" (the old bug)
        assert "SET r;" not in stmt
        # Must end with just ";"
        assert stmt.endswith(";")
        # Should be: ...MERGE (a)-[r:ARRIVES_AT]->(b);
        assert "MERGE (a)-[r:ARRIVES_AT]->(b);" in stmt

    def test_relationship_statement_with_props(self):
        """Relationship WITH properties should use 'SET r += {map};'."""
        a_match = "MATCH (a {_element_id: '4:abc:0'})"
        b_match = "MATCH (b {_element_id: '4:abc:1'})"
        rel_type = "HAS_PRICE"
        props = {"amount": 1090.0, "currency": "TWD"}
        props_cypher = _props_cypher(props)

        set_clause = f" SET r{props_cypher};" if props_cypher else ";"
        stmt = f"{a_match} {b_match} MERGE (a)-[r:{_bt(rel_type)}]->(b){set_clause}"

        # Must contain "SET r += {"
        assert "SET r += {" in stmt
        # Must contain the property values
        assert "1090.0" in stmt
        assert "'TWD'" in stmt
        # Must end with semicolon
        assert stmt.endswith(";")