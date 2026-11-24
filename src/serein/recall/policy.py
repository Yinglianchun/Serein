"""Deployment-neutral recall settings. Published private domain choices are config."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RecallPolicy:
    direct_threshold: float = 0.65
    body_candidate_threshold: float = 0.50
    cue_candidate_threshold: float = 0.55
    direct_pool_limit: int = 20
    max_cards: int = 2
    candidate_limit: int = 50
    domains: dict[str, str] = field(default_factory=dict)
    domain_rules: dict[str, dict[str, str]] = field(default_factory=dict)
    routing_file: str | None = None
    germany_policy_file: str | None = None
    passages_enabled: bool = False
    passage_min_chars: int = 500
    embedding_batch_size: int = 16
    embedding_concurrency: int = 1
    index_catch_up: bool = False
    cooldown_mode: str = 'rounds'
    cooldown_rounds: int = 5
    cooldown_hours: int = 24

    @classmethod
    def from_config(cls, raw):
        unknown = raw.keys() - {"direct_threshold", "body_candidate_threshold", "cue_candidate_threshold", "direct_pool_limit", "max_cards", "candidate_limit", "domains", "domain_rules", "routing_file", "germany_policy_file", "passages_enabled", "passage_min_chars", "embedding_batch_size", "embedding_concurrency", "index_catch_up", "cooldown_mode", "cooldown_rounds", "cooldown_hours"}
        if unknown:
            raise ValueError(f"Unknown recall policy fields: {', '.join(sorted(unknown))}")
        policy = cls(**raw)
        if policy.cooldown_mode not in ('rounds', 'hours'):
            raise ValueError('Cooldown mode must be rounds or hours')
        if any(type(value) is not int or not 1 <= value <= 10000
               for value in (policy.cooldown_rounds, policy.cooldown_hours)):
            raise ValueError('Cooldown duration must be an integer between 1 and 10000')
        if type(policy.embedding_batch_size) is not int or not 1<=policy.embedding_batch_size<=64:
            raise ValueError('embedding_batch_size must be an integer between 1 and 64')
        if type(policy.embedding_concurrency) is not int or not 1<=policy.embedding_concurrency<=4:
            raise ValueError('embedding_concurrency must be an integer between 1 and 4')
        if type(policy.index_catch_up) is not bool:raise ValueError('index_catch_up must be boolean')
        if type(policy.direct_pool_limit) is not int or not 6 <= policy.direct_pool_limit <= 100:
            raise ValueError('direct_pool_limit must be an integer between 6 and 100')
        thresholds=(policy.direct_threshold,policy.body_candidate_threshold,policy.cue_candidate_threshold)
        if any(type(value) not in (int,float) for value in thresholds):
            raise ValueError('Recall thresholds must be numbers')
        if type(policy.passages_enabled) is not bool: raise ValueError('passages_enabled must be boolean')
        if type(policy.passage_min_chars) is not int or not 1 <= policy.passage_min_chars <= 100000:
            raise ValueError('passage_min_chars must be an integer between 1 and 100000')
        if (not 0 <= policy.direct_threshold <= 1
                or not 0 <= policy.body_candidate_threshold <= 1
                or not 0 <= policy.cue_candidate_threshold <= 1
                or not 1 <= policy.max_cards <= 100 or not 1 <= policy.candidate_limit <= 100):
            raise ValueError("Invalid recall threshold or result budget")
        if any(value not in {"normal", "explicit_only", "excluded"} for value in policy.domains.values()):
            raise ValueError("Domain policies must be normal, explicit_only, or excluded")
        if any(kind not in {'event', 'scene'} or any(value not in {'normal', 'explicit_only', 'excluded'}
               for value in rules.values()) for kind, rules in policy.domain_rules.items()):
            raise ValueError('Invalid per-kind domain policies')
        return policy
