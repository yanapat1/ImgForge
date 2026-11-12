"""python -m dataviz  (run from scripts/image-tools/) — same as the app.py launcher.
`python -m dataviz bucket ...` runs one Bucket tool instead (bucket_cli.py).
`python -m dataviz data-sample status|reset|remove` looks after the example database (data/sample.py)."""

import sys

if len(sys.argv) > 1 and sys.argv[1] == 'bucket':
    from .bucket_cli import main as bucket_main
    sys.exit(bucket_main(sys.argv[2:]))

if len(sys.argv) > 1 and sys.argv[1] == 'data-sample':
    from .data.sample import cli as sample_cli
    sys.exit(sample_cli(sys.argv[2:]))

from .cli import main  # noqa: E402

main()
