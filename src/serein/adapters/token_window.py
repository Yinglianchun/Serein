"""Explicit local tokenizer budgets; no downloads or implicit credential lookup."""
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=8)
def _load(path):
    try:
        from transformers import AutoTokenizer
    except ImportError:
        raise ValueError('Install serein[tokenizer] to configure a local token window') from None
    if not Path(path).is_dir():
        raise ValueError('Configured local tokenizer directory is unavailable')
    return AutoTokenizer.from_pretrained(path, local_files_only=True, trust_remote_code=False)


class TokenWindow:
    def __init__(self, config):
        self.tokenizer = _load(config['path'])
        if not self.tokenizer.is_fast:
            raise ValueError('Token windows require a fast tokenizer with source offsets')
        self.maximum = config['max_tokens']
        if type(self.maximum) is not int or self.maximum < 8:
            raise ValueError('Token window must be an explicit positive input budget')

    def count(self, text, *, query=None):
        return len(self.tokenizer.encode(text) if query is None else self.tokenizer.encode(query, text))

    def fits(self, text, *, query=None):
        return self.count(text, query=query) <= self.maximum

    def spans(self, text, *, prefix='', query=None, max_chars=None):
        """Exact character spans, each verified against the complete model input."""
        if not self.fits(prefix, query=query) or (max_chars is not None and len(prefix) >= max_chars):
            raise ValueError('Query or instruction exhausts the configured token window')
        start = 0
        while start < len(text):
            low, high = start + 1, len(text)
            end = start
            while low <= high:
                middle = (low + high) // 2
                if (self.fits(prefix + text[start:middle], query=query) and
                        (max_chars is None or len(prefix) + middle-start <= max_chars)):
                    end, low = middle, middle + 1
                else:
                    high = middle - 1
            if end == start:
                raise ValueError('Token window cannot fit the next source character')
            if end == len(text):
                # Anchor the final window at the source end: a short trailing
                # overlap alone can otherwise split a tail fact across inputs.
                low = max(0, end-(max_chars-len(prefix))) if max_chars is not None else 0
                high, tail = start, start
                while low <= high:
                    middle = (low+high)//2
                    if self.fits(prefix+text[middle:end],query=query):
                        tail, high = middle, middle-1
                    else:
                        low = middle+1
                start = tail
            yield start, end
            if end == len(text):
                break
            # Overlap uses tokenizer offsets rather than a character guess.
            offsets = self.tokenizer(text[start:end], add_special_tokens=False,
                return_offsets_mapping=True)['offset_mapping']
            overlap = offsets[max(0, len(offsets) - min(16, len(offsets)//6))][0] if len(offsets) > 6 else end-start
            start += max(1, overlap)
