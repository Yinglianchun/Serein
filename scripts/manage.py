"""Cross-platform Docker or Python + Node installation menu."""
import getpass
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import importlib.util
import ipaddress

ROOT=Path(__file__).resolve().parents[1]
DEPLOY=ROOT/'deploy'


def ask(text,default=''):
    answer=input(text+(f' [{default}]' if default else '')+'：').strip()
    return answer or default


def heading(title):
    print('\n'+'─'*54+'\n'+title+'\n'+'─'*54)


def choose(title,options,*,default='',back=False):
    heading(title)
    for key,label in options:print(f'  {key}. {label}')
    if back:print('  r. 返回上一级')
    allowed={key for key,_ in options}|({'r'} if back else set())
    while True:
        value=ask('请选择',default).lower()
        if value in allowed:return value
        print('没有这个选项，请重新输入。')


def confirm(text):
    while True:
        value=ask(text+' [y/N]').lower()
        if value in ('y','yes'):return True
        if value in ('','n','no'):return False
        print('请输入 y 或 n；直接回车取消。')


def pause():
    if sys.stdin.isatty():input('\n按 Enter 返回菜单…')


def step(number,total,title):print(f'\n[{number}/{total}] {title}',flush=True)


def ensure_tools():
    if backend()=='local':
        if sys.version_info < (3,11):raise ValueError('直跑需要 Python 3.11+；Docker 管理菜单需要 Python 3.9+。')
        if not shutil.which('node') or not shutil.which('npm'):
            raise ValueError('直跑需要 Node.js 22.12+ 和 npm。Termux：pkg install python nodejs-lts git clang make rust pkg-config；Windows 安装 Node.js 并加入 PATH。')
        version=subprocess.check_output(['node','-p','process.versions.node'],text=True).strip()
        if tuple(map(int,version.split('.')[:2]))<(22,12):raise ValueError('请安装 Node.js 22.12 或更新版本。')
        return
    if not shutil.which('docker'):raise ValueError('未找到 Docker。Linux 安装 Docker Engine 和 Compose 插件；Windows 安装并启动 Docker Desktop，使用 Linux containers。')
    run(['docker','compose','version'],stdout=subprocess.DEVNULL)
    run(['docker','info','--format','{{.ServerVersion}}'],stdout=subprocess.DEVNULL)
    system=subprocess.check_output(['docker','info','--format','{{.OSType}}'],text=True).strip()
    if system!='linux':raise ValueError('请将 Docker Desktop 切换到 Linux containers。')


def run(args,**kwargs):return subprocess.run(args,check=True,**kwargs)


def compose(*args,**kwargs):
    source=installation().get('legacy_source_root')
    host_source=source or str(DEPLOY/'runtime'/'legacy-input')
    return run(['docker','compose','-p',installation().get('compose_project','serein-public'),'--project-directory',str(DEPLOY),
        '-f',str(DEPLOY/'compose.yaml'),*args],env={**os.environ,'SEREIN_INSTALL_ROOT':str(ROOT),
        'SEREIN_LEGACY_SOURCE':host_source,'SEREIN_LEGACY_SOURCE_HOST':host_source,
        'SEREIN_LEGACY_SOURCE_CONFIGURED':'1' if source else '0'},**kwargs)


def installation():
    path=DEPLOY/'installation.json'
    return json.loads(path.read_text('utf-8')) if path.exists() else {'backend':'docker'}


def backend():return installation()['backend']


def select_environment():
    if (DEPLOY/'installation.json').exists():return
    if (DEPLOY/'config.toml').exists():
        print('发现已有 Docker 安装配置，沿用当前部署方式和数据。')
        private_file(DEPLOY/'installation.json',json.dumps({'backend':'docker'}));return
    termux='com.termux' in os.environ.get('PREFIX','')
    default='3' if termux else ('2' if os.name=='nt' else '1')
    choice=choose('选择部署环境',[
        ('1','Linux / VPS · Docker Compose'),('2','Windows · Docker Desktop'),
        ('3','安卓 Termux / Windows / Linux · Python + Node 直跑')],default=default,back=True)
    if choice=='r':return False
    if choice=='2':print('请先启动 Docker Desktop，使用 Linux containers。PowerShell 可直接运行 scripts/one_click.ps1。')
    if choice=='3':
        print('需要 Python 3.11+、Node.js 22.12+ 和 npm；脚本会创建独立虚拟环境、安装依赖并构建页面。')
        print('Termux：pkg install python nodejs-lts git clang make rust pkg-config；将发行目录放在 Termux HOME 内。')
        print('手机长期运行请允许 Termux 后台运行；可自行执行 termux-wake-lock，结束后 termux-wake-unlock。')
    project='serein-public-'+hashlib.sha256(str(DEPLOY.resolve()).encode()).hexdigest()[:10]
    private_file(DEPLOY/'installation.json',json.dumps({'backend':'local' if choice=='3' else 'docker','compose_project':project}))


