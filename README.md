# Trading Alerts

Backtesting, forward testing and phone alerts for **trading rules you write yourself**,
running on your own computer or server. Open source under the
[GNU AGPL-3.0](LICENSE).

> Not financial advice. Trading Alerts follows rules you set; it doesn't recommend trades.
> See [engine/DISCLAIMER.md](engine/DISCLAIMER.md).

- **Use it:** download a release, then run `setup.sh` (Linux, macOS) or `setup.ps1`
  (Windows). See [selfhost/README.md](selfhost/README.md).
- **Reference:** strategy format, indicators, how trades are simulated, price data:
  [engine/README.md](engine/README.md).
- **Don't want to run it yourself?** A hosted version with a desktop app is available by
  subscription.

## Repository

```
engine/      the Python package: engine, web interface, tests
selfhost/    setup scripts, Docker files and the desktop app (Tauri)
scripts/     release.py builds the downloadable zip
```

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e "engine[dev,yahoo]"
pytest engine/tests
tradealerts serve        # http://localhost:8321
```

## Licence

Copyright (C) 2026 William Cooper. This program is free software: you can redistribute it
and/or modify it under the terms of the GNU Affero General Public License, version 3. If you
run a modified version as a service other people use over a network, you must offer them
its source code. See [LICENSE](LICENSE).
