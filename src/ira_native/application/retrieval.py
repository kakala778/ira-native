"""Deterministic K1 query decomposition for structured evidence retrieval.

K1 splits model identifiers, attributes, document/table/section clues and short
fallback terms, then searches each term within the caller's authorized scope.
It does not expand model equivalence or follow continuation-table headers.
"""

import html
import json
import re
from dataclasses import dataclass, field

from ira_native.application.service import EvidenceService
from ira_native.domain.models import Evidence

_TAG = re.compile(r"<[^>]+>")
_MODEL = re.compile(
    r"(?<![A-Za-z0-9_-])((?:NX|ON|SS|ZXG|SYN)-[A-Z0-9]+(?:-[A-Z0-9]+)*)(?![A-Za-z0-9_-])",
    re.I,
)
_CJK = re.compile(r"[\u4e00-\u9fff]+")
_ASCII = re.compile(r"[A-Za-z0-9][A-Za-z0-9+.\-]*")
_ASCII_PHRASE = re.compile(r"[A-Za-z0-9][A-Za-z0-9+.\-]*(?:\s+[A-Za-z0-9][A-Za-z0-9+.\-]*)+")
_TABLE_CLUE = re.compile(r"表\s*\d+(?:\s*[-–]\s*\d+)?(?:\s*[（(]\s*续\s*[）)])?")
_SECTION_CLUE = re.compile(r"(?<!\d)\d+(?:\.\d+){1,3}(?!\d)")
_INTERFACE_COUNT_CLUE = re.compile(
    r"(?<![A-Za-z0-9_.-])\d+\s*(?:个\s*(?:[A-Za-z][A-Za-z0-9+-]*\s*)?)?"
    r"(?:电口|光口|网口|接口)"
)

DOC_CLUES = (
    "规格书",
    "投标偏差表",
    "现场测试记录",
    "安装手册",
    "设备清单",
    "术语表",
    "选型说明",
    "偏差表",
)
CONFLICT_MARKERS = ("与", "和", "及", "一致", "冲突", "两侧", "比较", "分别", "各自", "是否一致")
STOPWORDS = {
    "的",
    "与",
    "和",
    "及",
    "请",
    "给出",
    "检索",
    "查询",
    "分别",
    "是否",
    "支持",
    "作为",
    "依据",
    "要求",
    "证据",
    "两侧",
    "规格书",
    "投标偏差表",
    "现场测试记录",
    "安装手册",
    "设备清单",
    "术语表",
    "选型说明",
    "表",
    "续",
}


@dataclass(frozen=True)
class Attribute:
    canonical: str
    variants: tuple[str, ...]
    short: tuple[str, ...] = ()


ATTRIBUTES = (
    Attribute(
        "最大功耗",
        (
            "最大功耗",
            "功耗",
            "耗电",
            "耗电量",
            "省电",
            "电源功耗",
            "功率消耗",
            "power consumption",
            "power draw",
            "power usage",
            "power",
        ),
        ("功耗",),
    ),
    Attribute(
        "接收灵敏度",
        ("接收灵敏度", "收光灵敏度", "灵敏度", "receive sensitivity", "sensitivity"),
        ("灵敏度",),
    ),
    Attribute(
        "接收光功率",
        ("接收光功率", "收光功率", "光功率", "receive optical power", "optical power"),
        ("光功率",),
    ),
    Attribute("插入损耗", ("插入损耗", "插损", "insertion loss"), ("损耗",)),
    Attribute("最大分光比", ("最大分光比", "分光比", "split ratio"), ("分光比",)),
    Attribute("端口数", ("端口数", "端口数量", "口数", "端口", "ports", "port count"), ("端口",)),
    Attribute("传输距离", ("传输距离", "transmission distance", "distance"), ("距离",)),
    Attribute(
        "工作温度",
        ("工作温度", "运行温度", "温度", "operating temperature", "temperature"),
        ("温度",),
    ),
    Attribute("尺寸", ("尺寸", "dimensions", "dimension"), ("尺寸",)),
    Attribute("上联", ("上联", "上行口", "上行端口", "uplink port", "uplink"), ("上联",)),
    Attribute("仪表校准", ("仪表校准", "校准", "calibration"), ("校准",)),
    Attribute("测试条件", ("测试条件", "test condition", "test conditions"), ("测试",)),
    Attribute("条款", ("条款", "clause"), ("条款",)),
)

