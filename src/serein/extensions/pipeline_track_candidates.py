"""Optional local candidate selection; persisted Track state stays complete."""
import math
import re
from collections import Counter
from datetime import datetime, timedelta, timezone

STOPWORDS = set('的 了 是 我 你 他 她 我们 你们 这个 那个 一个 这样 那样 然后 但是 就 也 都 啊 嗯 吧 呢 吗 好 可以 老公 哥哥 宝宝 哦 噢 哦哦 哦哦哦 噢噢 噢噢噢 嗯嗯 哈哈 哈哈哈 就是说 感觉'.split())


def tokens(text):
    import jieba
    return [word for token in jieba.lcut(text) if (word := token.strip().lower())
            and re.search(r'[\w\u4e00-\u9fff]', word) and word not in STOPWORDS
            and not re.fullmatch(r'[哦噢嗯]{2,}|哈{2,}', word)]


def stamp(value):
    try:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return (result if result.tzinfo else result.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def select(cards, messages, recent, policy):
    if not policy.get('track_candidates_enabled', False):
        return list(cards)
    anchors = [value for message in messages if (value := stamp(message.get('created_at'))) is not None]
    if not anchors:
        return list(cards)
    cutoff = max(anchors) - timedelta(hours=policy.get('track_direct_hours', 12))
    activity = {card['track_id']: max((value for turn in card.get('recent_turns', [])
                if (value := stamp(turn.get('created_at'))) is not None), default=None) for card in cards}
    # Missing activity cannot safely prove a card belongs outside the direct window.
    direct = {key for key, value in activity.items() if value is None or value >= cutoff}
    older = [card for card in cards if card['track_id'] not in direct]
    docs = [Counter(tokens('\n'.join([card.get('subject', ''), card.get('throughline', ''),
            *(turn.get('text', '') for turn in card.get('recent_turns', []))]))) for card in older]
    query = set(tokens('\n'.join(message.get('content', message.get('text', '')) for message in [*recent, *messages])))
    average = sum(sum(doc.values()) for doc in docs) / max(1, len(docs)) or 1
    frequency = Counter(term for doc in docs for term in doc)
    scores = []
    for card, doc in zip(older, docs):
        length = sum(doc.values())
        score = sum(math.log(1 + (len(docs) - frequency[term] + .5) / (frequency[term] + .5))
                    * doc[term] * 2.5 / (doc[term] + 1.5 * (.25 + .75 * length / average))
                    for term in query & doc.keys())
        if score > 0:
            scores.append((score, card['track_id']))
    scores.sort(key=lambda item: (-item[0], item[1]))
    selected = direct | {key for _, key in scores[:policy.get('track_candidate_limit', 8)]}
    # Rank decides inclusion; chronological presentation is stable across queries.
    return sorted((card for card in cards if card['track_id'] in selected),
                  key=lambda card: (activity[card['track_id']] or datetime.min.replace(tzinfo=timezone.utc), card['track_id']))
