from click.testing import CliRunner
from bluepysnap.cli import cli
from utils import TEST_DATA_DIR


def test_cli_correct():
    runner = CliRunner()
    result = runner.invoke(cli, ['validate', str(TEST_DATA_DIR / 'circuit_config.json')])
    assert result.exit_code == 0
    assert result.stdout == ''


def test_cli_no_config():
    runner = CliRunner()
    result = runner.invoke(cli, ['validate'])
    assert result.exit_code == 2
    assert 'Missing argument "CONFIG_FILE"' in result.stdout
