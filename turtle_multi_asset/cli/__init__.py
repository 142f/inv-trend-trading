"""CLI package."""

__all__ = ["main"]


def main() -> None:
    """Run the CLI without importing its module during package initialization."""

    from .data_cli import main as run

    run()
