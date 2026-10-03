"""Deployment-neutral recall settings. Published private domain choices are config."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RecallPolicy:
    direct_threshold: float = 0.65
    body_candidate_threshold: float = 0.50
    cue_candidate_threshold: float = 0.55
    max_cards: int = 2
    candidate_limit: int = 50
    domains: dict[str, str] = field(default_factory=dict)
    domain_rules: dict[str, dict[str, str]] = field(default_factory=dict)
    routing_file: str | None = None
    germany_policy_file: str | None = None
    passages_enabled: bool = False
    passage_min_chars: int = 500

    @classmethod
    def from_config(cls, raw):
        unknown = raw.keys() - {"direct_threshold", "body_candidate_threshold", "cue_candidate_threshold", "max_cards", "candidate_limit", "domains", "domain_rules", "routing_file", "germany_policy_file", "passages_enabled", "passage_min_chars"}
        if unknown:
            raise ValueError(f"Unknown recall policy fields: {', '.join(sorted(unknown))}")
        policy = cls(**raw)
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
