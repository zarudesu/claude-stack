from app.export import to_csv


class TestExport:
    def test_to_csv(self):
        result = to_csv([1, 2, 3])
        assert result == "amount\n1\n2\n3"
