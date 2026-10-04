import json
from pathlib import Path
import subprocess
import sys

import pytest

from serein.application import Application
from serein.config import Settings, load_settings
from serein.core import Store
from serein.core.search import build_index
from serein.extensions import Contributions


def test_disabled_extension_never_loads_or_contributes(tmp_path):
    def unavailable_factory(*args):
        raise AssertionError("Disabled extension was loaded")

    app = Application(Settings(tmp_path / "absent.db", extensions={"handoff": {"enabled": False}}),
                      extension_factories={"handoff": unavailable_factory})
    assert app.capabilities() == {"extensions": [], "tools": ["memory_materials", "memory_read", "memory_search", "source_messages", "source_read"],
                                  "prompt_hooks": [], "jobs": []}
    assert not (tmp_path / "absent.db").exists()


def test_enabled_extensions_register_without_starting_work(tmp_path):
    calls = []

    def factory(services, options):
        calls.append("loaded")
        return Contributions(tools={"resume_context": lambda: options["limit"]},
                             prompt_hooks={"resume_hint": lambda: "resume"},
                             jobs={"resume_cleanup": lambda: calls.append("job")})

    app = Application(Settings(tmp_path / "absent.db", extensions={"handoff": {"enabled": True, "limit": 3}}),
                      extension_factories={"handoff": factory})
    assert calls == ["loaded"]
    assert app.capabilities()["jobs"] == ["resume_cleanup"]
    assert app.contributions.tools["resume_context"]() == 3
    with pytest.raises(ValueError, match="not installed"):
        Application(Settings(tmp_path / "absent.db", extensions={"unknown": {"enabled": True}}))
    with pytest.raises(ValueError, match="duplicates"):
        Application(Settings(tmp_path / "absent.db", extensions={"handoff": {"enabled": True}}),
                    extension_factories={"handoff": lambda *_: Contributions(tools={"memory_read": lambda: None})})


def test_explicit_profile_paths_and_unknown_settings(tmp_path, monkeypatch):
    config_dir = tmp_path / "profile"
    config_dir.mkdir()
    config = config_dir / "config.toml"
    config.write_text('[storage]\ndatabase="data/serein.db"\n[extensions.handoff]\nenabled=false\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    settings = load_settings(config)
    assert settings.database == config_dir / "data" / "serein.db"
    assert settings.index is None
    config.write_text('[storage]\ndatabase="data.db"\n[extensions.handoff]\nenabled="false"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="boolean"):
        load_settings(config)
    config.write_text('[storage]\ndatabase="data.db"\n[extension.handoff]\nenabled=true\n', encoding="utf-8")
    with pytest.raises(ValueError, match="Unknown"):
        load_settings(config)


def test_cli_profile_uses_same_core_as_explicit_paths(tmp_path):
    database, index = tmp_path / "canonical.db", tmp_path / "search.db"
    with Store(database) as store:
        store.create("event_a", "event", "归航", "雨天归航")
    build_index(database, index)
    config = tmp_path / "config.toml"
    config.write_text('[storage]\ndatabase="canonical.db"\nindex="search.db"\n', encoding="utf-8")
    # PYTHONPATH points only to the shared source, not any private checkout.
    import os
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"), "PYTHONIOENCODING": "utf-8"}

    def run(*args):
        proc = subprocess.run([sys.executable, "-m", "serein", *map(str, args)], cwd=tmp_path,
                              env=env, capture_output=True, text=True, encoding="utf-8", check=True)
        return json.loads(proc.stdout)

    assert run("--config", config, "read", "event_a") == run("read", database, "event_a")
    assert run("--config", config, "search", "归航") == run("search", database, index, "归航")
    assert run("--config", config, "materials", "narrative_missing")["status"] == "missing"
    assert run("--config", config, "capabilities")["extensions"] == []


def test_automatic_recall_method_uses_one_effective_snapshot(tmp_path, monkeypatch):
    from dataclasses import replace
    from types import SimpleNamespace
    from serein.application import Services

    settings = Settings(tmp_path / 'unused.db')
    effective = replace(settings, embedding={'endpoint':'https://synthetic.example/embeddings'})
    resolved, calls = [], []

    def resolve(base):
        resolved.append(base)
        return effective

    class FakeRecall:
        policy = SimpleNamespace(direct_threshold=.65)

        def __init__(self, current):
            assert current is effective

        def run(self, query, **options):
            calls.append(options)
            return {'status':'no_match'}

    monkeypatch.setattr('serein.configured_models.effective_settings', resolve)
    monkeypatch.setattr('serein.application.Recall', FakeRecall)
    services = Services(settings)
    services.recall('synthetic', method=None, min_cosine=.5)
    assert resolved == [settings]
    assert calls[-1]['method'] == 'semantic'
    services.recall('synthetic', method='lexical')
    assert calls[-1]['method'] == 'lexical'
    # Internal callers that omit method keep the engine's historical default.
    services.recall('synthetic')
    assert 'method' not in calls[-1]