def local_python():
    return DEPLOY/'venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')


def local_action(action,service='all'):
    if action not in ('stop','status') and not local_python().exists():raise ValueError('尚未安装直跑依赖，请先执行部署。')
    python=sys.executable if action in ('stop','status') else str(local_python())
    return run([python,str(ROOT/'scripts'/'local_runtime.py'),'--deploy',str(DEPLOY),action,service])


def service_action(action,service=None):
    if backend()=='local':return local_action({'up':'start'}.get(action,action),service or 'all')
    if action=='up':return compose('up','-d','--wait')
    if action=='restart':
        compose('restart',*([service] if service else []))
        return compose('up','-d','--wait',*([service] if service else []))
    return compose(action,*([service] if service else []))


def build_runtime(*,backup=True):
    if backend()=='docker':
        print('先停止本实例的网关和记忆服务，再依次构建；构建失败时保持停止，可修复后重新部署。',flush=True)
        compose('stop','gateway','memory')
        if backup:backup_runtime()
        # Keep pip installation and the frontend build from competing for RAM
        # on small VPS instances. Completed layers remain reusable on retry.
        compose('build','memory')
        return compose('build','gateway')
    if local_python().exists():
        local_action('stop')
        if backup:backup_runtime()
    else:run([sys.executable,'-m','venv',str(DEPLOY/'venv')])
    run([str(local_python()),'-m','pip','install','.[http,embedding,mcp,background]'],cwd=ROOT)
    # Calling npm's JS entry avoids cmd.exe quoting of paths on Windows.
    npm=shutil.which('npm')
    if os.name=='nt':
        cli=Path(npm).parent/'node_modules'/'npm'/'bin'/'npm-cli.js'
        if not cli.exists():raise ValueError('未找到 npm-cli.js，请使用完整 Node.js 安装。')
        prefix=[shutil.which('node'),str(cli)]
    else:prefix=[npm]
    run([*prefix,'ci'],cwd=ROOT/'web')
    run([*prefix,'run','build'],cwd=ROOT/'web')


def backup_runtime():
    spec=importlib.util.spec_from_file_location('runtime_backup',ROOT/'scripts'/'runtime_backup.py')
    helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
    target=helper.snapshot(DEPLOY)
    if target:print('更新前备份已保存：'+str(target)+'。包含密钥，请妥善保管；恢复前保持服务停止。',flush=True)


def check_memory():
    """Read the installed runtime's policy inputs, without invoking providers."""
    code=('import json,sys; from serein.config import load_settings; '
          'from serein.configured_models import memory_status; '
          'print(json.dumps(memory_status(load_settings(sys.argv[1]))))')
    try:
        if backend()=='docker':
            result=compose('exec','-T','memory','python','-c',code,'/config/config.toml',
                           capture_output=True,text=True,timeout=30)
        else:
            result=run([str(local_python()),'-c',code,str(DEPLOY/'config.toml')],
                       cwd=DEPLOY,capture_output=True,text=True,timeout=30)
        status=json.loads(result.stdout)
        if status['ready']:
            print(f"记忆检索本地校验通过：路由 {status['route_source']}，{status['routes']} 条；"
                  f"boundary {status['boundaries']} 条；domain {status['domain_source']}。未调用模型。")
        else:
            print(f"记忆检索尚未就绪：{status['stage']} / {status['reason']}。请在设置中检查检索配置；服务健康不代表检索就绪。")
        return status
    except (OSError,subprocess.SubprocessError,ValueError,KeyError,TypeError):
        print('未能完成记忆检索本地校验，请在设置中查看 memory_status；服务健康不代表检索就绪。')


