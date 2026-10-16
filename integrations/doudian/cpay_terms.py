"""Installment choices shared by browser selection and API verification."""


def required_cpay_terms(installments) -> tuple[int, ...]:
    """An explicit selection is exact; a scalar is a legacy request."""
    if isinstance(installments, (list, tuple)):
        if not installments or any(isinstance(term, bool) or str(term) not in {"6", "12", "24"}
                                   for term in installments):
            raise ValueError("分期免息只能选择 6、12、24 期")
        terms = tuple(sorted(int(term) for term in installments))
        if len(terms) != len(set(terms)):
            raise ValueError("分期期数不能重复")
        return terms
    if isinstance(installments, bool):
        raise ValueError("分期免息期数无效")
    # Existing persisted batches used 24 to mean both 12 and 24.
    term = int(installments)
    if term == 12:
        return (12,)
    if term == 24:
        return (12, 24)
    raise ValueError("旧版分期免息仅支持 12 期或 24 期")
