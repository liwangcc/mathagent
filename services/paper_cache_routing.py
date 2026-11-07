"""Which template page a student page is.

Template pages may be different exams whose printed question numbers overlap,
so content decides first.  ``_PaperCacheRoutingMixin`` holds these ``PaperCache``
methods:

- ``_content_identity_winner`` finds the one page the student page's content
  belongs to from three independent signals: printed stem openings matched
  server-side against each page's confirmed question text
  (``_snippet_matches_stems``, ``_content_text``, ``_MIN_STEM_SNIPPET_CHARS``,
  ``_STEM_BIGRAM_CONTAINMENT``), the printed page caption, and the coarse visual
  fingerprint; conflicting signals send the page to review.
- ``_resolve_joint_page_type`` then checks that the numbers, printed labels
  (``_printed_label_set``, ``_printed_labels_overlap``, ``_routable_page_labels``,
  ``_normalize_page_label_text``), caption and page role agree with that page;
  anything short of exactly one consistent page goes to teacher review.

Moved unchanged from services/paper_cache.py, which still exposes these names.
"""

from services.paper_cache_base import (
    _normalize_question_number,
    _question_number_sort_key,
)


_PRINTED_LABEL_EDGE_PUNCTUATION = " .．。、,，:：;；()（）[]【】"


def _normalize_page_label_text(value: object) -> str:
    """Compare printed page/question labels without spacing or edge punctuation.

    ``"1."``, ``"（1）"`` and ``" 1 "`` read as the same printed label; case is
    folded for Latin captions.  An empty result means "not observed".
    """
    text = "".join(str(value or "").split())
    return text.strip(_PRINTED_LABEL_EDGE_PUNCTUATION).casefold()


# A printed-stem opening shorter than this is too generic ("计算", "解方程")
# to tell two exams apart.
_MIN_STEM_SNIPPET_CHARS = 6
_STEM_BIGRAM_CONTAINMENT = 0.7


def _content_text(value: object) -> str:
    """Printed text reduced to letters/digits/CJK for OCR-tolerant comparison."""
    import unicodedata

    text = unicodedata.normalize("NFKC", str(value or ""))
    return "".join(ch for ch in text if ch.isalnum()).casefold()


def _snippet_matches_stems(snippet: str, stems: list[str]) -> bool:
    """Whether a normalized stem opening seen on a student page is one of these stems."""
    if any(snippet in stem for stem in stems):
        return True
    grams = {snippet[index:index + 2] for index in range(len(snippet) - 1)}
    if not grams:
        return False
    for stem in stems:
        stem_grams = {stem[index:index + 2] for index in range(len(stem) - 1)}
        if len(grams & stem_grams) / len(grams) >= _STEM_BIGRAM_CONTAINMENT:
            return True
    return False


