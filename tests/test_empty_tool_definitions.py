"""Explicit zero-tool agents cannot regain connector capabilities through Tool Search."""
import pytest


@pytest.mark.parametrize('quiet', [False, True])
def test_explicit_empty_tools_do_not_add_remote_connector_bridge(monkeypatch, quiet):
    import model_tools
    from tools import tool_search

    model_tools._clear_tool_defs_cache()
    monkeypatch.setattr(tool_search, 'connections_in_scope', lambda incoming: True)
    monkeypatch.setattr(tool_search, 'load_config', lambda: tool_search.ToolSearchConfig.from_raw({'enabled': 'on'}))
    monkeypatch.setattr(model_tools, '_resolve_active_context_length', lambda: 8192)

    assert model_tools.get_tool_definitions(enabled_toolsets=[], quiet_mode=quiet) == []


@pytest.mark.parametrize('selection', [None, ['terminal']])
def test_other_selections_keep_remote_connector_bridge(monkeypatch, selection):
    import model_tools
    from tools import tool_search

    model_tools._clear_tool_defs_cache()
    monkeypatch.setattr(model_tools.registry, 'get_definitions', lambda *args, **kwargs: [])
    monkeypatch.setattr(tool_search, 'connections_in_scope', lambda incoming: True)
    monkeypatch.setattr(tool_search, 'load_config', lambda: tool_search.ToolSearchConfig.from_raw({'enabled': 'on'}))
    monkeypatch.setattr(model_tools, '_resolve_active_context_length', lambda: 8192)

    definitions = model_tools.get_tool_definitions(enabled_toolsets=selection, quiet_mode=True)
    assert {'tool_search', 'tool_describe', 'tool_call'} <= {item['function']['name'] for item in definitions}