def cleanup_backups():
    heading('旧备份清理 · 仅当前实例的一键升级备份')
    print('默认保留最近 3 份，至少保留最新 1 份。未完成备份和手动备份不在清理范围内。')
    while True:
        value=ask('保留最近几份完整备份','3')
        if value.isdecimal() and int(value)>=1:break
        print('请输入不小于 1 的整数。')
    spec=importlib.util.spec_from_file_location('runtime_backup',ROOT/'scripts'/'runtime_backup.py')
    helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
    plan=helper.cleanup_plan(DEPLOY,int(value))
    if not plan['remove']:
        print(f"共有 {len(plan['files'])} 份完整备份，没有需要清理的旧备份。");return
    print('将删除以下旧备份：')
    for row in plan['remove']:print(f"  {row['name']}  {row['size']/1024**2:.1f} MiB")
    print(f"可释放 {sum(row['size'] for row in plan['remove'])/1024**2:.1f} MiB；保留最近 {plan['keep']} 份。")
    if not confirm('确认删除以上旧备份（不可恢复）'):return
    result=helper.cleanup(DEPLOY,plan)
    print(f"已删除 {result['removed']} 份旧备份，释放 {result['bytes']/1024**2:.1f} MiB，保留 {result['retained']} 份。")


def port_prompt(text,default,used=()):
    while True:
        value=ask(text,str(default))
        if value.isdecimal() and 1<=int(value)<=65535 and int(value) not in used:return int(value)
        print('端口需要是 1–65535 之间的整数，且不能与本实例其他服务重复。')


def private_file(path,text):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix(path.suffix+'.tmp')
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w',encoding='utf-8') as f:f.write(text)
    os.replace(temp,path)
    os.chmod(path,0o600)


def auth():
    heading('设置页面登录账号')
    while True:
        username=ask('前端登录用户名','admin')
        if username and ':' not in username:break
        print('用户名不能为空或包含冒号，请重新输入。')
    while True:
        password=getpass.getpass('前端登录密码（至少 10 个字符，不显示输入）：')
        if len(password)<10:
            print('请使用至少 10 个字符的密码。');continue
        if getpass.getpass('再输入一次密码：')==password:break
        print('两次密码不同，请重新输入。')
    salt=secrets.token_bytes(16)
    encoded=hashlib.scrypt(password.encode(),salt=salt,n=16384,r=8,p=1,dklen=32).hex()
    private_file(DEPLOY/'secrets'/'web-auth.json',json.dumps({'username':username,'salt':salt.hex(),'hash':encoded}))
    print('前端鉴权已保存；运行中的网关会自动读取新配置。')


def prepare():
    DEPLOY.mkdir(exist_ok=True)
    (DEPLOY/'runtime').mkdir(exist_ok=True,mode=0o700)
    (DEPLOY/'runtime'/'legacy-input').mkdir(exist_ok=True,mode=0o700)
    if not (DEPLOY/'secrets'/'web-auth.json').exists():auth()
    if not (DEPLOY/'secrets'/'api-token').exists():
        private_file(DEPLOY/'secrets'/'api-token',secrets.token_urlsafe(36))
        print('已自动生成 Gateway Key，请复制保存，稍后的连接说明也会显示：')
        print((DEPLOY/'secrets'/'api-token').read_text('utf-8').strip())
    config=DEPLOY/'config.toml'
    if not config.exists():
        text=(ROOT/'config.example.toml').read_text('utf-8')
        data='./runtime' if backend()=='local' else '/data'
        text=text.replace('./.runtime/serein.db',data+'/serein.db').replace('./.runtime/recall-index.sqlite',data+'/recall-index.sqlite')
        config.write_text(text,'utf-8')
    if not (DEPLOY/'.env').exists():
        while True:
            bind=ask('网关监听地址（同机填 127.0.0.1；手机连电脑／外网填 0.0.0.0）','127.0.0.1')
            if bind in ('0.0.0.0','127.0.0.1'):break
            print('请填 0.0.0.0 或 127.0.0.1。')
        port=port_prompt('网关端口',18217)
        private_file(DEPLOY/'.env',f'SEREIN_BIND={bind}\nSEREIN_PORT={port}\n')
    values=read_env();saved_public_url=installation().get('public_url','')
    if 'SEREIN_PUBLIC_ORIGIN' not in values and saved_public_url.startswith('https://'):
        private_file(DEPLOY/'.env',''.join(f'{k}={v}\n' for k,v in {**values,'SEREIN_PUBLIC_ORIGIN':saved_public_url}.items()))
    record=installation()
    if backend()=='local' and 'memory_port' not in record:
        port=int(read_env()['SEREIN_PORT'])
        record['memory_port']=port_prompt('内部记忆服务端口（仅本机）',18218,[port])
        record['preview_port']=port_prompt('内部页面服务端口（仅本机）',18219,[port,record['memory_port']])
        private_file(DEPLOY/'installation.json',json.dumps(record))


def read_env():
    return dict(line.split('=',1) for line in (DEPLOY/'.env').read_text('utf-8').splitlines() if '=' in line)


