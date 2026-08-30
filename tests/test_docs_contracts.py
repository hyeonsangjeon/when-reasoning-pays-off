from scripts import check_docs_contracts


def test_public_documentation_contracts() -> None:
    check_docs_contracts.check()
