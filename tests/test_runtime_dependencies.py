"""Release/install regressions for the exact official runtime dependency set."""
import json
from pathlib import Path
import subprocess
import tomllib

import pytest
from packaging.requirements import Requirement

from test_installer_menu import manager


ROOT=Path(__file__).resolve().parents[1]


def test_runtime_constraints_ship_and_cover_declared_install_dependencies():
    manifest=json.loads((ROOT/'release-files.json').read_text())
    assert 'constraints-runtime.txt' in manifest
    pins={}
    for line in (ROOT/'constraints-runtime.txt').read_text().splitlines():
        if line and not line.startswith('#'):
            requirement=Requirement(line)
            specifier=list(requirement.specifier)
            assert len(specifier)==1 and specifier[0].operator=='=='
            pins[requirement.name.lower().replace('_','-')]=specifier[0].version
    project=tomllib.loads((ROOT/'pyproject.toml').read_text())
    required=project['build-system']['requires']+['wheel']+project['project']['dependencies']
    for extra in ('http','embedding','mcp','background'):required+=project['project']['optional-dependencies'][extra]
    for value in required:
        requirement=Requirement(value)
        assert requirement.specifier.contains(pins[requirement.name.lower().replace('_','-')])


@pytest.mark.parametrize('failure',['install','check'])
def test_native_dependency_failure_prevents_frontend_build(tmp_path,monkeypatch,failure):
    module=manager();commands=[]
    python=tmp_path/'python';python.touch()
    monkeypatch.setattr(module,'backend',lambda:'local')
    monkeypatch.setattr(module,'local_python',lambda:python)
    monkeypatch.setattr(module,'local_action',lambda *args:None)
    def run(args,**kwargs):
        commands.append(args)
        if (failure=='install' and args[-1]=='.[http,embedding,mcp,background]') or (failure=='check' and args[-1]=='check'):
            raise subprocess.CalledProcessError(1,args)
    monkeypatch.setattr(module,'run',run)
    with pytest.raises(subprocess.CalledProcessError):module.build_runtime(backup=False)
    assert commands[0][-4:]==['-c',str(module.ROOT/'constraints-runtime.txt'),'setuptools','wheel']
    assert not any('ci' in command or 'build' in command for command in commands)
