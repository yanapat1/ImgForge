"""python -m dataviz  (run from scripts/image-tools/) — same as the app.py launcher.
`python -m dataviz dc ...` runs one Data Center tool instead (datacenter_cli.py)."""

import sys

if len(sys.argv) > 1 and sys.argv[1] == 'dc':
    from .datacenter_cli import main as dc_main
    sys.exit(dc_main(sys.argv[2:]))

from .cli import main  # noqa: E402

main()