def configure_client():
    record=installation()
    if 'client_host' in record:return
    if read_env().get('SEREIN_BIND')=='127.0.0.1':host='127.0.0.1'
    elif confirm('客户端就在这台电脑／手机上吗'):host='127.0.0.1'
    else:
        while True:
            host=ask('客户端访问用的局域网 IP／公网 IP／域名（不含协议或端口）')
            if host and host not in ('0.0.0.0','localhost','127.0.0.1') and all(c.isalnum() or c in '.-:' for c in host):break
            print('请填写可访问的 IP 或域名。')
    record['client_host']=host
    private_file(DEPLOY/'installation.json',json.dumps(record))


def url():
    if installation().get('public_url'):return installation()['public_url']
    port='18217'
    for line in (DEPLOY/'.env').read_text().splitlines():
        if line.startswith('SEREIN_PORT='):port=line.split('=',1)[1]
    host=installation().get('client_host','127.0.0.1')
    if ':' in host:host='['+host+']'
    return 'http://'+host+':'+port


def container_command(*args,source=None):
    if backend()=='local':
        translated=[str(source) if value=='/legacy/input' else str(DEPLOY/'runtime'/'migration-options.json') if value=='/data/migration-options.json' else value for value in args]
        return run([str(local_python()),'-m','serein.legacy_migration','--config',str(DEPLOY/'config.toml'),*translated],cwd=ROOT)
    extra=[] if source is None else ['-v',str(source)+':/legacy/input:ro']
    return compose('run','--rm','--no-deps',*extra,'--entrypoint','python','memory',
        '-m','serein.legacy_migration','--config','/config/config.toml',*args)


def stop_old(source):
    # Only inspect containers whose bind mounts intersect the explicitly named source.
    if source.is_file():
        print('输入的是备份文件，不会自动停止旧服务；同机资源紧张时应在构建前手动停旧服务。');return []
    if backend()=='local' or os.name=='nt':
        print('目录输入：请先停止写入此旧库的服务（包括 Docker Desktop、旧启动脚本）。备份文件不受旧库写入影响，但构建仍需要足够资源。')
        if not confirm('确认旧库当前不会继续写入'):raise ValueError('迁移未开始')
        return []
    ids=subprocess.check_output(['docker','ps','-q'],text=True).split()
    rows=json.loads(subprocess.check_output(['docker','inspect',*ids],text=True)) if ids else []
    names=[]
    for row in rows:
        if (row.get('Config',{}).get('Labels') or {}).get('com.docker.compose.project')==installation().get('compose_project','serein-public'):continue
        for mount in row.get('Mounts',[]):
            if mount.get('Type')!='bind':continue
            path=Path(mount['Source']).resolve()
            if path==source or path.is_relative_to(source) or source.is_relative_to(path):
                names.append(row['Name'].lstrip('/'));break
    if not names:
        print('没有发现挂载此旧库路径的运行容器；若由 systemd 等方式运行，请先自行停止写入。')
        if not confirm('确认旧库当前不会继续写入'):raise ValueError('迁移未开始')
        return []
    print('发现使用此旧库路径的服务：'+', '.join(names))
    if not confirm('停止以上旧服务并开始迁移'):raise ValueError('迁移未开始')
    run(['docker','stop',*names])
    private_file(DEPLOY/'runtime'/'stopped-legacy-services.json',json.dumps(names))
    return names


