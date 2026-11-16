"""Free-text -> item master matching (policy 1.1).

Deterministic and explainable: token coverage in both directions, numeric/model tokens weighted heavily
and required, abbreviations expanded, light fuzzy matching for typos.  It never invents an item: if the
best score is not convincingly above the rest the caller gets candidates (chat asks, batch -> NEW item).
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

ABBREV = {
    "brg": "bearing", "brng": "bearing", "brgs": "bearing", "bearng": "bearing",
    "hyd": "hydraulic", "hydr": "hydraulic", "hydrolic": "hydraulic",
    "pneu": "pneumatic", "pneum": "pneumatic", "pneumatics": "pneumatic",
    "cyl": "cylinder", "cylindr": "cylinder",
    "sol": "solenoid", "elec": "electrical", "mtr": "meter",
    "rod": "electrode", "rods": "electrode", "electrodes": "electrode",
    "welding": "welding", "weld": "welding",
    "gloves": "glove", "gloves.": "glove", "glov": "glove",
    "boxes": "box", "bags": "bag", "belts": "belt", "vbelt": "v belt",
    "kb": "keyboard", "mouse": "mouse", "lappy": "laptop", "pc": "laptop",
    "sheets": "sheet", "plates": "plate", "wheels": "wheel", "shoe": "shoes",
    "seals": "seal", "nuts": "nut", "bolts": "bolt", "screws": "screw", "washers": "washer",
    "cables": "cable", "oils": "oil", "lube": "lube", "mcbs": "mcb", "sensors": "sensor",
    "monitors": "monitor", "helmets": "helmet", "goggle": "goggles", "plug": "plugs",
    "cum": "cum", "ss": "ss", "ms": "ms",
}
STOP = {"of", "the", "a", "an", "and", "with", "to", "in", "on", "pcs", "pc", "nos", "no", "need", "please", "pls", "qty", "unit", "units"}
LOW_WEIGHT = {"inch", "mm", "kg", "mtr", "ltr", "pair", "nos", "x"}
NOISE_RE = re.compile(
    r"\b(as per sample|per sample|same as before|urgent(ly)?|asap|emergency|repeat(ed)?|re-?order|again|new)\b|\(.*?repeat.*?\)",
    re.I,
)
FOR_TAIL_RE = re.compile(r"\bfor\s+[a-z ]+$", re.I)
TOKEN_RE = re.compile(r"[a-z0-9.]+")


def _has_digit(t: str) -> bool:
    return any(c.isdigit() for c in t)


def tokenize(text: str) -> list[str]:
    s = (text or "").lower()
    s = NOISE_RE.sub(" ", s)
    s = FOR_TAIL_RE.sub(" ", s)
    s = s.replace("&", " and ")
    s = re.sub(r'(\d)\s*(?:"|”|″|in\b)', r"\1 inch", s)
    s = s.replace("v-belt", "v belt").replace("v belt", "vbelt")
    toks = []
    for raw in TOKEN_RE.findall(s):
        t = raw.strip(".")
        if not t or t in STOP:
            continue
        t = ABBREV.get(t, t)
        for piece in t.split():
            if piece:
                toks.append(piece)
    return toks


def _stem(t: str) -> str:
    return t[:-1] if len(t) > 4 and t.endswith("s") and not _has_digit(t) else t


def _tok_match(a: str, b: str) -> float:
    """similarity of query token a against item token b in [0, 1]."""
    if a == b:
        return 1.0
    if _stem(a) == _stem(b):
        return 1.0
    if _has_digit(a) or _has_digit(b):
        return 0.0  # model numbers / sizes must match exactly
    if len(a) >= 4 and len(b) >= 4 and (b.startswith(a) or a.startswith(b)):
        return 0.9
    if len(a) >= 5 and len(b) >= 5:
        r = difflib.SequenceMatcher(None, a, b).ratio()
        if r >= 0.85:
            return r
    return 0.0


def _weight(t: str) -> float:
    if _has_digit(t):
        return 3.0
    if t in LOW_WEIGHT:
        return 0.3
    return 1.0


@dataclass
class Candidate:
    item_code: str
    description: str
    score: float
    detail: str = ""


@dataclass
class MatchResult:
    item_code: str | None
    status: str  # 'exact_code' | 'matched' | 'ambiguous' | 'none'
    score: float = 0.0
    candidates: list[Candidate] = field(default_factory=list)
    note: str = ""


class ItemMatcher:
    ACCEPT = 0.80
    MARGIN = 0.08
    SHOW = 0.35

    def __init__(self, items: dict[str, dict]):
        self.items = items
        self._tok = {code: tokenize(it["description"]) for code, it in items.items()}

    def score(self, query_tokens: list[str], code: str) -> float:
        itoks = self._tok[code]
        if not query_tokens or not itoks:
            return 0.0
        # item coverage: how much of the item description does the query cover?
        num = den = 0.0
        for it in itoks:
            w = _weight(it)
            den += w
            num += w * max((_tok_match(q, it) for q in query_tokens), default=0.0)
        item_cov = num / den
        # query coverage: how much of the query is explained by the item description?
        num = den = 0.0
        for q in query_tokens:
            w = _weight(q)
            den += w
            num += w * max((_tok_match(q, it) for it in itoks), default=0.0)
        query_cov = num / den
        s = 0.65 * item_cov + 0.35 * query_cov
        # every model/size token of the item must appear in the query
        item_nums = [t for t in itoks if _has_digit(t)]
        if item_nums and not any(_tok_match(q, t) == 1.0 for t in item_nums for q in query_tokens):
            s = min(s, 0.45)
        # a number in the query that the item does not have is a strong contradiction
        q_nums = [q for q in query_tokens if _has_digit(q)]
        if q_nums and item_nums and not any(_tok_match(q, t) == 1.0 for q in q_nums for t in item_nums):
            s = min(s, 0.40)
        return s

    def rank(self, text: str, top: int = 5) -> list[Candidate]:
        q = tokenize(text)
        scored = [Candidate(code, it["description"], round(self.score(q, code), 3)) for code, it in self.items.items()]
        scored.sort(key=lambda c: (-c.score, c.item_code))
        return [c for c in scored[:top] if c.score >= self.SHOW]

    def match(self, description: str, item_code: str | None = None) -> MatchResult:
        code = (item_code or "").strip().upper()
        if code:
            if code in self.items:
                return MatchResult(code, "exact_code", 1.0, [Candidate(code, self.items[code]["description"], 1.0)])
            note = f"item code {code} is not in the item master"
        else:
            note = ""
        cands = self.rank(description)
        if not cands:
            return MatchResult(None, "none", 0.0, [], note)
        best = cands[0]
        second = cands[1].score if len(cands) > 1 else 0.0
        if (best.score >= self.ACCEPT and best.score - second >= self.MARGIN) or (best.score >= 0.70 and best.score - second >= 0.25):
            return MatchResult(best.item_code, "matched", best.score, cands, note)
        return MatchResult(None, "ambiguous" if best.score >= 0.5 else "none", best.score, cands, note)
