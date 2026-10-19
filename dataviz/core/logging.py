"""
Terminal logging for the dataset visualizer (rich).

One shared Console: log lines, job progress bars and the startup banner all go through it,
so lines print cleanly above the live bars. Each subsystem gets a coloured tag:

    2026-10-16 12:03:41  INFO     index      cache hit: 9877 boxes                        indexer.py:212
    2026-10-16 12:03:42  INFO     http       POST   /api/projection → 200 14 ms  10.0.0.5     app.py:94

Every record carries the full date + time on the left and the source file:line on the right (so you can tell which
python file logged it), and a blank line separates records.
"""

import logging

from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

TAG_STYLES = {
    'index': 'bold cyan', 's3': 'bold magenta', 'sync': 'bold magenta', 'jobs': 'bold blue',
    'http': 'dim cyan', 'config': 'bold yellow', 'workspace': 'bold green', 'features': 'bold bright_blue',
    'datasets': 'bold green', 'app': 'bold white', 'workers': 'bold bright_magenta',
}

TIME_FORMAT = '[%Y-%m-%d %H:%M:%S]'

console = Console(theme=Theme({'logging.level.warning': 'bold yellow', 'logging.level.error': 'bold red'}),
                  log_time_format=TIME_FORMAT)
if not console.is_terminal:                 # `docker logs` / a pipe: rich would wrap at 80 columns
    console.width = 130
_configured = False


class _SpacedHandler(RichHandler):
    """RichHandler that leaves an empty line after every record, so entries are easy to tell apart."""

    def emit(self, record):
        super().emit(record)
        if not getattr(self, '_compact', False):
            self.console.print()


class _TagFilter(logging.Filter):
    """Prefix every record with its coloured subsystem tag (the logger name below 'dataviz.')."""

    def filter(self, record):
        tag = record.name.split('.', 1)[1] if record.name.startswith('dataviz.') else record.name
        if not getattr(record, '_tagged', False):
            style = TAG_STYLES.get(tag, 'bold white')
            record.msg = f'[{style}]{tag:<9}[/] {record.msg}'
            record._tagged = True
        return True


def setup_logging(level='info'):
    global _configured
    root = logging.getLogger('dataviz')
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    if _configured:
        return
    handler = _SpacedHandler(console=console, markup=True, rich_tracebacks=True, tracebacks_show_locals=False,
                             show_path=True, log_time_format=TIME_FORMAT, omit_repeated_times=False)
    handler.addFilter(_TagFilter())
    root.addHandler(handler)
    root.propagate = False
    _configured = True


def get_logger(tag):
    if not _configured:
        setup_logging()
    return logging.getLogger(f'dataviz.{tag}')


def log_fn(tag):
    """A print-like callable (for code that takes log=print)."""
    lg = get_logger(tag)

    def log(msg, *_, **__):
        msg = str(msg)
        # legacy "[tag] message" strings: drop the tag, the handler adds a coloured one
        if msg.startswith('[') and '] ' in msg[:24]:
            msg = msg.split('] ', 1)[1]
        lg.info(esc(msg), stacklevel=2)      # report the caller's file:line, not this wrapper's
    return log


def text_bar(frac, width=24):
    """A text progress bar for log lines (`docker logs` / a pipe cannot show rich's live bars)."""
    frac = 0.0 if frac is None else max(0.0, min(1.0, frac))
    n = round(frac * width)
    return f'[cyan]{"█" * n}[/][dim]{"░" * (width - n)}[/] [bold]{frac * 100:3.0f}%[/]'


def esc(s):
    """Escape rich markup in user data (paths, errors) before logging it."""
    return str(s).replace('[', r'\[')


def banner(title, rows, url):
    t = Table.grid(padding=(0, 2))
    t.add_column(style='bold cyan', justify='right')
    t.add_column()
    for k, v in rows:
        t.add_row(k, Text(str(v)))
    t.add_row('open', Text(url, style='bold underline bright_green'))
    console.print(Panel(t, title=f'[bold]{title}[/]', border_style='bright_blue', expand=False, padding=(1, 2)))
