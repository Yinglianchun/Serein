"""Read-only diagnostics for the explicitly selected Serein installation."""
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.request import ProxyHandler, build_opener


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def health(port, path):
    # Ignore shell proxies and never follow a redirect to another deployment.
    from urllib.request import HTTPRedirectHandler
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    opener = build_opener(ProxyHandler({}), NoRedirect())
    with opener.open(f'http://127.0.0.1:{port}{path}', timeout=5) as response:
        return response.status == 200


LOG_RULES = [
    ('认证失败', r'\b(?:401|403|unauthorized|forbidden)\b', '检查客户端 Gateway Key 和设置中的上游密钥；两者不同。'),
    ('限流', r'\b429\b|rate.?limit', '检查上游额度和请求频率。'),
    ('端口占用', r'address already in use|EADDRINUSE', '核对本实例端口和重复启动的服务。'),
    ('权限不足', r'permission denied|EACCES', '检查当前实例目录的读写权限。'),
    ('超时或连接失败', r'timeout|timed out|connection refused|ECONNREFUSED', '检查服务状态、上游地址和网络。'),
    ('服务或程序错误', r'\b(?:500|502|503|504|error|exception|traceback)\b', '在菜单 4 查看日志定位；分享前移除密钥和私人内容。'),
]


def log_findings(text):
    """Return fixed labels/counts only: log lines may contain private content."""
    lines = text.splitlines()[-160:]
    return [(label, sum(bool(re.search(pattern, line, re.I)) for line in lines), hint)
            for label, pattern, hint in LOG_RULES
            if any(re.search(pattern, line, re.I) for line in lines)]


