import computer_use


def test_package_exposes_version() -> None:
    assert isinstance(computer_use.__version__, str)
    assert computer_use.__version__
