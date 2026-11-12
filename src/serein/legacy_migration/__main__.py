import argparse
import asyncio
import getpass
import json
from pathlib import Path
from ..file_lock import exclusive_lock

from ..config import load_settings
from ..bootstrap import initialize
from ..deployment import DEFAULT_DOMAINS,read_settings,save_settings,task_model
from ..api.settings import TaggingPatch,ModelEntry
from .scan import scan,unpack
from .models import import_models
from .workflow import Migration
from .vectors import reuse_legacy,maintain


def ask(text,default=''):
    value=input(text+(f' [{default}]' if default else '')+'：').strip()
    return value or default


def domains(database):
    catalog=read_settings(database)['tagging']['domains']
    while True:
        for n,row in enumerate(catalog):print(f"{n}. {row['label']} ({row['key']})：{row['description']}")
        choice=ask('主域：a 添加；d 删除；e 修改；yes 确认')
        if choice=='yes':
            valid=TaggingPatch(domains=catalog)
            if not valid.domains:raise ValueError('至少保留一个主域')
            save_settings(database,{'tagging':valid.model_dump(exclude_none=True)});return
        try:
            if choice=='d':catalog.pop(int(ask('删除哪一项的序号')))
            if choice in ('a','e'):
                old=catalog[int(ask('修改哪一项的序号'))] if choice=='e' else None
                row={'key':ask('英文标识',old['key'] if old else ''),'label':ask('显示名称',old['label'] if old else ''),
                    'description':ask('短描述',old['description'] if old else ''),'policy':old['policy'] if old else 'normal'}
                proposed=[row if item is old else item for item in catalog] if old else catalog+[row]
                TaggingPatch(domains=proposed)
                catalog=proposed
        except (ValueError,IndexError):print('序号、英文标识或名称不合适，请重新输入；主域标识不能重复。')


