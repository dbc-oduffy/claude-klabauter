
from __future__ import annotations

import sys
from typing import List, Optional, Tuple

_PROG = "parse-completeness-item"

_CLASSES = ("live", "restart-gated")


class _Malformed(Exception):
    pass


def _read_item_from_stdin() -> str:
    data = sys.stdin.read()
    lines = data.split("\n")[:-1]

    item = ""
    for line in lines:
        if line == "":
            continue
        if item != "":
            raise _Malformed("multi-line input not supported")
        item = line
    return item


def _split_class(item: str) -> Tuple[str, str]:
    for cls in _CLASSES:
        prefix = f"{cls}: "
        if item.startswith(prefix):
            return cls, item[len(prefix):]

    if item in (f"{c}:" for c in _CLASSES):
        raise _Malformed(f'empty assertion (nothing after class prefix): {item}')

    if ":" in item:
        bad_class = item.split(":", 1)[0]
        raise _Malformed(
            f'unknown class "{bad_class}" (must be "live" or "restart-gated"): {item}'
        )

    raise _Malformed(f'missing "<class>: " prefix: {item}')


def _strip_trailing_ws(text: str) -> str:
    end = len(text)
    while end > 0 and text[end - 1] in (" ", "\t"):
        end -= 1
    return text[:end]


def _extract_probe(item: str, item_rest: str) -> Tuple[str, str]:
    needle = " [probe: "
    idx = item_rest.find(needle)
    if idx == -1:
        return _strip_trailing_ws(item_rest), ""

    assertion = item_rest[:idx]
    remainder = item_rest[idx + len(needle):]

    if assertion == "":
        raise _Malformed(
            f"empty assertion (probe present but assertion is empty): {item}"
        )

    if not remainder.endswith("]"):
        raise _Malformed(f'unterminated [probe: (no closing "]"): {item}')

    # Peel from the right to find the final UNESCAPED "]" -- a "\]" pair is an
    probe_scan = remainder
    probe_raw_content: Optional[str] = None
    while True:
        without_close = probe_scan[:-1]
        if without_close.endswith("\\"):
            probe_scan = without_close[:-1]
            if probe_scan.endswith("]"):
                continue
            raise _Malformed(
                f'unterminated [probe: (all "]" are escaped): {item}'
            )
        probe_raw_content = without_close
        break

    if not probe_raw_content:
        raise _Malformed(f"empty probe command in [probe: ]: {item}")

    probe = probe_raw_content.replace("\\]", "]")
    return assertion, probe


def parse_completeness_item(item: str) -> Tuple[str, str, str]:
    if item == "":
        raise _Malformed("empty input")

    item_class, item_rest = _split_class(item)

    if item_rest == "":
        raise _Malformed(f"empty assertion after class prefix: {item}")

    assertion, probe = _extract_probe(item, item_rest)

    if assertion == "":
        raise _Malformed(f"empty assertion: {item}")

    return item_class, assertion, probe


def main(argv: List[str]) -> int:
    if argv:
        item = argv[0]
    else:
        try:
            item = _read_item_from_stdin()
        except _Malformed as exc:
            print(f"{_PROG}: malformed item: {exc}", file=sys.stderr)
            return 1

    try:
        item_class, assertion, probe = parse_completeness_item(item)
    except _Malformed as exc:
        print(f"{_PROG}: malformed item: {exc}", file=sys.stderr)
        return 1

    print(f"class={item_class}")
    print(f"assertion={assertion}")
    print(f"probe={probe}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