# An attribute hit is stronger when the block contains an observable value pattern.
VALUE_PATTERNS = {
    "最大功耗": re.compile(r"(?:功耗|power)[^\n]{0,24}?\d+(?:\.\d+)?\s*(?:w|瓦)"),
    "接收灵敏度": re.compile(r"灵敏度[^\n]{0,28}?[-≤<>≥]?\s*\d+(?:\.\d+)?\s*dbm"),
    "接收光功率": re.compile(r"(?:接收光功率|光功率)[^\n]{0,28}?-?\d+(?:\.\d+)?\s*dbm"),
    "插入损耗": re.compile(r"插入损耗[^\n]{0,24}?≤?\s*\d+(?:\.\d+)?\s*db"),
    "最大分光比": re.compile(r"分光比[^\n]{0,24}?1\s*[:：]\s*\d+"),
    "端口数": re.compile(r"\d+\s*个?\s*(?:gpon\s*)?(?:上行)?端口"),
    "传输距离": re.compile(r"传输距离[^\n]{0,24}?\d+(?:\.\d+)?\s*km"),
    "工作温度": re.compile(r"工作温度[^\n]{0,28}?-?\d+\s*℃"),
    "尺寸": re.compile(r"尺寸[^\n]{0,24}?\d+(?:\.\d+)?\s*mm"),
    "条款": re.compile(r"条款\s*\d+(?:\.\d+)+"),
}


@dataclass(frozen=True)
class Scope:
    parse_run_id: str
    source_asset_id: str
    artifact_asset_id: str
    physical_pages: tuple[int, int] | None = None


class ScopeInvariantError(PermissionError):
    def __init__(self, rejected: int):
        super().__init__("scope_invariant_failed")
        self.rejected = rejected


@dataclass
class Hit:
    evidence: Evidence
    page: int
    text: str
    score: float
    reasons: list[str]
    matched_terms: list[str] = field(default_factory=list)
    visible: str = ""
    context_tokens: int = 0
    context_flags: dict = field(default_factory=dict)

    @property
    def evidence_id(self):
        return self.evidence.evidence_id


def make_scope(
    service: EvidenceService, parse_run_id: str, physical_pages: tuple[int, int] | None = None
) -> Scope:
    run = service.get_run(parse_run_id)
    return Scope(
        parse_run_id,
        run.source_asset_id,
        run.artifact_asset_id,
        tuple(physical_pages) if physical_pages else None,
    )


def _check_scope(service: EvidenceService, scope: Scope) -> None:
    run = service.get_run(scope.parse_run_id)
    if (run.source_asset_id, run.artifact_asset_id) != (
        scope.source_asset_id,
        scope.artifact_asset_id,
    ):
        raise PermissionError("scope_mismatch")
    if scope.physical_pages is not None:
        low, high = scope.physical_pages
        if not 1 <= low <= high:
            raise PermissionError("scope_pages_invalid")


def _in_scope(evidence: Evidence, scope: Scope) -> bool:
    if (
        evidence.parse_run_id,
        evidence.source_asset_id,
        evidence.artifact_asset_id,
    ) != (scope.parse_run_id, scope.source_asset_id, scope.artifact_asset_id):
        return False
    if scope.physical_pages is not None:
        low, high = scope.physical_pages
        if not low <= evidence.physical_page <= high:
            return False
    return True


def _plain(value) -> str:
    if isinstance(value, str):
        return html.unescape(_TAG.sub(" ", value)) if "<" in value and ">" in value else value
    return json.dumps(value, ensure_ascii=False)


