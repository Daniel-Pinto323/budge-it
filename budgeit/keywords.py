"""Keyword auto-categorization: if a transaction's merchant text contains a keyword, it gets that rule's category.

Matching is case-insensitive and "contains". When several keywords match, the longest (most specific)
wins, so "uber eats" beats "uber" without any ordering to manage.
"""

from __future__ import annotations

DEFAULTS: dict[str, list[str]] = {
    "Transfer": ["autopay", "crcardpmt", "credit crd", "transfer", "performance savings", "360 checking", "fid bkg svc", "coinbase"],
    "Income": ["payroll", "direct dep", "interest paid"],
    "Zelle": ["zelle"],
    "Dining": ["uber eats", "ubereats", "doordash", "grubhub", "seamless", "postmates", "starbucks", "dunkin",
               "mcdonald", "chipotle", "restaurant", "pizza"],
    "Transportation": ["lyft", "uber", "divvy", "citibike", "citi bike", "metrocard", "ventra", "parking", "shell",
                       "exxon", "chevron", "sunoco"],
    "Subscriptions": ["netflix", "spotify", "hulu", "disney plus", "disney+", "apple.com/bill", "amazon prime",
                      "prime video", "youtube", "icloud"],
    "Groceries": ["whole foods", "trader joe", "kroger", "safeway", "aldi", "publix", "wegmans", "instacart", "costco", "marianos", "jewel"],
    "Utilities": ["comcast", "xfinity", "verizon", "at&t", "t-mobile", "con edison", "electric"],
    "Travel": ["airlines", "delta air", "united air", "american air", "southwest", "airbnb", "marriott", "hilton",
               "hotel", "expedia"],
    "Shopping": ["amazon", "amzn", "target", "walmart", "best buy", "etsy", "ebay"],
}


# Keywords added after the first release; the schema upgrade that introduced them adds just these (and only these)
# to existing users, so a default someone deliberately deleted is not revived.
ADDED_IN_SCHEMA_4: dict[str, list[str]] = {
    "Transfer": ["360 checking", "fid bkg svc", "coinbase"],
    "Groceries": ["marianos", "jewel"],
}


def match(rules: list[tuple[str, str]], text: str) -> str | None:
    """rules: (lower-cased keyword, category) pairs. Returns the category of the longest keyword found in text."""
    lowered = text.lower()
    best: tuple[int, str] | None = None
    for keyword, category in rules:
        if keyword in lowered and (best is None or len(keyword) > best[0]):
            best = (len(keyword), category)
    return best[1] if best else None
