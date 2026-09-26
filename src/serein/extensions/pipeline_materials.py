"""Per-Event writing choices grounded in the frozen owned originals."""

USES = {'main', 'background', 'omit', 'mixed'}

PROMPT = """
decision_review.events 每项增加 materials，覆盖该 Event 全部 owned 来源（包括旧来源），逐条输出：
{"source_message_id":1,"use":"main|background|omit|mixed","reason":"简短依据","omit_quotes":[]}
main 是实际起因、推进、结果、必要回应或有区别的关系语气；background 是理解本事不可缺少的前提；omit 是与本事无关的附带内容；mixed 是同条消息含保留内容与可省略旁支。main/background 的 omit_quotes 为空；mixed 必须列出可省略的逐字片段，omit 可列原文。不得遗漏或重复 owned 消息。
先判断实际在谈什么。吃饭、睡觉、早安、晚安只有在附带问候、状态提醒或结束聊天时省略；本来就在谈饮食睡眠的体验，或它改变了决定、行动、结果，则保留。不按关键词删，不把亲昵、玩笑一律当旁支。
同条消息含决定、必要回应或结果，不代表整条都是 main。逐段核对：若同时含与本事无关的作息提醒、告别或任务回执，标为 mixed，并在 omit_quotes 列出可省略的逐字片段；保留决定和接住决定的回应，不能因为它们重要就连带保留告别。亲昵称呼不使普通告别自动成为本事的独特关系表达；若告别、约定或玩笑本身正在被讨论，仍保留。
保留拒绝、暂缓、纠正、条件、因果前提及改变前文走向的回应；不能因信息相近删掉承载不同态度、关系或语气的表达。最后一条消息不自动是活动结尾。
这里只标本 Event 的写作用途，不删原文、不改变 ownership；桥接消息在不同 Event 的用途可以不同。纯旁支与仅背景不用于凑实质对话轮数。
"""

WRITER_RULE = """
<curator_materials_json> 是 Curator 按本 Event 原文判定的写作用途，不是新的事实来源。main 保留实际展开与有区别的语气；background 仅保留理解主线不可缺少的前提；omit 不进入正文、命题组或细节清单；mixed 保留有效回应，省略 omit_quotes 标出的旁支。完整原文仍供核对，不因保留来源绑定而将旁支写成结尾。不得用省略片段的同义改写绕过取舍，也不得删掉必要否定、条件、因果或被回应内容。若标注与必要语义冲突，以完整原文为准并在 discarded_details 简述冲突。
"""


def attach(review, output, plan, component):
    """Validate Curator annotations, then pass them to the matching Writer job."""
    if not component.get('writer_material_review'):
        return
    messages = {int(item['id']): str(item.get('content') or '')
                for item in [*component.get('context_messages', []), *component.get('messages', [])]}
    if not isinstance(review, dict) or not isinstance(review.get('events'), list):
        raise ValueError('Curator material review is missing')
    accepted = {event['event_ref']: event for event in plan['events']}
    for row in review['events']:
        event = output['events'][row['event_index']]
        materials = row.get('materials')
        if not isinstance(materials, list):
            raise ValueError('Every Event needs materials for each owned source')
        owned = {item['source_message_id'] for item in event['source_bindings']}
        seen = set()
        for item in materials:
            if not isinstance(item, dict) or set(item) != {'source_message_id', 'use', 'reason', 'omit_quotes'}:
                raise ValueError('Invalid Curator material fields')
            source_id, use, quotes = item['source_message_id'], item['use'], item['omit_quotes']
            if type(source_id) is not int or source_id not in owned or source_id in seen:
                raise ValueError('Materials must exactly cover owned sources once')
            seen.add(source_id)
            if use not in USES or not isinstance(item['reason'], str) or not item['reason'].strip():
                raise ValueError('Invalid material use or reason')
            if not isinstance(quotes, list) or any(not isinstance(quote, str) or not quote.strip()
                    or quote not in messages.get(source_id, '') for quote in quotes):
                raise ValueError('Material omission quotes must be verbatim')
            if (use in {'main', 'background'} and quotes) or (use == 'mixed' and not quotes):
                raise ValueError('Mixed sources need omission quotes; main/background cannot omit')
            if use == 'mixed':
                remaining = messages.get(source_id, '')
                for quote in quotes:
                    remaining = remaining.replace(quote, '', 1)
                if not remaining.strip():
                    raise ValueError('Mixed source must retain content')
        if seen != owned:
            raise ValueError('Materials must exactly cover owned sources once')
        if event['event_ref'] in accepted:
            accepted[event['event_ref']]['source_materials'] = materials


def substantive_ids(event):
    materials = event.get('source_materials')
    if materials is None:
        return event['source_message_ids']
    return [item['source_message_id'] for item in materials if item['use'] in {'main', 'mixed'}]