def deploy():
    print('开始前：请将发行目录放在持久目录，不要放在 /tmp。小内存设备请先手动停止同机旧服务，为构建留出资源；从备份迁移不会自动停旧服务。')
    choice=choose('部署 Serein',[('0','从旧 Ombre 记忆库迁移'),('1','全新安装／重新部署')],default='1',back=True)
    if choice=='r':return
    backup=True
    if choice=='1' and (DEPLOY/'runtime'/'serein.db').is_file():
        selection=choose('更新前是否备份',[('1','先备份再更新（默认）'),('0','跳过备份直接更新')],default='1',back=True)
        if selection=='r':return
        backup=selection=='1'
        if not backup:print('本次不生成更新前备份。')
    source=None
    if choice=='0':
        heading('旧库来源与名字')
        source=Path(ask('旧记忆库根目录、buckets 目录或 tar.gz 备份路径')).expanduser().resolve()
        if not source.exists():raise ValueError('旧库路径不存在')
        if source==DEPLOY or source.is_relative_to(DEPLOY):raise ValueError('旧库不能指向当前新部署目录')
        names={'user_name':ask('旧记忆里的用户名字'),'ai_name':ask('旧记忆里的 AI 名字')}
        if not all(names.values()):raise ValueError('两个名字都需要填写')
        aliases=[s.strip() for s in ask('旧称／别名（逗号分隔，可留空）').replace('，',',').split(',') if s.strip()]
        generate_cues=choose('打标时是否生成召回线索 cues',[('1','生成 cues（沿用原迁移行为）'),('0','仅打主域和实体标签，不生成 cues')],default='1')=='1'
        print('将重新打标、提取实体'+('和 cues' if generate_cues else '（不生成 cues）')+'、用程序转换旧边，并为无法复用的正文与 passage 生成向量。会调用模型并产生费用。')
        print('reflection / affect_anchor 整段删除；其他 ### 标题删除、正文保留。feel / whisper 转日记；日印象不导入。')
        if not confirm('接受以上转换规则，继续查看导入预览'):return
    total=6 if source is not None else 3
    step(1,total,'保存页面账号、部署路径和端口')
    prepare()
    configure_client()
    step(2,total,'安装依赖并构建页面' if backend()=='local' else '构建网关和记忆库镜像；首次构建可能需要几分钟')
    build_runtime(backup=backup)
    if source is not None:
        step(3,total,'扫描旧库并显示导入预览')
        options={**names,'aliases':aliases,'source_path':str(source),'generate_cues':generate_cues}
        private_file(DEPLOY/'runtime'/'migration-options.json',json.dumps(options,ensure_ascii=False))
        container_command('scan','/legacy/input',source=source)
        if not confirm('确认预览后继续，下一步核对旧库写入状态，原库仍保留'):return
        step(4,total,'核对旧服务，准备转换环境')
        stop_old(source)
    if source is not None:
        # Keep writers stopped even when resuming a partially completed conversion.
        service_action('stop')
        if backend()=='docker':compose('create')
        try:
            step(5,total,'导入模型配置、确认主域并转换记忆；进度会保存')
            container_command('wizard','/legacy/input','--options','/data/migration-options.json',source=source)
        except (subprocess.CalledProcessError,KeyboardInterrupt):
            print('迁移已暂停，已完成内容保留。重新选择“从旧库迁移”并使用同一路径可继续。')
            print('脚本停止的旧容器名称记录在 deploy/runtime/stopped-legacy-services.json；手动停止的服务请用原启动方式恢复。不要同时向新旧库写入。')
            raise
    step(total,total,'启动两个服务并等待就绪')
    service_action('up')
    check_memory()
    heading('部署完成 · 接下来怎么使用')
    print('服务已部署。打开 '+url()+'，用刚才的页面账号登录，进入“设置 → 模型；侧栏问号 → 使用说明”。')
    print('聊天 API 的 Base URL 为上述地址加 /v1；密钥保存在 deploy/secrets/api-token。外网使用请配置 HTTPS。')
    connection_guide()
    print('需要局域网／公网访问时，选择主菜单 5 配置入口；无需再次迁移或构建。')
    if backend()=='local':print('关闭菜单不会停止服务；停止／启动／状态与日志使用菜单 4。手机重启后需要重新启动服务。')


def connection_guide():
    key=(DEPLOY/'secrets'/'api-token').read_text('utf-8').strip()
    guide=f'Gateway Key：{key}\n请复制保存到客户端 API Key；这不是上游模型密钥或页面密码。不要公开此连接说明。\n需要更换时使用主菜单 6；更换后旧 Key 失效，所有客户端需更新。\n'
    try:
        address=url()
    except (OSError,ValueError):
        guide+='访问地址未读取：deploy/.env 或安装记录缺失、不可读或格式有误。已有实例请沿用原来的页面、聊天 API 和 MCP 地址，只更新客户端 Key。\n'
    else:
        guide+=f'页面：{address}\n聊天 Base URL：{address}/v1\nMCP 地址：{address}/serein/mcp\n'
    guide+='API 密钥文件：deploy/secrets/api-token（不是页面密码）\n同机使用 127.0.0.1；手机连电脑填电脑的局域网 IP，并允许网关端口通过防火墙。\n公网入口可在主菜单 5 配置，页面密码和 API 鉴权保持启用。\n'
    guide+='传输类型：Streamable HTTP\nMCP OAuth：HTTPS 域名入口选择 OAuth，在 Serein 授权页输入上方 Gateway Key\nMCP 静态方式：Authorization: Bearer <上方 Gateway Key>（用于支持自定义请求头的客户端）\n'
    guide+='聊天经过此网关且开启“开窗续接”时，可发送 /resume 后面想继续聊的话；指令和接续材料由网关处理，不经过 MCP 工具返回。\n'
    guide+='在客户端添加请求头 X-Serein-Window-ID，值为当前会话 ID（例如 chat-001）；同一会话不变，新窗口换值。\n不填则使用默认会话 main，共用提醒轮次与召回冷却，不自动识别新窗口。固定值也不能区分窗口；开窗续接请在经过此网关的聊天中发送 /resume。\n'
    print(guide,end='')
    try:
        private_file(DEPLOY/'connection-guide.txt',guide)
    except OSError:
        print('连接说明文件未能保存；Gateway Key 已显示在上方，请复制保存。')
    else:
        print('已保存 deploy/connection-guide.txt。')


