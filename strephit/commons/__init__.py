import click
from strephit.commons import pos_tag, io


CLI_COMMANDS = {
    'pos_tag': pos_tag.main,
    'preprocess_corpus': io.preprocess_corpus,
}


@click.group(name='commons', commands=CLI_COMMANDS)
@click.pass_context
def cli(ctx):
    """ Common utilities used by others """
    pass