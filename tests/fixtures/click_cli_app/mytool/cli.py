"""Every Click declaration shape the extractor is expected to recognise."""
import click


@click.group()
def main():
    """Root group; not itself an invocation that does work."""


@main.command()
def start():
    """Name comes from the function, unchanged."""


@main.command()
def list_items():
    """Name comes from the function, with the underscore dashed."""


@main.command("build-all")
def build(force: bool = False):
    """Explicit name beats the function name."""


@main.group()
def db():
    """Nested group; also not a leaf."""


@db.command("migrate-up")
def db_migrate():
    """Explicit name, one group deep."""


@db.command()
def reset_db():
    """Derived name, one group deep."""