def diagnose(manager):
    issues = warnings = 0
    def report(level, label, hint=''):
        nonlocal issues, warnings
        issues += level == 'FAIL'
        warnings += level == 'WARN'
        print(f'{level:<4} {label}' + (f'；{hint}' if hint else ''))

    def command(label, args=None, compose_args=None):
        try:
            options = dict(capture_output=True, text=True, timeout=15)
            result = (manager.compose(*compose_args, **options) if compose_args is not None
                      else manager.run(args, **options))
            return result.stdout
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
            # Never print subprocess output or exception text, which may contain secrets.
            report('FAIL', label, '检查依赖或服务后重试菜单 12。')
            return None

    manager.heading('故障排查 · 当前实例')
    print(f'安装目录：{manager.ROOT}\n实例目录：{manager.DEPLOY}')
    print('只读检查，不重启、不改配置、不调用模型。日志仅显示错误类别和次数。')
    manager.heading('配置与鉴权')
    for relative in ('config.toml', '.env', 'secrets/api-token', 'secrets/web-auth.json'):
        try:
            content = (manager.DEPLOY / relative).read_text('utf-8').strip()
            if not content:
                raise ValueError('empty')
            if relative.endswith('web-auth.json'):
                record = json.loads(content)
                if not isinstance(record, dict) or not all(record.get(k) for k in ('username', 'salt', 'hash')):
                    raise ValueError('invalid')
            report('OK', relative + ' 已配置')
        except (OSError, ValueError):
            report('FAIL', relative + ' 缺失、为空或不可读', '页面账号用菜单 0；其他安装文件用菜单 1 完成部署。')
    try:
        record = manager.installation()
        kind = record['backend']
        if kind not in ('local', 'docker'):
            raise ValueError('backend')
    except (OSError, ValueError, KeyError, TypeError):
        report('FAIL', '安装记录损坏', '核对 deploy/installation.json；不要删除现有数据。')
        kind = None

    manager.heading('基础环境与服务状态')
    services_ready = False
    if kind == 'docker':
        if not shutil.which('docker'):
            report('FAIL', '未找到 Docker', '安装 Docker Engine 或 Docker Desktop 并加入 PATH。')
        else:
            compose_ok = command('Docker Compose 不可用', ['docker', 'compose', 'version']) is not None
            system = command('Docker daemon 不可用', ['docker', 'info', '--format', '{{.OSType}}'])
            if system is not None:
                report('OK' if system.strip() == 'linux' else 'FAIL', 'Docker 容器类型', '需要 Linux containers；Windows 请启动 Docker Desktop。')
            if compose_ok and system is not None and system.strip() == 'linux':
                output = command('无法读取当前 Compose 项目状态', compose_args=('ps', '--all', '--format', 'json'))
                if output is not None:
                    try:
                        rows = json.loads(output) if output.lstrip().startswith('[') else [json.loads(line) for line in output.splitlines() if line.strip()]
                        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                            raise ValueError('rows')
                        services_ready = True
                        for service in ('memory', 'gateway'):
                            row = next((r for r in rows if r.get('Service') == service), {})
                            running = row.get('State') == 'running'
                            healthy = row.get('Health') == 'healthy'
                            report('OK' if running and healthy else 'FAIL', service + (' 运行且健康' if running and healthy else ' 未运行或未就绪'), '启动／日志用菜单 4，重启用菜单 2。' if not (running and healthy) else '')
                    except (ValueError, TypeError):
                        report('FAIL', 'Compose 状态无法解析', '检查 Compose 插件版本。')
    elif kind == 'local':
        report('OK' if sys.version_info >= (3, 11) else 'WARN', '管理 Python ' + '.'.join(map(str, sys.version_info[:3])), '直跑安装需要 Python 3.11+。')
        python = manager.local_python()
        if not python.is_file():
            report('FAIL', '实例虚拟环境缺失', '菜单 1 安装直跑依赖。')
        else:
            output = command('实例 Python 不可用', [str(python), '-c', 'import sys; print("ok" if sys.version_info >= (3,11) else "old")'])
            if output is not None:
                report('OK' if output.strip() == 'ok' else 'FAIL', '实例 Python 版本检查')
        if not shutil.which('node'):
            report('FAIL', '未找到 Node.js', '安装 Node.js 22.12+ 并加入 PATH。')
        else:
            output = command('Node.js 不可用', ['node', '-p', 'process.versions.node'])
            if output is not None:
                try:
                    valid = tuple(map(int, output.strip().split('.')[:2])) >= (22, 12)
                    report('OK' if valid else 'FAIL', 'Node.js 版本检查', '需要 22.12+。' if not valid else '')
                except ValueError:
                    report('FAIL', 'Node.js 版本无法解析')
        report('OK' if shutil.which('npm') else 'FAIL', 'npm 命令检查', '完整安装 Node.js 和 npm。' if not shutil.which('npm') else '')
        runtime = load('doctor_local_runtime', manager.ROOT / 'scripts/local_runtime.py')
        for service in ('memory', 'gateway'):
            current = runtime.control(manager.DEPLOY, service, 'status')
            running = isinstance(current, dict) and current.get('running')
            report('OK' if running else 'FAIL', service + (' 正在运行' if running else ' 未运行'), '启动／日志用菜单 4。' if not running else '')
        services_ready = True

    manager.heading('端口与健康检查')
    try:
        values = manager.read_env()
        port = int(values['SEREIN_PORT'])
        ports = [port] + ([int(record['memory_port']), int(record['preview_port'])] if kind == 'local' else [])
        if not all(1 <= p <= 65535 for p in ports) or len(set(ports)) != len(ports):
            raise ValueError('ports')
        if values.get('SEREIN_BIND') not in ('127.0.0.1', '0.0.0.0'):
            raise ValueError('bind')
        report('OK', f'网关监听 {values["SEREIN_BIND"]}:{port}')
        probes = [('gateway', port, '/ready')]
        if kind == 'local':
            probes.append(('memory', int(record['memory_port']), '/health'))
        for service, target, path in probes:
            try:
                healthy = health(target, path)
            except (OSError, ValueError):
                healthy = False
            report('OK' if healthy else 'FAIL', f'{service} 本机健康检查' + ('通过' if healthy else '失败'), '菜单 4 查看状态／日志；检查端口和启动结果。' if not healthy else '')
        if values['SEREIN_BIND'] == '127.0.0.1':
            report('INFO', '网关仅本机监听', '其他设备访问用菜单 5 配置入口。')
        else:
            report('INFO', '外部访问还需允许网关端口通过防火墙／云安全组；HTTPS 检查域名和 nginx。')
    except (OSError, ValueError, KeyError, TypeError):
        report('FAIL', '监听地址或端口配置缺失／无效', '核对 deploy/.env 和 installation.json；菜单 1 完成部署。')

    manager.heading('最近错误 · 每服务最近 160 行')
    for service in ('memory', 'gateway'):
        text = None
        if kind == 'docker' and services_ready:
            text = command(service + ' 日志读取失败', compose_args=('logs', '--no-color', '--tail', '160', service))
        elif kind == 'local':
            path = manager.DEPLOY / 'runtime/logs' / (service + '.log')
            try:
                with path.open('rb') as log:
                    log.seek(max(0, path.stat().st_size - 65536))
                    text = log.read().decode('utf-8', errors='replace')
            except OSError:
                report('WARN', service + ' 日志尚不存在或不可读')
        if text is not None:
            findings = log_findings(text)
            for label, count, hint in findings:
                report('WARN', f'{service}：{label} {count} 行', hint)
            if not findings:
                report('OK', service + ' 未发现明显错误关键词（不代表全部功能正常）')
        elif kind != 'local':
            report('INFO', service + ' 日志检查跳过；先恢复 Docker 和当前项目配置。')

    manager.heading('记忆检索本地校验')
    if kind is not None and services_ready and (manager.DEPLOY / 'config.toml').is_file():
        state = manager.check_memory()
        if not state or not state.get('ready'):
            report('WARN', '记忆检索未就绪或无法校验', '网页设置检查检索配置；健康接口通过不代表召回就绪。')
    else:
        report('INFO', '跳过检索校验；服务就绪后重跑。')
    manager.heading('排查结果')
    print(f'发现 {issues} 个问题、{warnings} 个提醒。按对应提示修复后重跑菜单 12。')
    return {'issues': issues, 'warnings': warnings}


if __name__ == '__main__':
    manager = load('doctor_manager', Path(__file__).with_name('manage.py'))
    sys.exit(1 if diagnose(manager)['issues'] else 0)
