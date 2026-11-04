"""An explicit empty local session selection cannot acquire default plugin capabilities."""
import json

import pytest


def test_empty_session_selection_excludes_auto_enabled_plugins(monkeypatch):
    from gateway.session_policy import build_policy
    from hermes_cli import tools_config

    monkeypatch.setattr(tools_config, '_get_plugin_toolset_keys', lambda: {'fixture-plugin'})
    config = {'platform_toolsets': {'cli': ['terminal']}}
    policy = build_policy({'source': 'tool', 'toolsets': [], 'ignore_rules': True}, config)

    assert policy.toolsets == ()
    assert policy.current_toolsets(config) == ()
    assert json.loads(policy.request_json)['toolsets'] == []
    # A probe's selection does not rewrite the profile's configuration.
    assert config == {'platform_toolsets': {'cli': ['terminal']}}


@pytest.mark.parametrize('selection', [None, ['terminal']])
def test_other_selections_keep_plugin_enrichment(monkeypatch, selection):
    from gateway.session_policy import build_policy
    from hermes_cli import tools_config

    monkeypatch.setattr(tools_config, '_get_plugin_toolset_keys', lambda: {'fixture-plugin'})
    request = {'source': 'tool'}
    if selection is not None:
        request['toolsets'] = selection
    policy = build_policy(request, {'platform_toolsets': {'cli': ['terminal']}})
    assert 'fixture-plugin' in policy.toolsets