def rotate_key():
    path=DEPLOY/'secrets'/'api-token'
    if not path.is_file():raise ValueError('请先完成部署。')
    if not confirm('生成新的 Gateway Key 并重启两个服务（旧 Key 立即失效，客户端需更新）'):return
    previous=path.read_text('utf-8')
    private_file(path,secrets.token_urlsafe(36))
    try:
        service_action('restart')
    except Exception:
        private_file(path,previous)
        service_action('restart')
        raise
    print('Gateway Key 已更换，请复制并更新所有客户端：')
    connection_guide()


def access():
    if not (DEPLOY/'.env').is_file() or not (DEPLOY/'secrets'/'web-auth.json').is_file() or not (DEPLOY/'secrets'/'api-token').is_file():
        raise ValueError('请先完成部署和登录配置，再设置访问入口。')
    choice=choose('访问入口',[('0','仅本机'),('1','局域网／公网 IP 和端口'),('2','HTTPS 域名（Linux nginx，已有证书）')],back=True)
    if choice=='r':return
    spec=importlib.util.spec_from_file_location('public_access',ROOT/'scripts'/'public_access.py')
    helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
    record=installation();values=read_env();port=int(values['SEREIN_PORT']);proxy=None
    if record.get('public_url','').startswith('https://') and choice!='2':
        raise ValueError('该实例已有 HTTPS 代理；请先移除 nginx 中对应的入口，再切换为 IP 或仅本机。')
    if choice=='2':
        if not sys.platform.startswith('linux') or os.geteuid()!=0 or not shutil.which('nginx'):
            raise ValueError('HTTPS 自动接入需要 Linux root 和已安装的 nginx。')
        domain=helper.host(ask('已解析到本机的 HTTPS 域名'))
        dump=subprocess.check_output(['nginx','-T'],text=True,stderr=subprocess.PIPE)
        proxy=helper.plan(domain,port,DEPLOY,dump)
        print('将接入 HTTPS 首页：'+domain+'；nginx 配置：'+str(proxy[0]))
        bind='127.0.0.1';public_url='https://'+domain;client_host=domain
    elif choice=='1':
        client_host=ask('其他设备访问用的 IP 或域名（不含协议和端口）').strip('[]')
        try:
            address=ipaddress.ip_address(client_host)
        except ValueError:
            helper.host(client_host)
        else:
            if address.is_unspecified or address.is_loopback:raise ValueError('请填写其他设备可访问的地址。')
        bind='0.0.0.0';public_url='';print('将开放网关端口 '+str(port)+'；请允许该端口通过服务器防火墙和云安全组。IP 入口使用 HTTP，公网长期使用建议选择 HTTPS。')
    else:bind='127.0.0.1';client_host='127.0.0.1';public_url=''
    if not confirm('应用此访问入口（保留数据、页面密码和 API 密钥）'):return
    oauth_origin=public_url if public_url else ''
    before=(DEPLOY/'.env').read_text();updated={**values,'SEREIN_BIND':bind,'SEREIN_PUBLIC_ORIGIN':oauth_origin}
    env_changed=values.get('SEREIN_BIND')!=bind or values.get('SEREIN_PUBLIC_ORIGIN','')!=oauth_origin
    try:
        if env_changed:
            private_file(DEPLOY/'.env',''.join(f'{k}={v}\n' for k,v in updated.items()))
            if backend()=='docker':compose('up','-d','--no-deps','--wait','gateway')
            else:local_action('restart','gateway')
        if proxy:helper.apply(*proxy,DEPLOY)
    except Exception:
        private_file(DEPLOY/'.env',before)
        if env_changed:
            if backend()=='docker':compose('up','-d','--no-deps','--wait','gateway')
            else:local_action('restart','gateway')
        raise
    record.update(client_host=client_host,public_url=public_url)
    private_file(DEPLOY/'installation.json',json.dumps(record))
    connection_guide()


