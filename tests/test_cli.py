from yukitools_rime.cli import main


def test_empty_command_is_successful() -> None:
    assert main([]) == 0
