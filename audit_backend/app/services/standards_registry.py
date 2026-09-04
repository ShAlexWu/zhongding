"""Official standard catalogue lookup and deterministic version comparison."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class StandardRecord:
    base: str
    current: str
    amendments: tuple[str, ...]
    source_url: str


@dataclass(frozen=True)
class StandardReviewItem:
    reference: str
    verdict: str
    current: str = ""
    amendments: tuple[str, ...] = ()
    source_url: str = ""
    checked_at: str = ""
    live_verified: bool = False
    note: str = ""


@dataclass(frozen=True)
class StandardReview:
    verdict: str
    items: tuple[StandardReviewItem, ...] = field(default_factory=tuple)


_CATALOG = {
    "ISO 668": StandardRecord(
        "ISO 668",
        "ISO 668:2020",
        ("Amd 1:2022",),
        "https://www.iso.org/standard/76912.html",
    ),
    "ISO 6346": StandardRecord(
        "ISO 6346",
        "ISO 6346:2022",
        (),
        "https://www.iso.org/standard/83558.html",
    ),
    "ISO 1161": StandardRecord(
        "ISO 1161",
        "ISO 1161:2016",
        (),
        "https://www.iso.org/standard/65553.html",
    ),
    "ISO 1496-1": StandardRecord(
        "ISO 1496-1",
        "ISO 1496-1:2013",
        ("Amd 1:2016", "Amd 2:2024"),
        "https://www.iso.org/standard/59672.html",
    ),
    "ISO 830": StandardRecord(
        "ISO 830",
        "ISO 830:2024",
        (),
        "https://www.iso.org/standard/85378.html",
    ),
    "JIS G 3193": StandardRecord(
        "JIS G 3193",
        "JIS G 3193:2025",
        (),
        "https://webdesk.jsa.or.jp/books/W11M0090/?bunsyo_id=JIS+G+3193%3A2025",
    ),
}


def normalize_standard_reference(value: str) -> str:
    """Normalize the citation, not its edition semantics."""
    text = " ".join(value.upper().replace("STANDARD", " ").split())
    iso = re.search(
        r"\bISO\s*[/ -]?\s*(\d+)(?:\s*[/ -]\s*(\d+))?\s*(?::\s*(\d{4}))?",
        text,
    )
    if iso:
        base = f"ISO {iso.group(1)}"
        if iso.group(2):
            base += f"-{iso.group(2)}"
        return base + (f":{iso.group(3)}" if iso.group(3) else "")
    jis = re.search(r"\bJIS\s*([A-Z])\s*(\d+)\s*(?:[-:]\s*(\d{4}))?", text)
    if jis:
        base = f"JIS {jis.group(1)} {jis.group(2)}"
        return base + (f":{jis.group(3)}" if jis.group(3) else "")
    return text.strip(" .;；，,")


class OfficialStandardsRegistry:
    """Small interface over the maintained catalogue plus optional official-page check."""

    def __init__(self, live: bool = True, timeout_s: float = 6.0) -> None:
        self.live = live
        self.timeout_s = timeout_s
        self._live_cache: dict[str, bool] = {}

    def review(self, references: list[str]) -> StandardReview:
        items = tuple(self._review_one(reference) for reference in references)
        verdicts = {item.verdict for item in items}
        verdict = "fail" if "fail" in verdicts else "warning" if "warning" in verdicts else "pass"
        return StandardReview(verdict, items)

    def _review_one(self, reference: str) -> StandardReviewItem:
        normalized = normalize_standard_reference(reference)
        base, _, cited_year = normalized.partition(":")
        record = _CATALOG.get(base)
        checked_at = date.today().isoformat()
        if record is None:
            return StandardReviewItem(
                normalized,
                "warning",
                checked_at=checked_at,
                note="官方目录快照中无对应条目",
            )

        live_verified = self._verify_official_page(record) if self.live else False
        if cited_year and cited_year != record.current.rsplit(":", 1)[-1]:
            verdict = "fail"
            note = f"引用版本 {normalized} 已不是现行发布版"
        elif not cited_year:
            verdict = "warning"
            note = "说明书未注明版本年份，需确认实际采用版本"
        elif self.live and not live_verified:
            verdict = "warning"
            note = "官方页面实时核验失败，当前结果来自维护快照"
        else:
            verdict = "pass"
            note = "与现行发布版一致"
        return StandardReviewItem(
            normalized,
            verdict,
            current=record.current,
            amendments=record.amendments,
            source_url=record.source_url,
            checked_at=checked_at,
            live_verified=live_verified,
            note=note,
        )

    def _verify_official_page(self, record: StandardRecord) -> bool:
        cached = self._live_cache.get(record.source_url)
        if cached is not None:
            return cached
        try:
            request = Request(record.source_url, headers={"User-Agent": "zhongji-audit/1.0"})
            with urlopen(request, timeout=self.timeout_s) as response:  # noqa: S310 - allowlisted URLs
                body = response.read(1_000_000).decode("utf-8", errors="ignore")
            verified = record.current in body
        except Exception:  # noqa: BLE001 - network failure becomes auditable warning
            verified = False
        self._live_cache[record.source_url] = verified
        return verified
