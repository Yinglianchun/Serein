import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


def test_release_includes_runtime_python_modules():
    root = Path(__file__).resolve().parents[1]
    manifest = set(json.loads((root/'release-files.json').read_text(encoding='utf-8')))
    modules = {
        path.relative_to(root).as_posix()
        for path in (root/'src'/'serein').rglob('*.py')
    }
    assert modules <= manifest, sorted(modules - manifest)


@pytest.mark.parametrize('private_path',[
    'deploy/secrets/api-token','deploy/runtime/settings.json',
    'deploy/installation.json','deploy/venv/pyvenv.cfg',
])
def test_release_refuses_private_installation_even_if_manifest_lists_it(tmp_path,private_path):
    scripts=tmp_path/'scripts';scripts.mkdir()
    shutil.copy2(Path(__file__).parents[1]/'scripts/release.py',scripts/'release.py')
    target=tmp_path/private_path;target.parent.mkdir(parents=True,exist_ok=True);target.write_text('synthetic-private-data')
    (tmp_path/'release-files.json').write_text(json.dumps([private_path]))
    output=tmp_path/'must-not-exist.zip'
    result=subprocess.run([sys.executable,str(scripts/'release.py'),str(output)],capture_output=True)
    assert result.returncode!=0 and not output.exists()


@pytest.mark.parametrize('text', [
    'participants: ['+'xiao'+'yu, '+'ha'+'ven]',
    'key: '+'sk-'+'synthetic'*4,
    'window_'+'a'*24,
    '今天'+'小'+'雨'+'选择了这本书',
    'C:/Users/123456/runtime/config.toml',
])
def test_release_blocks_private_example_content(tmp_path,text):
    scripts=tmp_path/'scripts';scripts.mkdir()
    shutil.copy2(Path(__file__).parents[1]/'scripts/release.py',scripts/'release.py')
    target=tmp_path/'examples/route-examples.json';target.parent.mkdir()
    target.write_text(json.dumps({'examples':[text]}),encoding='utf-8')
    (tmp_path/'release-files.json').write_text(json.dumps(['examples/route-examples.json']))
    output=tmp_path/'blocked.zip'
    result=subprocess.run([sys.executable,str(scripts/'release.py'),str(output)],capture_output=True)
    assert result.returncode!=0 and not output.exists()


def test_release_accepts_synthetic_configured_name_examples(tmp_path):
    scripts=tmp_path/'scripts';scripts.mkdir()
    shutil.copy2(Path(__file__).parents[1]/'scripts/release.py',scripts/'release.py')
    target=tmp_path/'examples/route-examples.json';target.parent.mkdir()
    target.write_text(json.dumps({'examples':['{ai_name}, hello.','{user_name} chose a book.']}),encoding='utf-8')
    (tmp_path/'release-files.json').write_text(json.dumps(['examples/route-examples.json']))
    output=tmp_path/'safe.zip'
    result=subprocess.run([sys.executable,str(scripts/'release.py'),str(output)],capture_output=True)
    assert result.returncode==0 and output.is_file()
