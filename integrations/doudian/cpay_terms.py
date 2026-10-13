"""Installment choices shared by browser selection and API verification."""


def required_cpay_terms(installments: int) -> tuple[int, ...]:
    term = int(installments)
    if term == 12:
        return (12,)
    if term == 24:
        return (12, 24)
    raise ValueError("分期免息仅支持 12 期或 24 期")