def maintenance():
    choice=choose('向量重建与清理',[('0','补齐缺失向量'),('1','重建当前模型的全部向量'),('2','清理孤立的派生向量')],default='0',back=True)
    if choice=='r':return
    print('只操作当前 Serein 的派生索引，正文和原库不删除。补齐／重建会调用嵌入模型。')
    if not confirm('开始维护'):return
    service_action('stop')
    try:container_command('vectors','--mode',{'0':'fill','1':'rebuild','2':'clean'}[choice])
    finally:service_action('up')


def repair_legacy_edges():
    if not (DEPLOY/'config.toml').is_file():raise ValueError('请先完成当前实例的部署和旧库正文导入')
    heading('旧边转换补救 · 修复早期版本未正确转换的关系')
    print('请先用新版发行文件完成重新部署，再运行本功能。')
    print('只用程序规则：updates 转 continues 并交换两端；不确定项保留原记录并报告。')
    print('不会重导正文、重新打标或重建向量，不调用模型。执行前自动备份当前数据库。')
    value=ask('原始旧库根目录、buckets 或 tar/tar.gz 备份（留空读取已保存的旧边；当时漏读边文件需提供备份）')
    source=Path(value).expanduser().resolve() if value else None
    if source is not None and not source.exists():raise ValueError('旧库路径不存在')
    args=('repair-edges',*(['/legacy/input'] if source is not None else []))
    container_command(*args,source=source)
    if not confirm('按以上来源补救旧边；匹配到的早期泛关联退出活动关系，历史保留，期间短暂停止当前实例'):return
    service_action('stop')
    try:container_command(*args,'--apply',source=source)
    finally:service_action('up')
    print('旧边补救已结束，具体成功／保留／跳过及原记录见上方报告路径。中断后可用同一来源重跑。')


def repair_legacy_history():
    if not (DEPLOY/'config.toml').is_file():raise ValueError('请先部署当前实例')
    heading('历史数据补漏 · 梦境、窗影、正式日记、暗房、旧原文、日期与记忆注脚')
    print('请先用新版发行文件完成重新部署，再运行本功能。旧库需停止写入或使用完整备份。')
    print('补入旧原文供搜索与绑定，补齐旧 Scene 日期和 comments／年轮注脚；不自动整理旧原文、不调用模型、不重导 Scene、不打标、不重建向量；执行前自动备份。')
    value=ask('原始旧库根目录、buckets 目录或含 state 的 tar/tar.gz 完整备份')
    if not value:raise ValueError('请提供完整旧库来源')
    source=Path(value).expanduser().resolve()
    if not source.exists():raise ValueError('旧库路径不存在')
    if source==DEPLOY or source.is_relative_to(DEPLOY):raise ValueError('旧库不能指向当前实例目录')
    args=('repair-history','/legacy/input')
    container_command(*args,source=source)
    if not confirm('按以上预览补漏，保留删除和锁定状态，期间短暂停止当前实例'):return
    service_action('stop')
    try:container_command(*args,'--apply',source=source)
    finally:service_action('up')
    print('补漏结束，请查看报告中的 ID 对应关系及待核对记录；同一备份可安全重跑。')


def repair_legacy_cues():
    if not (DEPLOY/'config.toml').is_file():raise ValueError('请先部署当前实例')
    heading('旧 Scene 补 cues · 显式模型调用')
    print('只处理已导入、仍活动且缺少 cues 的 Ombre Scene；保留正文、已有 cues 和已完成的主域／实体。')
    print('失败项会保存进度并停止自动重试；再次执行会从缺失或失败项继续。')
    container_command('repair-cues')
    if not confirm('按以上预览补 cues（会调用打标模型并产生费用，执行前自动备份）'):return
    service_action('stop')
    try:container_command('repair-cues','--apply')
    finally:service_action('up')
    print('补 cues 已结束。已有 cues 未覆盖；空结果也会记为已处理，避免重复收费。')


def restart():
    choice=choose('重启服务',[('0','Gateway 网关'),('1','记忆库')],back=True)
    if choice=='r':return
    service={'0':'gateway','1':'memory'}[choice]
    service_action('restart',service)
    if backend()=='docker':compose('ps',service)
    print('重启命令已完成，服务状态见上方。')


def operations():
    choice=choose('服务与日志',[('0','启动全部服务'),('1','停止全部服务'),('2','查看状态'),('3','查看最近日志')],back=True)
    if choice=='r':return
    if choice=='0':service_action('up')
    elif choice=='1':
        if confirm('停止本实例的两个服务'):service_action('stop')
    elif choice=='2':
        if backend()=='local':local_action('status')
        else:compose('ps')
        check_memory()
    elif backend()=='docker':compose('logs','--tail','80')
    else:
        for service in ('memory','gateway'):
            path=DEPLOY/'runtime'/'logs'/f'{service}.log'
            print(f'\n{service} · {path}')
            if path.exists():
                with path.open('rb') as log:
                    log.seek(max(0,path.stat().st_size-32768))
                    print('\n'.join(log.read().decode('utf-8',errors='replace').splitlines()[-80:]))
            else:print('尚无日志。')


