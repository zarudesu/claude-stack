"""In-memory ledger with a balance and simple entries.

Amounts must be non-negative; add() raises ValueError on a negative amount.
"""


class Ledger:
    def __init__(self):
        self._entries = []

    def add(self, amount):
        if amount < 0:
            raise ValueError("amount must not be negative")
        self._entries.append(amount)

    def balance(self):
        return sum(self._entries)