def ensure_model(database,task,label):
    current=task_model(database,task)
    if current and ask(f"{label}：{current['model']}，沿用此连接？yes/no",'yes')=='yes':return
    model=ModelEntry(id='migration-'+task,label=label,base_url=ask(label+' API Base URL（含 /v1）'),
        model=ask(label+' 模型名称'),api_key=getpass.getpass(label+' API key（可留空）：'),protocol='openai').model_dump(exclude_none=True)
    state=read_settings(database)
    models={m['id']:m for m in state['models']};models[model['id']]=model
    save_settings(database,{'models':list(models.values()),'assignments':{task:model['id']}})


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',required=True)
    sub=parser.add_subparsers(dest='command',required=True)
    for command in ('scan','wizard'):
        p=sub.add_parser(command);p.add_argument('source',type=Path)
        if command=='wizard':p.add_argument('--options',type=Path,required=True)
    p=sub.add_parser('vectors');p.add_argument('--mode',choices=['fill','rebuild','clean'],required=True)
    p=sub.add_parser('repair-edges',help='补救早期版本没有正确转换的旧关系边')
    p.add_argument('source',type=Path,nargs='?');p.add_argument('--apply',action='store_true')
    p=sub.add_parser('repair-history',help='补入早期迁移遗漏的梦境、窗影、正式日记和暗房')
    p.add_argument('source',type=Path);p.add_argument('--apply',action='store_true')
    p=sub.add_parser('repair-cues',help='为已导入但缺少 cues 的旧 Scene 显式补齐 cues')
    p.add_argument('--apply',action='store_true')
    args=parser.parse_args();settings=load_settings(args.config)
    root=settings.database.parent/'migrations';root.mkdir(parents=True,exist_ok=True)
    with exclusive_lock(root/'operation.lock'):
        if args.command=='repair-history':
            if not settings.database.is_file():raise SystemExit('请先部署当前实例，补漏不会创建新实例')
            from .history import run_history_repair
            plan=scan(unpack(args.source,root))
            if plan['errors']:
                print(json.dumps(plan['errors'],ensure_ascii=False,indent=2))
                raise SystemExit('请先处理旧库扫描错误')
            print(json.dumps({'history':plan['history']['summary'],'originals':plan['originals']['summary'],
                'memory_comments':plan['summary']['memory_comments'],'scenes_with_legacy_dates':sum(bool(item.get('date')) for item in plan['items'] if item['kind']=='scene')},ensure_ascii=False,indent=2))
            print('补漏不调用模型，不重导 Scene、不打标、不重建向量；保留删除与锁定状态。')
            preview=root/'history-repair-preview.json'
            identity={'memory':plan['fingerprint'],'history':plan['history']['fingerprint'],'originals':plan['originals']['fingerprint']}
            if not args.apply:
                preview.write_text(json.dumps(identity),encoding='utf-8')
                print('当前仅预览；停止旧库写入后，使用 --apply 执行。')
                return
            if not preview.is_file() or json.loads(preview.read_text('utf-8'))!=identity:
                raise SystemExit('历史数据与确认的预览不一致，请重新预览')
            result=run_history_repair(settings,plan)
            print(json.dumps(result,ensure_ascii=False,indent=2));return
        if args.command=='repair-edges':
            if not settings.database.is_file():raise SystemExit('请先完成旧库正文导入；补救不会创建或导入记忆')
            from .repair import collect,run_repair
            plan=scan(unpack(args.source,root)) if args.source else None
            if plan and plan['errors']:
                print(json.dumps(plan['errors'],ensure_ascii=False,indent=2))
                raise SystemExit('请先处理原始备份的扫描错误')
            records,issues=collect(settings.database,plan)
            print(json.dumps({'old_edges':len(records),'issues':issues,
                              'edge_sources':plan['summary']['edge_sources'] if plan else []},ensure_ascii=False,indent=2))
            print('这是早期旧边未正确转换的补救：只用程序规则，updates 转 continues 并交换两端；不确定项保留原记录并报告。')
            print('不调用模型，不重导正文、不打标、不重建向量；匹配到的早期泛关联退出活动关系，历史保留。')
            if not records:
                print('没有可处理的旧边。若当时未读入边文件，请提供原始旧库根目录或含 state 的备份。');return
            if not args.apply:
                print('当前仅预览；确认并停止当前实例写入后，添加 --apply 执行。');return
            result=run_repair(settings,records,issues)
            print(json.dumps({k:result[k] for k in ('counts','backup','report','issues')},ensure_ascii=False,indent=2));return
        initialize(settings)
        if args.command=='repair-cues':
            from .cues import preview,run
            current=preview(settings.database)
            print(json.dumps(current,ensure_ascii=False,indent=2))
            print('只处理活动的 Ombre 旧导入 Scene；保留正文和已有 cues。会调用打标模型并产生费用。')
            if not args.apply:
                print('当前仅预览；使用 --apply 执行。');return
            print(json.dumps(asyncio.run(run(settings)),ensure_ascii=False,indent=2));return
        if args.command=='vectors':print(json.dumps(maintain(settings,args.mode),ensure_ascii=False));return
        source=unpack(args.source,root)
        plan=scan(source)
        print(json.dumps(plan['summary'],ensure_ascii=False,indent=2))
        if plan['errors']:
            print(json.dumps(plan['errors'],ensure_ascii=False,indent=2))
            raise SystemExit('请先处理输入错误，再继续迁移。')
        if args.command=='scan':return
        options=json.loads(args.options.read_text('utf-8'))
        migration=Migration(settings,plan,options)
        try:
            migration.backup()
            print(json.dumps(import_models(settings.database,plan['root']),ensure_ascii=False))
            save_settings(settings.database,{'identity':{k:options[k] for k in ('user_name','ai_name')}})
            domains(settings.database)
            for task,label in [('operit_tagging','打标／实体／cues' if migration.options['generate_cues'] else '打标／实体'),('embedding','嵌入'),('reranker','重排')]:
                ensure_model(settings.database,task,label)
            print('下一步将调用模型：重新打标、提取实体'+('与不含名字的 cues' if migration.options['generate_cues'] else '（不生成 cues）')+'，并准备正文向量；若开启长文分段，按已保存门槛（默认 500 字）补齐 passage 向量。')
            print('无法验证输入一致的旧向量需要重建；旧边只转成五种关系，updates 转 continues 并反向；不确定项保留原记录并报告，不生成“相关”边、不调用关系模型。')
            print('同时迁入扫描到的 Persona 状态／历史与照顾备忘，保留期限、已提醒次数与会话轮次；相关功能开关不自动开启。')
            if ask('确认开始／继续（yes）')!='yes':raise SystemExit('尚未开始模型处理。')
            migration.freeze_configuration()
            migration.import_history()
            migration.import_companion()
            migration.import_bodies()
            asyncio.run(migration.tag_all())
            from ..configured_models import prepare_selected,effective_settings
            reuse={}
            if not migration.done('all','vectors'):
                def reuse_before_fill(current,profile):reuse.update(reuse_legacy(current,profile,plan,migration.ids))
                report=prepare_selected(settings,before_fill=reuse_before_fill)
                migration.mark('all','vectors','done',{'reuse':reuse,'preparation':report})
            asyncio.run(migration.edges())
            from ..recall.legacy_indexes import refresh
            cue_report=asyncio.run(refresh(effective_settings(settings), retry_failed=True))
            if cue_report['cues'].get('failed_scenes'):raise ValueError('部分 cue passage 绑定失败，可继续重试')
            migration.mark('all','cue_bindings','done',cue_report)
            print(json.dumps({'status':'complete','stages':migration.report(),'vectors':reuse,'report_dir':str(migration.root)},ensure_ascii=False,indent=2))
        finally:
            migration.report();migration.close()


if __name__=='__main__':main()