class _PaperCacheRoutingMixin:
    """PaperCache's methods for deciding which template page a student page is."""

    def _resolve_joint_page_type(
        self,
        data: dict,
        *,
        visual_fingerprint: str = "",
    ) -> tuple[str, list[str]]:
        """Route a student page by its content identity, then check the binding.

        Template pages may be different exams whose printed numbers overlap,
        so the paper-global numbers the model reports are never the identity
        on their own.  Content decides first (:meth:`_content_identity_winner`:
        printed stems, printed page caption, server-side visual fingerprint).
        The numbers, printed labels, caption and page role then only have to
        agree with that page.  Without content evidence the numbers decide
        only when no two pages share the observed printed labels.  Anything
        short of exactly one consistent page goes to teacher review.
        """
        if not isinstance(data, dict):
            raise ValueError("联合页型识别结果必须是 JSON 对象")
        selected_label = str(data.get("page_type_id") or "").strip()
        raw_observed = data.get("observed_question_numbers")
        if not isinstance(raw_observed, (list, tuple)):
            raise ValueError("联合识别缺少 observed_question_numbers")

        observed: list[str] = []
        seen: set[str] = set()
        for raw_question_number in raw_observed:
            question_number = _normalize_question_number(
                str(raw_question_number or "")
            )
            if not question_number:
                continue
            if question_number in seen:
                raise ValueError("联合识别返回了重复题号")
            seen.add(question_number)
            observed.append(question_number)
        if not observed and not self._non_graded_pages:
            raise ValueError("联合识别没有返回可验证的印刷题号")

        observed_page_label = _normalize_page_label_text(data.get("observed_page_label"))
        raw_printed = data.get("observed_printed_labels")
        observed_printed = {
            label
            for label in (
                _normalize_page_label_text(value)
                for value in (raw_printed if isinstance(raw_printed, list) else [])
            )
            if label
        }
        content_label = self._content_identity_winner(
            data, observed_page_label, visual_fingerprint,
        )

        # Binding gates: which pages the reported numbers, printed labels,
        # caption and role are consistent with.  They never pick a page alone.
        consistent = []
        for label in self._routable_page_labels():
            identity = self._page_identities.get(label) or {}
            page_label = _normalize_page_label_text(identity.get("printed_page_label"))
            if label in self._non_graded_pages:
                # A page without contract numbers is never chosen by default:
                # an unreadable answer page must not silently become ungraded.
                if observed or selected_label != label or content_label != label:
                    continue
            else:
                question_numbers = set(self._expected_question_numbers(label))
                if not observed or not set(observed).issubset(question_numbers):
                    continue
            printed_labels = self._printed_label_set(label)
            if printed_labels and observed_printed and not observed_printed <= printed_labels:
                continue
            if page_label and observed_page_label and page_label != observed_page_label:
                continue
            consistent.append(label)

        if content_label:
            if content_label not in consistent:
                raise ValueError("试卷内容与模型返回的题号或页型不一致，需人工复核")
            resolved = content_label
        else:
            # Numbers alone identify a page only when its printed labels are
            # unique in this template (one exam numbered across its pages).
            # Two exams that both print "1." need content evidence.
            label_fit = [
                label for label in self._baselines
                if self._printed_label_set(label)
                and (not observed_printed or observed_printed <= self._printed_label_set(label))
            ]
            if len(label_fit) > 1 and self._printed_labels_overlap(label_fit):
                raise ValueError("多页模板的印刷题号重复且缺少试卷内容证据，需人工复核")
            if len(consistent) != 1:
                raise ValueError("题号不能唯一匹配已确认页型，需人工复核")
            resolved = consistent[0]
        if selected_label and selected_label not in self._baselines and selected_label not in self._non_graded_pages:
            raise ValueError("模型返回了不存在的页型，需人工复核")
        if selected_label and selected_label != resolved:
            raise ValueError("模型返回的页型与题号锚点冲突，需人工复核")
        return resolved, observed

    def _routable_page_labels(self) -> list[str]:
        return sorted(
            set(self._baselines) | set(self._non_graded_pages),
            key=lambda value: _question_number_sort_key(str(value)),
        )

    def _printed_label_set(self, label: str) -> set[str]:
        identity = self._page_identities.get(label) or {}
        return {
            normalized
            for normalized in (
                _normalize_page_label_text(value)
                for value in identity.get("printed_question_labels") or []
            )
            if normalized
        }

    def _printed_labels_overlap(self, labels: list[str]) -> bool:
        seen: set[str] = set()
        for label in labels:
            printed = self._printed_label_set(label)
            if seen & printed:
                return True
            seen |= printed
        return False

    def _content_identity_winner(
        self,
        data: dict,
        observed_page_label: str,
        visual_fingerprint: str,
    ) -> str:
        """Return the one template page the student page's content belongs to.

        Three independent content signals, each silent unless decisive:

        * printed stems -- ``observed_stem_snippets`` (openings of the printed
          questions the model sees) matched server-side against each page's
          confirmed question text, which is never sent to the model;
        * the printed page caption (exam title) against the teacher's label;
        * the coarse visual fingerprint, only with a clear margin.

        Signals that point to different pages send the page to teacher review.
        An empty result means there is no content evidence at all.
        """
        winners: set[str] = set()
        labels = self._routable_page_labels()

        caption_matches = [
            label for label in labels
            if observed_page_label and _normalize_page_label_text(
                (self._page_identities.get(label) or {}).get("printed_page_label")
            ) == observed_page_label
        ]
        if len(caption_matches) == 1:
            winners.add(caption_matches[0])

        raw_snippets = data.get("observed_stem_snippets")
        snippets = [
            text for text in (
                _content_text(value)
                for value in (raw_snippets if isinstance(raw_snippets, list) else [])[:12]
            )
            if len(text) >= _MIN_STEM_SNIPPET_CHARS
        ]
        if snippets:
            stems = {
                label: [
                    text for text in (
                        _content_text(question.get("question_text"))
                        for question in self._baselines[label]["ocr_result"].get("questions", [])
                    )
                    if text
                ]
                for label in self._baselines
            }
            hits = {label: 0 for label in stems}
            for snippet in snippets:
                for label, page_stems in stems.items():
                    if _snippet_matches_stems(snippet, page_stems):
                        hits[label] += 1
            ranked = sorted(hits.items(), key=lambda item: item[1], reverse=True)
            top_label, top_hits = ranked[0] if ranked else ("", 0)
            runner_up = ranked[1][1] if len(ranked) > 1 else 0
            if top_hits >= 1 and top_hits >= 2 * runner_up:
                winners.add(top_label)

        if visual_fingerprint:
            from services.template_page_identity import visual_winner
            visual_label = visual_winner(visual_fingerprint, {
                label: identity["visual_fingerprint"]
                for label, identity in self._page_identities.items()
                if identity.get("visual_fingerprint")
            })
            if visual_label:
                winners.add(visual_label)

        if len(winners) > 1:
            raise ValueError("试卷内容特征指向不同页型，需人工复核")
        return next(iter(winners), "")