def evidence_text(evidence: Evidence) -> str:
    parts = [_plain(evidence.raw_content)]
    for key in ("captions", "footnotes"):
        for annotation in evidence.native_context.get(key) or []:
            if isinstance(annotation, dict) and isinstance(annotation.get("content"), str):
                parts.append(annotation["content"])
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def token_count(text: str) -> int:
    # Diagnostic estimate only. Loading a tokenizer can download its vocabulary.
    return max(1, len(text) // 2)


def _contains(term: str, folded: str) -> bool:
    if not term:
        return False
    if term.isascii():
        return (
            re.search(
                r"(?<![a-z0-9])" + re.escape(term.casefold()) + r"(?![a-z0-9])",
                folded,
            )
            is not None
        )
    return term.casefold() in folded


def models_in(text: str) -> list[str]:
    return list(dict.fromkeys(match.upper() for match in _MODEL.findall(text)))


def _model_in(text: str, model: str) -> bool:
    return (
        re.search(
            r"(?<![A-Z0-9_-])" + re.escape(model.upper()) + r"(?![A-Z0-9_-])",
            text.upper(),
        )
        is not None
    )


def _plan(need: str) -> dict:
    folded = need.casefold()
    models = models_in(need)
    attributes = [
        attribute
        for attribute in ATTRIBUTES
        if any(_contains(variant, folded) for variant in attribute.variants)
    ]
    doc_clues = [clue for clue in DOC_CLUES if clue in need]
    table_clues = list(dict.fromkeys(_normalize_clue(x) for x in _TABLE_CLUE.findall(need)))
    section_clues = list(dict.fromkeys(_SECTION_CLUE.findall(need)))
    literal_phrases = [
        phrase
        for phrase in _ASCII_PHRASE.findall(need)
        if not any(_model_in(phrase, model) for model in models)
    ]
    literal_terms = []
    for term in _ASCII.findall(need):
        if term.casefold() in {model.casefold() for model in models}:
            continue
        if len(term) >= 2 and term.casefold() not in STOPWORDS:
            literal_terms.append(term)
    for run in _CJK.findall(need):
        if run in STOPWORDS or len(run) < 2:
            continue
        if any(run in clue for clue in doc_clues) or any(run in a.canonical for a in attributes):
            continue
        literal_terms.append(run)
    required_phrases = (
        [phrase for phrase in literal_phrases if re.search(r"\d", phrase)] if not attributes else []
    )
    primary = []
    if models:
        primary.extend(models)
    for attribute in attributes:
        primary.extend(attribute.variants)
    primary.extend(doc_clues)
    primary.extend(table_clues)
    primary.extend(section_clues)
    primary.extend(literal_phrases)
    primary.extend(literal_terms)
    return {
        "models": models,
        "attributes": attributes,
        "doc_clues": doc_clues,
        "table_clues": table_clues,
        "section_clues": section_clues,
        "literal_phrases": literal_phrases,
        "literal_terms": literal_terms,
        "required_phrases": required_phrases,
        "primary_terms": list(dict.fromkeys(term for term in primary if term.strip())),
    }


def _normalize_clue(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("–", "-")).strip()


def should_decompose(query: str) -> bool:
    """Route only structured information needs through the decomposed planner."""
    plan = _plan(query)
    return bool(
        plan["attributes"]
        or plan["table_clues"]
        or plan["section_clues"]
        or len(plan["models"]) > 1
        or (plan["models"] and _INTERFACE_COUNT_CLUE.search(query))
    )


def _search_terms(service: EvidenceService, scope: Scope, terms: list[str], limit: int):
    results = {}
    for term in terms:
        response = service.search_evidence(
            term,
            parse_run_id=scope.parse_run_id,
            source_asset_id=scope.source_asset_id,
            physical_pages=scope.physical_pages,
            limit=limit,
        )
        rejected = sum(not _in_scope(evidence, scope) for evidence in response.items)
        if rejected:
            raise ScopeInvariantError(rejected)
        for evidence in response.items:
            entry = results.setdefault(
                evidence.evidence_id,
                {"evidence": evidence, "terms": [], "text": evidence_text(evidence)},
            )
            if term not in entry["terms"]:
                entry["terms"].append(term)
    return results


def search_k1(service: EvidenceService, scope: Scope, need: str, k: int = 6) -> list[Hit]:
    service._search_query(need, "literal")
    _check_scope(service, scope)
    plan = _plan(need)
    collected = _search_terms(service, scope, plan["primary_terms"], max(12, k * 4))
    # Short fallback is only used when the attribute's primary terms produced no hit.
    for attribute in plan["attributes"]:
        if any(
            any(_contains(variant, entry["text"].casefold()) for variant in attribute.variants)
            for entry in collected.values()
        ):
            continue
        extra = _search_terms(service, scope, list(attribute.short), max(12, k * 4))
        for evidence_id, entry in extra.items():
            current = collected.setdefault(
                evidence_id, {"evidence": entry["evidence"], "terms": [], "text": entry["text"]}
            )
            for term in entry["terms"]:
                if term not in current["terms"]:
                    current["terms"].append(term)

    hits = []
    allowed_pages = None
    for clue_key, required in (
        ("doc_clues", len(plan["doc_clues"]) == 1 and not any(m in need for m in CONFLICT_MARKERS)),
        ("table_clues", True),
        ("section_clues", True),
    ):
        clues = plan[clue_key]
        if not clues or not required:
            continue
        pages = {
            entry["evidence"].physical_page
            for entry in collected.values()
            if any(clue in entry["text"] for clue in clues)
        }
        if pages:
            allowed_pages = pages if allowed_pages is None else allowed_pages & pages

    for entry in collected.values():
        evidence = entry["evidence"]
        if allowed_pages is not None and evidence.physical_page not in allowed_pages:
            continue
        text = entry["text"]
        folded = text.casefold()
        score = 0.0
        reasons = []
        if plan["models"]:
            matched = [model for model in plan["models"] if _model_in(text, model)]
            if not matched:
                continue
            score += 4.0 * len(matched)
            reasons.extend(f"model:{model}" for model in matched)
        matched_attributes = []
        for attribute in plan["attributes"]:
            if any(_contains(variant, folded) for variant in attribute.variants + attribute.short):
                matched_attributes.append(attribute.canonical)
                if attribute.canonical.casefold() in folded:
                    score += 3.0
                else:
                    score += 2.0
                reasons.append(f"attr:{attribute.canonical}")
                pattern = VALUE_PATTERNS.get(attribute.canonical)
                if pattern is not None and pattern.search(folded):
                    score += 1.5
                    reasons.append(f"value:{attribute.canonical}")
        if plan["attributes"] and not matched_attributes:
            continue
        for clue in plan["doc_clues"]:
            if clue in text:
                score += 2.0
                reasons.append(f"doc:{clue}")
        for clue in plan["table_clues"]:
            if clue in text:
                score += 2.0
                reasons.append(f"table:{clue}")
        for clue in plan["section_clues"]:
            if clue in text:
                score += 1.0
                reasons.append(f"section:{clue}")
        for phrase in plan["literal_phrases"]:
            if phrase.casefold() in folded:
                score += 2.0
                reasons.append(f"phrase:{phrase}")
        for term in plan["literal_terms"]:
            if _contains(term, folded):
                score += 1.0
                reasons.append(f"term:{term}")
        if plan["required_phrases"] and not any(
            _contains(phrase, folded) for phrase in plan["required_phrases"]
        ):
            continue
        literal_required = (
            bool(plan["literal_terms"] or plan["literal_phrases"])
            and not plan["attributes"]
            and not plan["doc_clues"]
            and not plan["table_clues"]
            and not plan["section_clues"]
        )
        if literal_required and not any(
            _contains(term, folded) for term in plan["literal_terms"] + plan["literal_phrases"]
        ):
            continue
        hits.append(
            Hit(
                evidence=evidence,
                page=evidence.physical_page,
                text=text,
                score=score,
                reasons=reasons,
                matched_terms=entry["terms"],
            )
        )

    hits.sort(key=lambda hit: (-hit.score, hit.page, hit.evidence.source_pointer))
    selected = hits[:k]
    for hit in selected:
        hit.visible = hit.text[:220]
        hit.context_tokens = token_count(hit.visible)
    return selected