def legacy_source_access():
    if backend()!='docker':
        print('直跑实例可在网页填写这台机器上可读取的旧库绝对路径，不需要额外挂载。')
        return
    if not (DEPLOY/'config.toml').is_file():raise ValueError('请先完成当前实例的部署')
    value=ask('允许网页迁移读取的旧库根目录或 tar 备份路径（只读）')
    source=Path(value).expanduser().resolve()
    if not source.exists():raise ValueError('旧库路径不存在')
    if source==DEPLOY or source.is_relative_to(DEPLOY):raise ValueError('旧库不能指向当前实例部署目录')
    root=source if source.is_dir() else source.parent
    if not root.is_dir():raise ValueError('请选择旧库目录或 tar/tar.gz 备份')
    if not confirm(f'将 {root} 只读挂载到本实例并重建两个容器（短暂断开网页）'):return
    before=installation();updated={**before,'legacy_source_root':str(root)}
    private_file(DEPLOY/'installation.json',json.dumps(updated))
    try:
        compose('up','-d','--no-deps','--force-recreate','--wait','memory')
        compose('up','-d','--no-deps','--force-recreate','--wait','gateway')
    except Exception:
        private_file(DEPLOY/'installation.json',json.dumps(before))
        try:
            compose('up','-d','--no-deps','--force-recreate','--wait','memory')
            compose('up','-d','--no-deps','--force-recreate','--wait','gateway')
        except Exception:pass
        raise
    print(f'只读来源已就绪：网页“旧库迁移”可以填写 {source} 或它下面的路径。')


def doctor():
    helper_spec=importlib.util.spec_from_file_location('serein_doctor',ROOT/'scripts'/'doctor.py')
    helper=importlib.util.module_from_spec(helper_spec);helper_spec.loader.exec_module(helper)
    return helper.diagnose(sys.modules[__name__])


def main():
    while True:
        try:
            print(f'\n安装目录：{ROOT}\n实例数据：{DEPLOY / "runtime"}')
            choice=choose('Serein · 安装与维护',[
                ('0','设置前端用户名／密码'),('1','拉取上游代码并重建' if (DEPLOY/'config.toml').is_file() else '安装 Serein（全新安装／旧库迁移）'),('2','重启服务'),('3','向量重建与清理'),('4','启动／停止／状态／日志'),('5','访问入口：本机／局域网／公网'),('6','更换 Gateway Key'),('7','网页旧库目录只读授权'),('8','旧边转换补救（早期版本未转换成功）'),('9','旧备份清理'),('10','历史数据补漏（梦境／窗影／日记／暗房）'),('11','旧 Scene 补 cues'),('12','故障排查（环境／配置／服务／日志）'),('q','退出')])
            if choice=='q':return
            if choice=='12':
                doctor()
                pause()
                continue
            spec=importlib.util.spec_from_file_location('installer_lock',ROOT/'src'/'serein'/'file_lock.py')
            locks=importlib.util.module_from_spec(spec);spec.loader.exec_module(locks)
            with locks.exclusive_lock(DEPLOY/'runtime'/'installer.lock'):
                if choice=='0':auth()
                elif choice=='9':cleanup_backups()
                elif choice=='1' and (DEPLOY/'config.toml').is_file():
                    spec=importlib.util.spec_from_file_location('upstream_update',ROOT/'scripts'/'upstream_update.py')
                    helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
                    if helper.run_update(sys.modules[__name__]):return
                else:
                    if select_environment() is False:continue
                    ensure_tools()
                    {'1':deploy,'2':restart,'3':maintenance,'4':operations,'5':access,'6':rotate_key,'7':legacy_source_access,'8':repair_legacy_edges,'10':repair_legacy_history,'11':repair_legacy_cues}[choice]()
        except EOFError:
            print('\n输入已结束，退出管理菜单。');return
        except KeyboardInterrupt:print('\n已取消当前操作。')
        except subprocess.CalledProcessError as exc:
            print(f'命令未完成（退出码 {exc.returncode}），请查看上方错误。修复后可从菜单重试。')
        except (ValueError,OSError,RuntimeError) as exc:
            print('未完成：'+str(exc))
        try:pause()
        except (EOFError,KeyboardInterrupt):return


if __name__=='__main__':main()
