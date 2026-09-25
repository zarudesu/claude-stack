from app.ledger import Ledger


class TestLedger:
    def test_add_and_balance(self):
        ledger = Ledger()
        ledger.add(10)
        ledger.add(5)
        assert ledger.balance() == 15

    def test_negative_amount_rejected(self):
        ledger = Ledger()
        try:
            ledger.add(-1)
        except ValueError:
            return
        assert False, "expected ValueError"
